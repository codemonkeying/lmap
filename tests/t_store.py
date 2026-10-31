#!/usr/bin/env python3
"""Cache vs store: `cache list|rm|prune|pin|unpin` only ever delete area/map folders, refuse pinned ones,
honour --dry-run and need --yes; `data rm` / `import rm` (the only copy) need --yes AND --confirm ID (+ --force for
imported ones); `import add` takes GeoJSON/GPX/GeoTIFF, an ATAK .zip and an .lmapbundle made by `export bundle`
(round trip, renamed with --as); import_clip puts overlapping imported layers into an area folder; sat_even evens
open water only (rivers untouched). All in a scratch root. Usage: t_store.py <scratch>"""
import json, os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
R = T / "root"; R.mkdir()
ENV = {**os.environ, "LMAP_ROOT": str(R), "LMAP_TMP": str(T / "scr"), "XDG_STATE_HOME": str(T / "st"),
       "XDG_CONFIG_HOME": str(T / "cfg"), "LMAP_SCOPE": "0"}
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


def lm(*a, inp=None):
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "--json", *a], capture_output=True, text=True,
                       env=ENV, timeout=200, stdin=subprocess.DEVNULL)
    try:
        j = json.loads(r.stdout or r.stderr.strip().splitlines()[-1])
    except (ValueError, IndexError):
        j = {"raw": r.stdout + r.stderr}
    return r.returncode, j


def mkarea(kind, name, age_days=0, box=(146.7, -19.3, 146.9, -19.1)):
    d = R / kind / "australia" / "queensland" / name; d.mkdir(parents=True)
    (d / "area.json").write_text(json.dumps({"name": name, "level": "local", "km2": 1, "bbox": list(box),
                                             "crs": "EPSG:7855", "made": "2026-01-01 00:00"}))
    (d / "x.tif").write_bytes(b"0" * 1000)
    t = __import__("time").time() - age_days * 86400
    for f in d.iterdir():
        os.utime(f, (t, t))
    return d


# ---------------------------------------------------------------- cache
a1, a2, m1 = mkarea("areas", "old-area", 60), mkarea("areas", "keep-area", 60), mkarea("maps", "new-map", 0)
data = R / "data" / "x"; data.mkdir(parents=True); (data / "precious.tif").write_bytes(b"1")
rc, j = lm("cache", "list")
check(rc == 0 and {i["name"] for i in j} == {"old-area", "keep-area", "new-map"}, f"cache list ({rc})")
rc, j = lm("cache", "pin", "keep-area", "--yes")
check(rc == 0 and (a2 / ".lmap-pin").is_file(), "pin writes the marker")
rc, j = lm("cache", "prune", "--older", "30", "--dry-run")
check(rc == 0 and [i["name"] for i in j["delete"]] == ["old-area"] and a1.is_dir(), f"prune --dry-run: only the old unpinned one, nothing deleted ({j.get('delete')})")
rc, j = lm("cache", "prune", "--older", "30")
check(rc == 5 and a1.is_dir(), f"prune without --yes refused, exit 5 ({rc})")
rc, j = lm("cache", "prune")
check(rc == 2, "prune with no criterion = usage error")
rc, j = lm("cache", "prune", "--older", "30", "--yes")
check(rc == 0 and not a1.exists() and a2.is_dir() and m1.is_dir() and (data / "precious.tif").is_file(),
      "prune --yes: old area gone, pinned + new kept, data/ untouched")
rc, j = lm("cache", "rm", "keep-area", "--yes")
check(rc == 5 and a2.is_dir(), "rm of a pinned area refused without --force")
rc, j = lm("cache", "rm", "keep-area", "--yes", "--force")
check(rc == 0 and not a2.exists(), "rm --force deletes the pinned area")
rc, j = lm("cache", "rm", "nope", "--yes")
check(rc == 4, "rm of an unknown name: not found")
(m1 / "x.part.tif").write_bytes(b"0")
rc, j = lm("cache", "rm", "new-map", "--yes")
check(rc == 5 and m1.is_dir(), "rm refuses a folder being built (fresh .part file)")
(m1 / "x.part.tif").unlink()

# ---------------------------------------------------------------- import
gj = T / "route.geojson"
gj.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "wp1"},
              "geometry": {"type": "LineString", "coordinates": [[146.75, -19.25], [146.8, -19.2]]}}]}))
