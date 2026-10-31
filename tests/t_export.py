#!/usr/bin/env python3
"""lmap export end to end on a tiny made-up library: --dry-run lists what it would do and writes nothing; copy
mirrors the root (no .part / .lock files) plus the tool; phone builds MBTiles from imagery over the bbox; atak makes
DTED cells from the DEM; verify passes, then catches a changed byte and a missing file; a re-run reuses the work.
Usage: t_export.py <scratch>"""
import json, types, os, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


from osgeo import gdal, osr
import numpy as np
gdal.UseExceptions()

root = tmp / "root"; usb = tmp / "usb"; usb.mkdir()


def raster(path, bands, dtype, box, px, fill):
    path.parent.mkdir(parents=True, exist_ok=True)
    w, s, e, n = box; W, H = round((e - w) / px), round((n - s) / px)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), W, H, bands, dtype, ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((w, px, 0, n, 0, -px)); sr = osr.SpatialReference(); sr.ImportFromEPSG(4326)
    ds.SetProjection(sr.ExportToWkt())
    for b in range(1, bands + 1):
        ds.GetRasterBand(b).WriteArray(fill(H, W, b))
    ds = None


# a 1x1 degree DEM (90 m class) and a small imagery raster, at the catalog paths lmap expects
BOX = (146.0, -20.0, 147.0, -19.0)
yy, xx = np.mgrid[0:400, 0:400]
raster(root / "data/australia/dem-90m.cog.tif", 1, gdal.GDT_Int16, BOX, 1 / 400,
       lambda H, W, b: (200 + 50 * np.sin(np.mgrid[0:H, 0:W][0] / 30)).astype("int16"))
raster(root / "data/australia/sat-30m.cog.tif", 3, gdal.GDT_Byte, (146.4, -19.6, 146.6, -19.4), 0.0005,
       lambda H, W, b: ((np.mgrid[0:H, 0:W][1] * b) % 255).astype("uint8"))
ad = root / "areas/australia/new-south-wales/a1"             # a built area with a live-source photo clip
raster(ad / "sat.tif", 3, gdal.GDT_Byte, (146.44, -19.56, 146.56, -19.44), 0.0002,
       lambda H, W, b: ((np.mgrid[0:H, 0:W][0] * b) % 255).astype("uint8"))
(ad / "area.json").write_text(json.dumps({"name": "a1", "items": [{"key": "sat", "from": "nsw-imagery"}]}))
far = root / "areas/australia/queensland/far"                  # an area elsewhere: its clip is left out
raster(far / "topo.tif", 3, gdal.GDT_Byte, (140.0, -25.0, 140.1, -24.9), 0.001, lambda H, W, b: np.full((H, W), 9, "uint8"))
(far / "area.json").write_text(json.dumps({"name": "far", "items": [{"key": "topo", "from": "qld-topo"}]}))
tw = root / "areas/australia/queensland/a1"                    # same dir name, other state, same box: no overwrite
raster(tw / "topo.tif", 3, gdal.GDT_Byte, (146.44, -19.56, 146.56, -19.44), 0.0004, lambda H, W, b: np.full((H, W), 50, "uint8"))
(tw / "area.json").write_text(json.dumps({"name": "a1", "items": [{"key": "topo", "from": "qld-topo"}]}))
# field features (AU layer) + an area osm.gpkg with one peak mapped twice: ATAK features.kml
from osgeo import ogr, osr
_sr = osr.SpatialReference(); _sr.ImportFromEPSG(4326)
def _vec(path, layer, gtype, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = ogr.Open(str(path), 1) if path.exists() else ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    ly = ds.CreateLayer(layer, _sr, gtype)
    for f_ in fields:
        ly.CreateField(ogr.FieldDefn(f_, ogr.OFTString))
    for wkt, vals in rows:
        ft = ogr.Feature(ly.GetLayerDefn()); ft.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        for k_, v_ in vals.items():
            ft.SetField(k_, v_)
        ly.CreateFeature(ft)
    ds = None
_fg = root / "data/australia/osm/au-field.gpkg"
_vec(_fg, "field_points", ogr.wkbPoint, ["kind", "name"], [("POINT (146.5 -19.5)", {"kind": "water point", "name": "Bore <1> & tank"}),
                                                            ("POINT (140 -25)", {"kind": "fuel", "name": "far away"})])
_vec(_fg, "field_lines", ogr.wkbLineString, ["kind", "name", "osm_tags"],
     [("LINESTRING (146.46 -19.5, 146.47 -19.5)", {"kind": "4WD track", "name": "Ridge Trail", "osm_tags": '"surface"=>"gravel","4wd_only"=>"yes"'})])
_pd = root / "maps/australia/queensland/pk"; _pd.mkdir(parents=True)
(_pd / "area.json").write_text(json.dumps({"name": "pk", "items": [], "bbox": [146.0, -20.0, 147.0, -19.0]}))
_vec(_pd / "osm.gpkg", "points", ogr.wkbPoint, ["name", "other_tags"],
     [("POINT (146.52 -19.48)", {"name": "Twin Peak", "other_tags": '"natural"=>"peak","ele"=>"512"'}),
      ("POINT (146.5205 -19.4802)", {"name": "Twin Peak", "other_tags": '"natural"=>"peak"'})])
(root / "notes.txt").write_text("hello\n")
(root / "half.part").write_text("partial download")
(root / "x.lock").write_text("")

env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_TMP": str(tmp / "scr"), "XDG_STATE_HOME": str(tmp / "state"),
       "XDG_CONFIG_HOME": str(tmp / "cfg")}


