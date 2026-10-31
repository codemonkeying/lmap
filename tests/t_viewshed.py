#!/usr/bin/env python3
"""terrain viewshed: synthetic Web Mercator DEM with a N-S ridge; known seen / hidden cells, OP count, CLI contract.
Offline; builds its own DEM under a fake data root."""
import importlib.machinery, os, importlib.util, json, math, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from osgeo import gdal, osr

ROOT = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(ROOT / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
gdal.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


d = Path(tempfile.mkdtemp(prefix="t_vs_", dir=sys.argv[1] if len(sys.argv) > 1 else None))
A = d / "data" / "australia"; A.mkdir(parents=True)
LAT0, LON0 = -19.3, 146.8
mx = lambda lon: 6378137.0 * math.radians(lon)
my = lambda lat: 6378137.0 * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
PX, W, H = 30.0, 1000, 1000                       # 30 km of mercator (~28 km of ground)
X0, Y0 = mx(LON0) - W * PX / 2, my(LAT0) + H * PX / 2
ds = gdal.GetDriverByName("GTiff").Create(str(A / "dem-3d-30m.cog.tif"), W, H, 1, gdal.GDT_Float32, ["TILED=YES"])
ds.SetGeoTransform((X0, PX, 0, Y0, 0, -PX)); sr = osr.SpatialReference(); sr.ImportFromEPSG(3857)
ds.SetProjection(sr.ExportToWkt()); b = ds.GetRasterBand(1); b.SetNoDataValue(-32768)
xs = X0 + (np.arange(W) + 0.5) * PX
ridge_x = mx(LON0) + 3000                          # ridge 3 km (mercator) east of the centre, 200 m high
z = 100 + 200 * np.exp(-((xs - ridge_x) / 150) ** 2)
b.WriteArray(np.tile(z, (H, 1)).astype("float32")); ds = None

lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(d)]


def cli(*a):
    c = subprocess.run(lm + list(a), capture_output=True, text=True); return c.returncode, c.stdout, c.stderr


obs = f"{LAT0:.6f},{LON0:.6f}"
rc, out, err = cli("terrain", "viewshed")
check(rc == 2 and json.loads(err)["error"], f"no --from/--points: usage error exit 2 ({rc})")
rc, out, err = cli("terrain", "viewshed", "--from", obs, "--radius-km", "500")
check(rc == 2, f"--radius-km out of range: exit 2 ({rc})")
rc, out, err = cli("terrain", "viewshed", "--from", obs, "--name", "bad name!")
check(rc == 2, f"bad --name: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "t1", "--radius-km", "8", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dry_run") and j["dem"] == "dem-3d-30m.cog.tif" and j["utm_epsg"] == 32755 and j["res_m"] == 28,
      f"dry-run: 30 m DEM, UTM 55S ({rc} {err[:80]})")
check(not (A / "viewsheds").exists(), "dry-run writes nothing")
rc, out, err = cli("terrain", "viewshed", "--from", obs, "--name", "t1", "--radius-km", "8")
check(rc == 5 and json.loads(err)["error"], f"refuses without --yes: exit 5 ({rc})")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "t1", "--radius-km", "8", "--yes")
j = json.loads(out) if out else {}
f = A / "viewsheds" / "t1.cog.tif"
check(rc == 0 and f.is_file() and j.get("visible_km2", 0) > 0, f"--yes writes viewsheds/t1.cog.tif ({rc} {err[-150:]})")


def val(path, lat, lon):
    ds = gdal.Open(str(path)); gt = ds.GetGeoTransform()
    x, y, _ = L._utm_tr(55, True).TransformPoint(lon, lat)
    c, r = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    if not (0 <= c < ds.RasterXSize and 0 <= r < ds.RasterYSize):
        return -1
    return int(ds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0])