rc, j = lm("import", "add", str(gj), "--as", "route", "--dry-run")
check(rc == 0 and j.get("dry_run") and not (R / "local").exists(), "import --dry-run writes nothing")
rc, j = lm("import", "add", str(gj), "--as", "route")
check(rc == 5, "import without --yes refused")
rc, j = lm("import", "add", str(gj), "--as", "route", "--yes")
check(rc == 0 and (R / "local/route/route.gpkg").is_file(), f"geojson imported ({j})")
rc, j = lm("import", "add", str(gj), "--as", "route", "--yes")
check(rc == 5, "same id twice refused")
gpx = T / "wp.gpx"
gpx.write_text('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
               '<wpt lat="-19.22" lon="146.77"><name>camp</name></wpt></gpx>')
from osgeo import gdal, osr
gdal.UseExceptions()
tif = T / "overlay.tif"
ds = gdal.GetDriverByName("GTiff").Create(str(tif), 20, 20, 3, gdal.GDT_Byte)
ds.SetGeoTransform((146.7, 0.01, 0, -19.1, 0, -0.01)); sr = osr.SpatialReference(); sr.ImportFromEPSG(4326)
ds.SetProjection(sr.ExportToWkt()); [ds.GetRasterBand(b).Fill(100) for b in (1, 2, 3)]; ds = None
pkg = T / "atak.zip"
with zipfile.ZipFile(pkg, "w") as z:
    z.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest/>"); z.write(gpx, "a/wp.gpx"); z.write(tif, "b/overlay.tif")
rc, j = lm("import", "add", str(pkg), "--as", "patrol", "--yes")
check(rc == 0 and len(j.get("imported", [])) == 2 and (R / "local/patrol-1").is_dir() and (R / "local/patrol-2").is_dir(),
      f"ATAK package: each layer imported ({[r.get('id') for r in j.get('imported', [])]})")
rc, j = lm("import", "list")
check(rc == 0 and {i["id"] for i in j} == {"route", "patrol-1", "patrol-2"} and all(i["present"] for i in j), "import list")
rc, j = lm("import", "rm", "route", "--yes")
check(rc == 5 and (R / "local/route").is_dir(), "import rm: refused without --force")
rc, j = lm("import", "rm", "route", "--yes", "--force")
check(rc == 5 and (R / "local/route").is_dir(), "import rm: refused without --confirm ID")
rc, j = lm("import", "rm", "route", "--yes", "--force", "--confirm", "rout")
check(rc == 5 and (R / "local/route").is_dir(), "import rm: wrong id typed refused")

# import_clip: overlapping imported layers land in an area folder
sys.argv = ["lmap"]; os.environ.update(ENV)
g = {"__name__": "x", "__file__": str(HERE / "bin" / "lmap")}
exec(compile((HERE / "bin" / "lmap").read_text().split("if __name__")[0], "lmap", "exec"), g)
ctx = g["Ctx"](g["build_parser"]().parse_args(["status"]))
got = g["import_clip"](ctx, m1, [146.7, -19.3, 146.9, -19.1], "EPSG:7855")
check(set(got) == {"route", "patrol-1", "patrol-2"} and (m1 / "imp-route.gpkg").is_file() and (m1 / "imp-patrol-2.tif").is_file(),
      f"import_clip: overlapping layers clipped into the map folder ({got})")
far = mkarea("areas", "far", 0, (150.0, -34.0, 150.1, -33.9))
check(g["import_clip"](ctx, far, [150.0, -34.0, 150.1, -33.9], "EPSG:7856") == {}, "import_clip: nothing for a far area")
rc, j = lm("import", "rm", "route", "--yes", "--force", "--confirm", "route")
check(rc == 0 and not (R / "local/route").exists() and "route" not in (R / "local/sources.local.toml").read_text(),
      "import rm with --yes --force --confirm: gone from disk and catalog")
g["import_clip"](ctx, m1, [146.7, -19.3, 146.9, -19.1], "EPSG:7855")
check(not (m1 / "imp-route.gpkg").exists(), "a removed import's clip is dropped from areas")

