#!/usr/bin/env python3
"""area image: CLI contract + a small real render of townsville (needs the built area). Usage: t_image.py <scratch>"""
import json, os, subprocess, sys
from pathlib import Path
L = str(Path(__file__).resolve().parent.parent / "bin" / "lmap")
BB = "--bbox=146.79,-19.27,146.81,-19.25"
fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += not c
def lm(*a, env=None):
    r = subprocess.run([L, "--json", "area", "image", *a], capture_output=True, text=True, timeout=280,
                       env={**os.environ, **(env or {})})
    j = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("{")), {})
    return r.returncode, j

if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(0)
# skip decided BEFORE the tested command, from an independent listing: a broken area lookup must FAIL, not skip
r = subprocess.run([L, "--json", "area", "list"], capture_output=True, text=True, timeout=120)
areas = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("[")), None)
if r.returncode != 0 or not isinstance(areas, list):
    print(f"FAIL area list (rc {r.returncode}): {r.stderr[-300:]}"); sys.exit(1)
if not any(a.get("name") == "townsville" for a in areas):
    print("SKIP needs a built 'townsville' area (LMAP_ROOT)"); sys.exit(0)
c, j = lm("townsville", BB, "--res", "2", "--dry-run")
ok(c == 0 and j.get("dry_run") and j["m_per_px"] == 2.0, "dry-run plan")
ok(abs(j["pixels"][0] - 2104 / 2) < 30, f"pixels ~1050 wide ({j.get('pixels')})")
c, j = lm("townsville", "--scale", "25000", "--dpi", "150", "--dry-run")
ok(c == 0 and abs(j["m_per_px"] - 4.233) < 0.01 and j["styled_as"] == "1:25000 at 150 dpi", "scale+dpi -> 4.23 m/px")
ok(lm("townsville", BB)[0] == 5, "no --yes = 5")
c_, j_ = lm("townsville", "--interval", "20", "--contours", "dem", "--dry-run")
ok(c_ == 0 and j_.get("interval") == 20, "interval 20 + dem: thinned to 20 m (14102f3)")
ok(lm("townsville", "--interval", "5", "--dry-run")[0] == 2, "bad --interval: usage 2")
ok(lm("nosuch-area", "--interval", "5")[0] == 2, "usage checked before not-found")
ok(lm("nosuch-area", "--dry-run")[0] == 4, "missing area = 4")
ok(lm("townsville", "--res", "0.01", "--dry-run")[0] == 2, "bad --res = 2")
ok(lm("townsville", "--bbox=1,2,3", "--dry-run")[0] == 2, "bad --bbox = 2")
ok(lm("townsville", "--bbox=10,10,11,11", "--dry-run")[0] == 2, "bbox outside area = 2")
ok(lm("townsville", "--label", "a/b", "--dry-run")[0] == 2, "bad label = 2")
ok(lm("townsville", "--res", "0.2", "--dry-run")[0] == 2, "over the pixel limit = 2")
ok(lm("townsville", "--res", "0.2", "--force", "--dry-run")[0] == 0, "--force passes the limit")
ok(lm("townsville", "--res", "1.5", "--jpeg", "--dry-run")[0] == 2, "--jpeg over 500 Mpx = 2")
ok(lm("townsville", "--res", "1.5", "--kmz", "--dry-run")[0] == 2, "--kmz over 500 Mpx = 2")
ok(lm("townsville", "--res", "1.5", "--dry-run")[0] == 0, "1.5 m GeoTIFF alone is fine")

# --codec jpeg plans ~6x smaller than the lossless default
_l = lm("townsville", BB, "--res", "2", "--dry-run")[1].get("est_mb") or 0
_j = lm("townsville", BB, "--res", "2", "--codec", "jpeg", "--dry-run")[1].get("est_mb") or 0
ok(_j > 0 and _l > 4 * _j, f"est_mb per codec (lzw {_l}, jpeg {_j})")
# old project without the 20 m view: exit 4 before drawing (simulated by a copied project with no best layer)
c, j = lm("townsville", BB, "--res", "2", "--contours", "lidar", "--label", "zz-noc", "--dry-run")
ok(c in (0, 4), "lidar contours plan")

# real render, small tiles so seams are exercised
c, j = lm("townsville", BB, "--res", "2", "--label", "zz-test", "--jpeg", "--kmz", "--yes", env={"LMAP_IMAGE_TILE": "300"})
ok(c == 0 and j.get("ok"), f"render exit 0 ({c} {j.get('error')})")
from osgeo import gdal
gdal.UseExceptions()
f = Path(j.get("file", "/nonexistent"))
import glob
ok(not glob.glob(str(f.parent / "*.part")), "no .part files left")
if f.is_file():
    ds = gdal.Open(str(f)); b = ds.GetRasterBand(1)
    ok(ds.RasterCount == 3 and ds.RasterXSize == j["pixels"][0], "3 bands, planned size")
    ok(b.GetOverviewCount() >= 1, "has overviews")
    ok(ds.GetMetadata("IMAGE_STRUCTURE").get("COMPRESSION", "") == "LZW", "LZW inside (default)")
    ok("7855" in ds.GetSpatialRef().GetAuthorityCode(None), "EPSG:7855")
    gt = ds.GetGeoTransform(); ok(abs(gt[1] - 2) < 1e-9, "2 m/px georef")
    import numpy as np
    a = np.stack([ds.GetRasterBand(i).ReadAsArray() for i in (1, 2, 3)])
    ok(a.std() > 20, f"not blank (std {a.std():.0f})")
    ok((a.min(axis=0) > 250).mean() < 0.2, "mostly drawn, not background")
    ds = None
    for x in [f, f.with_suffix(".jpg"), f.with_suffix(".wld"), f.with_suffix(".kmz"), Path(str(f.with_suffix(".jpg")) + ".aux.xml")]:
        if x.suffix != ".xml":
            ok(x.is_file() and x.stat().st_size > (40 if x.suffix == ".wld" else 1000), f"{x.name} written")
        x.unlink(missing_ok=True)
else:
    ok(False, "output file")
print(f"{'FAIL' if fails else 'ALL PASS'} ({fails} failed)")
sys.exit(1 if fails else 0)