lon_of = lambda dx: math.degrees((mx(LON0) + dx) / 6378137.0)     # mercator offset -> lon
check(val(f, LAT0, lon_of(-4000)) == 1, "flat ground west of the observer is seen")
check(val(f, LAT0, lon_of(3000)) == 1, "ridge crest is seen")
check(val(f, LAT0, lon_of(5000)) == 0, "ground behind the ridge is hidden")
ds = gdal.Open(str(f))
check(ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG" and ds.GetRasterBand(1).GetNoDataValue() == 0,
      "COG, 0 = not seen (transparent)")
check(rc == 0, "res is ground metres: 30 m mercator at 19S = 28 m")
check(json.loads(ds.GetMetadataItem("LMAP_VIEWSHED"))["observers"][0]["height_m"] == 1.7, "observer kept in metadata")
ds = None
rc, out, err = cli("terrain", "viewshed", "--from", obs, "--name", "t1", "--radius-km", "8")
check(rc == 5, f"existing file: refuse overwrite without --yes ({rc})")

# two OPs either side of the ridge: crest seen by 2, each flank by 1
pf = d / "ops.txt"
pf.write_text(f"W, {LAT0:.6f}, {lon_of(-2000):.6f}\nE, {LAT0:.6f}, {lon_of(8000):.6f}, 5m\n")
rc, out, err = cli("--json", "terrain", "viewshed", "--points", str(pf), "--radius-km", "5", "--yes")
j = json.loads(out) if out else {}
g = A / "viewsheds" / "ops.cog.tif"
check(rc == 0 and g.is_file() and j.get("name") == "ops" and j["observers"][1]["height_m"] == 5.0,
      f"--points: name from the file, per-point height ({rc} {err[-150:]})")
check(val(g, LAT0, lon_of(3000)) == 2, "crest seen by both OPs (count 2)")
check(val(g, LAT0, lon_of(-1000)) == 1 and val(g, LAT0, lon_of(7000)) == 1, "each flank seen by one OP")
check(set(j.get("seen_by", {})) >= {"1", "2"}, "seen_by area per count")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--points", str(pf))
check(rc == 2, f"--from and --points together: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", "-19.3,160.0", "--name", "sea", "--dry-run")
check(rc == 4, f"point off the DEM: not found exit 4 ({rc})")
# review: 21 points refused; N/S offset OPs (paste offsets in both axes); circle half off the data -> next DEM
many = d / "many.txt"; many.write_text("".join(f"P{i}, {LAT0:.6f}, {LON0 + i * 0.001:.6f}\n" for i in range(21)))
rc, out, err = cli("terrain", "viewshed", "--points", str(many), "--dry-run")
check(rc == 2, f"21 observers refused (max 20) ({rc})")
lat_of = lambda dy: math.degrees(2 * math.atan(math.exp((my(LAT0) + dy) / 6378137.0)) - math.pi / 2)
pf2 = d / "ns.txt"
pf2.write_text(f"N, {lat_of(3000):.6f}, {lon_of(-2000):.6f}\nS, {lat_of(-3000):.6f}, {lon_of(-2500):.6f}\n")
rc, out, err = cli("--json", "terrain", "viewshed", "--points", str(pf2), "--radius-km", "6", "--yes")
g2 = A / "viewsheds" / "ns.cog.tif"
check(rc == 0 and val(g2, lat_of(3000), lon_of(-1000)) >= 1 and val(g2, lat_of(-3000), lon_of(-1500)) >= 1
      and val(g2, lat_of(0), lon_of(-2200)) == 2, f"N/S offset OPs land in the right rows (paste) ({rc} {err[-120:]})")