# ---------------------------------------------------------------- bundle round trip
usb = T / "usb"; usb.mkdir()
rc, j = lm("export", "bundle", "new-map", "--to", str(usb), "--yes")
b = usb / "new-map.lmapbundle"
check(rc == 0 and b.is_file(), f"export bundle ({j})")
rc, j = lm("import", "add", str(b), "--yes")
check(rc == 5, "importing a bundle over an existing map refused")
rc, j = lm("import", "add", str(b), "--as", "copy-map", "--yes")
nd = R / "maps/australia/queensland/copy-map"
check(rc == 0 and (nd / "area.json").is_file() and json.loads((nd / "area.json").read_text())["name"] == "copy-map"
      and (nd / "imp-patrol-2.tif").is_file(), f"bundle imported under a new name, with its layers ({j})")

check(not (nd / ".lmap-pin").exists(), "unpinned map bundle: not pinned on import")
rc, j = lm("cache", "pin", "new-map", "--yes")
rc, j = lm("export", "bundle", "new-map", "--to", str(usb), "--yes")
check(rc == 0 and j.get("pinned") and not any("lmap-pin" in n for n in __import__("tarfile").open(b).getnames()),
      f"pinned map: bundle says pinned, the pin file is not packed ({j})")
rc, j = lm("import", "add", str(b), "--as", "pin-copy", "--yes")
check(rc == 0 and (R / "maps/australia/queensland/pin-copy/.lmap-pin").is_file(), f"pinned bundle: pinned on import ({j})")
rc, j = lm("cache", "unpin", "new-map", "--yes")

# one imported layer as a bundle, by id or local/ID; imported again on another root
rc, j = lm("export", "bundle", "patrol-2", "--to", str(usb), "--yes")
lb = usb / "patrol-2.lmapbundle"
check(rc == 0 and lb.is_file() and j.get("kind") == "local", f"export bundle of an imported layer ({j})")
rc, j = lm("export", "bundle", "local/nope", "--to", str(usb), "--yes")
check(rc == 4, f"export bundle local/<unknown>: not found ({rc})")
rc, j = lm("export", "bundle", "local/patrol-2", "--to", str(usb), "--yes")
check(rc == 0 and j.get("kind") == "local", f"export bundle local/ID form ({j})")
rc, j = lm("import", "add", str(lb), "--as", "dry-l", "--dry-run")
check(rc == 0 and not (R / "local/dry-l").exists() and not list((T / "scr").rglob("*.tif")), f"layer bundle --dry-run unpacks nothing ({j})")
rc, j = lm("import", "add", str(lb), "--yes")
check(rc == 5, "layer bundle with a taken id refused")
rc, j = lm("import", "add", str(lb), "--as", "patrol-copy", "--yes")
check(rc == 0 and (R / "local/patrol-copy/patrol-copy.tif").is_file()
      and j["imported"][0].get("bbox") and "patrol-copy" in (R / "local/sources.local.toml").read_text(),
      f"layer bundle imported as a new id ({j})")

rc, j = lm("import", "add", str(b), "--as", "../../../../evil", "--yes")
check(rc == 2 and not (R.parent / "evil").exists(), f"bundle --as with a path is refused ({rc})")
rc, j = lm("import", "add", str(pkg), "--as", "dryp", "--dry-run")
check(rc == 0 and not (R / "local/dryp-1").exists() and not list((T / "scr").rglob("*.gpx")), "ATAK --dry-run writes nothing")
lc = R / "local/sources.local.toml"; keep = lc.read_text()
lc.write_text(keep.replace('out = "local/patrol-1/patrol-1.gpkg"', 'out = "local/x.gpkg"'))
rc, j = lm("import", "rm", "patrol-1", "--yes", "--force", "--confirm", "patrol-1")
check(rc == 5 and (R / "local/patrol-2").is_dir() and lc.is_file(), "import rm of an edited entry pointing at local/ refused")
lc.write_text(keep)

# ---------------------------------------------------------------- data rm (main store)
cat = T / "cat.toml"
cat.write_text('[source.tsrc]\ntitle = "t"\nkind = "url"\nurl = "http://127.0.0.1:9/x"\nout = "data/x/precious.tif"\n'
               '[source.tman]\ntitle = "m"\nkind = "manual"\nout = "data/x/manual.tif"\n')