def lmap(*a):
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), *a, "--json"], capture_output=True, text=True,
                       env=env, timeout=280)
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    return r.returncode, j, r.stderr


BB = "146.45,-19.55,146.55,-19.45"

# 1. dry run: plan, nothing written
rc, j, err = lmap("export", "create", str(usb), "--format", "all", "--bbox", BB, "--dry-run")
check(rc == 0 and j.get("dry_run") and set(j.get("formats", [])) == {"copy", "phone", "atak"}, f"dry run plans 3 formats ({err[-200:]})")
check("sat-au-30m.mbtiles" in (j.get("phone") or {}).get("would_build", []), "dry run: phone would build the imagery")
check(not (usb / "lmap-export").exists(), "dry run writes nothing")
rc, j, err = lmap("export", "create", str(usb), "--format", "phone")
check(rc != 0 and "bbox" in err, "phone without --bbox refused (no hidden default area)")
rc, j, err = lmap("export", "create", str(usb), "--format", "copy")
check(rc != 0 and "--yes" in err, "real export without --yes refused")

# 2. real export
rc, j, err = lmap("export", "create", str(usb), "--format", "all", "--bbox", BB, "--yes")
out = usb / "lmap-export"
check(rc == 0, f"export all: exit 0 ({err[-300:]})")
check((out / "maps" / "notes.txt").is_file(), "copy: data mirrored")
check(not (out / "maps" / "half.part").exists() and not (out / "maps" / "x.lock").exists(), "copy: .part/.lock left out")
check((out / "lmap-tool" / "bin" / "lmap").is_file() and not (out / "lmap-tool" / ".git").exists(), "copy: the tool, no .git")
(out / "lmap-tool" / "dev").mkdir(exist_ok=True); (out / "lmap-tool" / "dev" / "old.toml").write_text("x")   # leaked by an old export
(out / "lmap-tool" / ".devenv").write_text("export LMAP_ROOT=/private")
rc, j, err = lmap("export", "create", str(usb), "--format", "copy", "--yes")
import subprocess as _sp                               # only tracked files: no dev worktree / .devenv / PLAN.md
_tr = set(_sp.run(["git", "-C", str(HERE), "ls-files"], capture_output=True, text=True).stdout.split())
_got = {str(p.relative_to(out / "lmap-tool")) for p in (out / "lmap-tool").rglob("*") if p.is_file()}
check(not _tr or (_got <= _tr and "bin/lmap" in _got), f"copy: tool = tracked files only (extra: {sorted(_got - _tr)[:4]})")
import importlib.machinery as _im, importlib.util as _iu
_ld = _im.SourceFileLoader("lmap", str(HERE / "bin" / "lmap")); L = _iu.module_from_spec(_iu.spec_from_loader("lmap", _ld)); _ld.exec_module(L)
_fa = L.tool_filter(tmp / "not-a-repo")
check("--files-from" not in " ".join(_fa) and "/dev/" in _fa and ".devenv" in _fa, "no git: dev/ and .devenv excluded")
mb = out / "phone" / "sat-au-30m.mbtiles"
ok = False
if mb.is_file():
    with sqlite3.connect(mb) as c:
        ok = c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] > 0
