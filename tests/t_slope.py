#!/usr/bin/env python3
"""terrain slope: synthetic Web Mercator DEMs with a known slope (checks the cos(lat) correction, the 5x5 LiDAR
average, nodata -> 255), plus the CLI contract (dry-run, refuse without --yes, unchanged on re-run). Offline."""
import json, math, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from osgeo import gdal, osr

ROOT = Path(__file__).resolve().parent.parent
gdal.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


d = Path(tempfile.mkdtemp(prefix="t_slope_", dir=sys.argv[1] if len(sys.argv) > 1 else None))
A = d / "data" / "australia"; A.mkdir(parents=True)
LAT = -19.3
Y0 = 6378137.0 * math.log(math.tan(math.pi / 4 + math.radians(LAT) / 2))   # mercator northing of LAT
X0 = 16_340_000.0


def dem(name, px, w, h, deg, hole=None):
    """Plane rising east at `deg` degrees in TRUE metres (mercator px shrinks by cos(lat) on the ground)."""
    ds = gdal.GetDriverByName("GTiff").Create(str(A / name), w, h, 1, gdal.GDT_Float32, ["TILED=YES"])
    ds.SetGeoTransform((X0, px, 0, Y0 + h * px / 2, 0, -px))
    sr = osr.SpatialReference(); sr.ImportFromEPSG(3857); ds.SetProjection(sr.ExportToWkt())
    b = ds.GetRasterBand(1); b.SetNoDataValue(-32768)
    ys = Y0 + h * px / 2 - (np.arange(h) + 0.5) * px
    k = np.cos(np.arctan(np.sinh(ys / 6378137.0)))[:, None]              # ground m per mercator m, per row
    z = 100 + math.tan(math.radians(deg)) * (np.arange(w) * px)[None, :] * k
    if hole:
        z[hole] = -32768
    b.WriteArray(z.astype("float32")); ds = None


dem("dem-3d-30m.cog.tif", 30.0, 400, 301, 12, hole=(slice(100, 140), slice(200, 260)))   # 301: short last strip
dem("dem-3d-2m-3dview.cog.tif", 2.0, 503, 503, 30, hole=(slice(200, 300), slice(200, 300)))  # 503: not / 5

lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(d)]


ENV = dict(os.environ, LMAP_SLOPE_PX="6400")      # 16-row strips: seams + halos + a short last strip


def cli(*a):
    c = subprocess.run(lm + list(a), capture_output=True, text=True, env=ENV); return c.returncode, c.stdout, c.stderr


rc, out, err = cli("--json", "terrain", "slope", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dry_run") and [l["out"] for l in j["layers"]] == ["slope-30m.cog.tif", "slope-10m-lidar.cog.tif"]
      and [l["res_m"] for l in j["layers"]] == [30.0, 10.0], f"dry-run lists both layers at 30 m / 10 m ({rc} {err[:80]})")
check(not list(A.glob("slope-*")), "dry-run writes nothing")
rc, out, err = cli("terrain", "slope")
check(rc == 5 and json.loads(err).get("error"), f"refuses without --yes: exit 5 + JSON error ({rc})")
rc, out, err = cli("terrain", "slope", "--bogus")
check(rc == 2, f"unknown flag: usage error exit 2 ({rc})")

rc, out, err = cli("--json", "terrain", "slope", "--yes")
j = json.loads(out) if out else {}
check(rc == 0 and all(l.get("built") for l in j.get("layers", [])), f"--yes builds both ({rc} {err[-200:]})")


def arr(n):
    ds = gdal.Open(str(A / n)); b = ds.GetRasterBand(1)
    return ds, b, b.ReadAsArray()


ds, b, s = arr("slope-30m.cog.tif")
check(b.DataType == gdal.GDT_Byte and b.GetNoDataValue() == 255, "30 m: Byte, nodata 255")
check(ds.RasterXSize == 400 and ds.RasterYSize == 301, "30 m: same size as the DEM")
inner = s[5:-5, 5:-5]
inner = inner[inner != 255]
check(np.all(np.abs(inner.astype(int) - 12) <= 1), f"30 m: 12 deg plane reads 12 across 19 strip seams {np.unique(inner)}")
check(np.all(s[:, 0] == 12) and np.all(s[-1, :] == 12) and np.all(s[0, :] == 12), "raster edges + last row have slope")
check(np.all(s[99, 200:260] == 12) and np.all(s[140, 200:260] == 12) and np.all(s[100:140, 199] == 12),
      "cells next to a void keep their slope (no lost ring)")
check(np.all(s[105:135, 205:255] == 255), "nodata hole stays nodata (255)")
check(ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG", "30 m: is a COG")
check((ds.GetMetadataItem("LMAP_SLOPE_FROM") or "").startswith("dem-3d-30m.cog.tif "), "records the DEM it came from")
ds = None
ds, b, s = arr("slope-10m-lidar.cog.tif")
check(ds.RasterXSize == 100 and abs(ds.GetGeoTransform()[1] - 10.0) < 1e-9, "LiDAR: 2 m (503 px) averaged 5x5 to 10 m")
inner = s[3:-3, 3:-3]; v = inner[inner != 255]
check(np.all(np.abs(v.astype(int) - 30) <= 1), f"LiDAR: 30 deg plane reads 30, no false cliff at the hole {np.unique(v)}")
check(np.all(s[41:59, 41:59] == 255) and s[39, 50] != 255, "LiDAR hole = 255, its edge still has slope")
ds = None

rc, out, err = cli("--json", "terrain", "slope", "--yes")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("unchanged"), f"re-run: unchanged, nothing rebuilt ({rc})")
rc, out, err = cli("--json", "terrain", "slope", "--refresh", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and all(l["build"] for l in j.get("layers", [])), "--refresh --dry-run: both would rebuild")
dem("dem-3d-2m-3dview.cog.tif", 2.0, 503, 503, 25)              # DEM rebuilt -> slope is stale
os.utime(A / "dem-3d-2m-3dview.cog.tif", (1, 1))                 # even with an OLD mtime: the stamp differs
rc, out, err = cli("--json", "terrain", "slope", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and [l["build"] for l in j.get("layers", [])] == [False, True], "a changed DEM makes only its slope stale")
m = (A / "slope-30m.cog.tif").stat().st_mtime
rc, out, err = cli("--json", "terrain", "slope", "--refresh", "--yes")
check(rc == 0 and (A / "slope-30m.cog.tif").stat().st_mtime > m, "--refresh rebuilds")
check(not list(d.rglob("*.part.tif")) and not list(d.rglob("*.raw.tif")), "no temp files left")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