(data / "manual.tif").write_bytes(b"2"); ENV["LMAP_CATALOG"] = str(cat)
rc, j = lm("data", "rm", "tsrc", "--dry-run")
check(rc == 0 and j.get("dry_run") and (data / "precious.tif").is_file() and "re-download" in j.get("after", ""), "data rm --dry-run")
rc, j = lm("data", "rm", "tsrc", "--yes")
check(rc == 5 and (data / "precious.tif").is_file(), "data rm --yes alone refused (needs --confirm ID)")
rc, j = lm("data", "rm", "tman", "--yes", "--confirm", "tman")
check(rc == 5 and (data / "manual.tif").is_file(), "data rm of a not-downloadable source needs --force")
rc, j = lm("data", "rm", "tsrc", "--yes", "--confirm", "tsrc")
check(rc == 0 and not (data / "precious.tif").exists() and (data / "manual.tif").is_file(), "data rm --yes --confirm deletes only that source")

# ---------------------------------------------------------------- sat_even
import numpy as np
W = H = 400
st = T / "sat.tif"; vg = T / "veg.tif"; se = T / "se.tif"
for p, nb in ((st, 3), (vg, 1)):
    ds = gdal.GetDriverByName("GTiff").Create(str(p), W, H, nb, gdal.GDT_Byte)
    ds.SetGeoTransform((400000, 10, 0, 7870000, 0, -10)); s_ = osr.SpatialReference(); s_.ImportFromEPSG(7855)
    ds.SetProjection(s_.ExportToWkt())
    if nb == 3:
        a = np.full((H, W), 120, "u1"); a[:, 200:] = 60; a[:, :50] = 200          # sea: two patches (60 / 120); land left
        a[300:390, 5:45] = 30                                                    # ...and a dark dam on the land
        for b_ in (1, 2, 3):
            ds.GetRasterBand(b_).WriteArray(a)
    else:
        v = np.full((H, W), 80, "u1"); v[:, :50] = 10; v[100:104, :50] = 80      # water, land strip with a thin river
        v[300:390, 5:45] = 80                                                    # the dam: water, not touching the sea
        ds.GetRasterBand(1).WriteArray(v)
    ds = None
g["sat_even"](ctx, st, vg, se)
_o = gdal.Open(str(se)); o = _o.GetRasterBand(1).ReadAsArray().astype(int); _o = None
seam = abs(int(o[200, 120:180].mean()) - int(o[200, 220:280].mean()))
check(seam < 20, f"sat_even: sea patches evened ({seam} levels apart, were 60)")
check(abs(o[310:380, 15:35] - 30).max() <= 8, f"sat_even: a dam keeps its own colour ({o[310:380, 15:35].mean():.0f}, was 30)")
lnd = np.ones((H, 40), bool); lnd[290:400, :] = False
check(abs(o[:, :40][lnd] - 200).max() <= 6 and abs(o[100:104, :40] - 200).max() <= 6,     # lossy COG: +-6
      f"sat_even: land and a thin river untouched ({abs(o[:, :40][lnd] - 200).max()} max change)")

# a lake wider than SEA_MIN_WIDTH_M is evened too, but to ITS OWN median, not the (bigger) sea's
st2, vg2, se2 = T / "sat2.tif", T / "veg2.tif", T / "se2.tif"
for p, nb in ((st2, 3), (vg2, 1)):
    ds = gdal.GetDriverByName("GTiff").Create(str(p), W, H, nb, gdal.GDT_Byte)
    ds.SetGeoTransform((400000, 10, 0, 7870000, 0, -10)); ds.SetProjection(s_.ExportToWkt())
    if nb == 3:
        a = np.full((H, W), 200, "u1"); a[:, 250:] = 120                      # land; sea (1.5 km) on the right
        a[50:170, 20:80] = 30; a[50:170, 80:140] = 50                        # 1.2 km lake: two shades
        for b_ in (1, 2, 3):
            ds.GetRasterBand(b_).WriteArray(a)
    else:
        v = np.full((H, W), 10, "u1"); v[:, 250:] = 80; v[50:170, 20:140] = 80
        ds.GetRasterBand(1).WriteArray(v)
    ds = None
g["sat_even"](ctx, st2, vg2, se2)
_o = gdal.Open(str(se2)); o = _o.GetRasterBand(1).ReadAsArray().astype(int); _o = None
lk = o[70:150, 40:120]
check(25 <= lk.mean() <= 55 and abs(int(o[110, 40:60].mean()) - int(o[110, 100:120].mean())) < 12,
      f"sat_even: a wide lake is evened to its own colour ({lk.mean():.0f}, was 30/50), not the sea's (120)")
check(abs(o[150:250, 300:380] - 120).max() <= 8, f"sat_even: sea next to it stays {o[150:250, 300:380].mean():.0f} (was 120)")

shutil.rmtree(T, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