check(ok, "phone: MBTiles with tiles")
check((out / "phone" / "area-queensland-a1-topo.mbtiles").is_file(), "phone: same area name in two states: both packed")
check((out / "phone" / "area-new-south-wales-a1-sat.mbtiles").is_file(), "phone: the area's own photo clip (live NSW imagery) is packed")
check(not (out / "phone" / "area-far-topo.mbtiles").exists(), "phone: an area outside the box is not")
(out / "phone" / "area-gone-sat.mbtiles").write_bytes(b"old")                  # an area deleted since: its pack goes
(out / "phone" / "mine-sat.mbtiles").write_bytes(b"user")                      # the user's own file stays
(root / "areas/australia/x/bad").mkdir(parents=True); (root / "areas/australia/x/bad/area.json").write_text('[1, 2]')
(root / "areas/australia/x/nul").mkdir(parents=True); (root / "areas/australia/x/nul/area.json").write_text('{"items": null}')
rc, j, err = lmap("export", "create", str(usb), "--format", "phone", "--bbox", BB, "--yes")
check(rc == 0, f"malformed area.json files skipped, no crash ({err[-200:]})")
check(not (out / "phone" / "area-gone-sat.mbtiles").exists() and (out / "phone" / "mine-sat.mbtiles").is_file(),
      "a removed area's old pack is dropped; other files kept")
_rc = types.SimpleNamespace(root=root, path=lambda x: root / x, sources={"nsw-imagery": {"restricted": True, "licence": "NC"}})
_h = L.restricted_in(_rc, ["copy"], None)
check(_h.get("nsw-imagery") == "NC" and "qld-topo" in _h, f"copy: area clips' licences warned, unknown source flagged ({sorted(_h)})")
_dec = tmp / "decoy"; _dec.mkdir(); (_dec / ".git").write_text("gitdir: x"); (_dec / "work.txt").write_text("mine")
_tl = tmp / "linkusb"; _tl.mkdir(); (_tl / "lmap-export").mkdir(); (_tl / "lmap-export" / "lmap-tool").symlink_to(_dec)
rc, j, err = lmap("export", "create", str(_tl), "--format", "copy", "--yes")     # a decoy, NEVER the real checkout
check(rc != 0 and (_dec / ".git").is_file() and (_dec / "work.txt").is_file(), "lmap-tool is a symlink: refused, nothing deleted behind it")
dt = list((out / "atak").rglob("*.dt1")) + list((out / "atak").rglob("*.dt2"))
check([p.relative_to(out / "atak").as_posix() for p in dt] == ["DTED/e146/s20.dt1"], f"atak: one DTED1 cell e146/s20 ({[p.name for p in dt]})")
if dt:
    d = gdal.Open(str(dt[0])); a = d.ReadAsArray(); d = None
    v = a[a != -32767]                                 # the cell's edge half-pixel lies outside this tiny DEM
    check(v.size > 0.99 * a.size and 195 < float(v.mean()) < 206 and v.min() >= 149 and v.max() <= 251,
          f"atak: DTED heights match the DEM (mean {v.mean():.1f} m, want ~200.5)")
check((out / "atak" / "imagery" / "sat-au-30m.mbtiles").is_file(), "atak: imagery reused from phone")
_fk = out / "atak" / "overlays" / "features.kml"
_kt = _fk.read_text() if _fk.is_file() else ""
check(_kt.count("<Placemark>") == 3 and "far away" not in _kt, f"atak features: 1 point + 1 track + 1 peak in the box ({_kt.count('<Placemark>')})")
check("Bore 1 and tank" in _kt and "&" not in _kt and "Ridge Trail" in _kt and "4wd only: yes" in _kt
      and "surface: gravel" in _kt and "Length: 1.05 km" in _kt,
      "atak features: names escaped, track tags + length (1.05 km) shown on tap")