check(val(g2, lat_of(3000), lon_of(3600)) == 0, "just behind the ridge hidden")
j2 = json.loads(out) if out else {}
check(all(o_["visible_km2"] <= j2["visible_km2"] for o_ in j2.get("observers", [])), "per-observer km2 <= total")
# --angles: mils from the eye; flat ground ~level (slightly down: curvature), crest well up, hidden = nodata
rc, out, err = cli("--json", "terrain", "viewshed", "--points", str(pf), "--angles", "--dry-run")
check(rc == 2, f"--angles with --points: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "ta", "--radius-km", "8", "--angles", "--dry-run")
check(rc == 0 and json.loads(out).get("angle_out") and not (A / "viewsheds" / "ta-angle.cog.tif").exists(), "--angles dry-run writes nothing")
rc, out, err = cli("terrain", "viewshed", "--from", obs, "--name", "x-angle", "--dry-run")
check(rc == 2, f"--name ending -angle refused ({rc})")
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "ta", "--radius-km", "8", "--angles", "--yes")
j = json.loads(out) if out else {}
fa = A / "viewsheds" / "ta-angle.cog.tif"


def fval(path, lat, lon):
    ds = gdal.Open(str(path)); gt = ds.GetGeoTransform(); b_ = ds.GetRasterBand(1)
    x, y, _ = L._utm_tr(55, True).TransformPoint(lon, lat)
    v = float(b_.ReadAsArray(int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5]), 1, 1)[0, 0])
    return None if v == b_.GetNoDataValue() else v


check(rc == 0 and fa.is_file() and j.get("angle_mils") and j["angle_out"] == str(fa) and "_angle_stats" not in j,
      f"--angles writes ta-angle ({rc} {err[-150:]})")
fl, cr, hd = fval(fa, LAT0, lon_of(-4000)), fval(fa, LAT0, lon_of(3000)), fval(fa, LAT0, lon_of(5000))
# crest: 200 m up at ~2830 m ground -> atan(200/2830) = 4.04 deg = 72 mils
check(fl is not None and -3 < fl < 0.5, f"flat ground 4 km off: ~level, a little down ({fl})")
check(cr is not None and 65 < cr < 78, f"ridge crest ~72 mils up ({cr})")
check(hd is None, f"hidden ground has no angle ({hd})")
ds = gdal.Open(str(fa)); check(ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG", "angle file is a COG"); ds = None
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "ta", "--radius-km", "8", "--yes")
check(rc == 0 and not fa.exists(), "re-run without --angles removes the old angle file")
ds = gdal.Open(str(A / "dem-3d-30m.cog.tif"), gdal.GA_Update); hb_ = ds.GetRasterBand(1)
c_, r_ = int((mx(LON0) - X0) / PX), int((Y0 - my(LAT0)) / PX)
keep = hb_.ReadAsArray(c_ - 3, r_ - 3, 7, 7); hb_.WriteArray(np.full((7, 7), -32768, "float32"), c_ - 3, r_ - 3); ds = hb_ = None
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "tn", "--radius-km", "3", "--angles", "--yes")
check(rc == 4 and not (A / "viewsheds" / "tn-angle.cog.tif").exists(), f"observer on no-data: exit 4, no angle file ({rc})")
ds = gdal.Open(str(A / "dem-3d-30m.cog.tif"), gdal.GA_Update); ds.GetRasterBand(1).WriteArray(keep, c_ - 3, r_ - 3); ds = None
hole = A / "dem-3d-10m-3dview.cog.tif"              # 10 m 'LiDAR' covering only the west half: must not be used
ds = gdal.GetDriverByName("GTiff").Create(str(hole), 1500, 1500, 1, gdal.GDT_Float32, ["TILED=YES"])
ds.SetGeoTransform((X0, 10, 0, Y0, 0, -10)); ds.SetProjection(sr.ExportToWkt()); hb = ds.GetRasterBand(1)
hb.SetNoDataValue(-32768); arr = np.full((1500, 1500), -32768, "float32"); arr[:, :500] = 100; hb.WriteArray(arr); ds = None
rc, out, err = cli("--json", "terrain", "viewshed", "--from", obs, "--name", "t2", "--radius-km", "5", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dem") == "dem-3d-30m.cog.tif", f"LiDAR with no data over the circle is skipped ({j.get('dem')})")
hole.unlink()
check(not list(d.rglob("*.part*")) and not list(Path(os.environ.get("LMAP_TMP") or L.DEFAULT_TMP).glob("lmap-*/vs-t1.*")),
      "no temp files left")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