check("-19.50000, 146.46500" in _kt, "atak features: a 2-point track is tapped at its middle (146.465), not an end")
check("55K" in _kt and "Height: 512 m" in _kt and _kt.count("Twin Peak</name>") == 1,
      "atak features: MGRS, peak height, a twice-mapped peak once")
_kd = ogr.Open(str(_fk)); check(_kd is not None and _kd.GetLayerCount() == 3, "atak features: valid KML (GDAL reads 3 folders)"); _kd = None
import zipfile as _zf
_pk = list((out / "atak").glob("lmap-*.zip"))
check(_pk and "features/features.kml" in _zf.ZipFile(_pk[0]).namelist()
      and "features/features.kml" in _zf.ZipFile(_pk[0]).read("MANIFEST/manifest.xml").decode(),
      f"atak data package carries features.kml ({_pk})")
check((out / "README-OFFLINE.txt").is_file() and (out / "SHA256SUMS").is_file(), "README + SHA256SUMS written")
check(not list((tmp / "scr").rglob("export/*")), "export scratch cleaned")

# 3. verify, then damage
rc, j, err = lmap("export", "verify", str(usb))
check(rc == 0 and j.get("ok") and j.get("checked", 0) > 3, f"verify: ok over {j.get('checked')} files")
f = out / "maps" / "notes.txt"; f.write_text("HELLO\n")
os.utime(f, (1, 1))
(out / "README-OFFLINE.txt").unlink()
rc, j, err = lmap("export", "verify", str(usb))
check(rc != 0 and "maps/notes.txt" in j.get("bad", []) and "README-OFFLINE.txt" in j.get("missing", []),
      f"verify: changed + missing files caught ({j.get('bad')}, {j.get('missing')})")

# 4. re-run is incremental: the MBTiles is kept, damage repaired
t0 = mb.stat().st_mtime_ns
rc, j, err = lmap("export", "create", str(usb), "--format", "all", "--bbox", BB, "--yes")
check(rc == 0 and mb.stat().st_mtime_ns == t0 and (j.get("phone") or {}).get("kept") == ["sat-au-30m.mbtiles", "area-new-south-wales-a1-sat.mbtiles", "area-queensland-a1-topo.mbtiles"],
      f"re-run: same area keeps the MBTiles ({(j.get('phone') or {}).get('kept')}; {err[-200:]})")
rc, j, err = lmap("export", "verify", str(usb))
check(rc == 0 and j.get("ok"), f"re-run: verify ok again (copy repaired) ({rc}, bad={j.get('bad')}, missing={j.get('missing')}; {err[-300:]})")

# 5. another area: never the old area's tiles under the same name (phone AND atak)
def bounds(p):
    with sqlite3.connect(p) as c:
        return c.execute("SELECT value FROM metadata WHERE name = 'bounds'").fetchone()[0]
b0 = bounds(mb)
rc, j, err = lmap("export", "create", str(usb), "--format", "phone,atak", "--bbox", "146.42,-19.58,146.48,-19.52", "--yes")
check(rc == 0 and bounds(mb) != b0, f"new area: phone MBTiles rebuilt ({b0} -> {bounds(mb)})")
check(bounds(out / "atak" / "imagery" / "sat-au-30m.mbtiles") == bounds(mb), "new area: atak imagery follows")
(out / "phone" / "my-own.mbtiles").write_text("user file"); (out / "phone" / "notes.sqlitedb").write_text("user file")
rc, j, err = lmap("export", "create", str(usb), "--format", "phone,atak", "--bbox", "146.80,-19.95,146.90,-19.85", "--yes")
check((out / "phone" / "my-own.mbtiles").is_file() and (out / "phone" / "notes.sqlitedb").is_file(),
      "user's own files in phone/ left alone")
check(rc == 0 and not mb.exists() and not (out / "atak" / "imagery" / "sat-au-30m.mbtiles").exists(),
      f"area with no imagery: the old area's tiles removed, not left as current ({(j.get('phone') or {}).get('removed')})")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
