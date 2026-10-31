#!/usr/bin/env python3
"""Staged photo overviews (eadee4a): a one-tile 3-band source with an internal mask, referenced from a VRT in
another directory, must yield overviews on all bands AND the mask, with the no-data area still transparent.
Real GDAL, tiny rasters, offline."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
sh = lambda *c: subprocess.run(c, check=True, capture_output=True)
src_d, tmpd = T / "src", T / "ck"; src_d.mkdir(); tmpd.mkdir()
a = src_d / "rgba.tif"                                  # 1024^2, left half no-data (alpha 0)
sh("gdal_create", "-of", "GTiff", "-outsize", "4096", "4096", "-bands", "4", "-burn", "120", "-burn", "90",
   "-burn", "60", "-burn", "255", "-a_srs", "EPSG:3857", "-a_ullr", "0", "4096", "4096", "0", str(a))
sh("python3", "-c", f"from osgeo import gdal; d = gdal.Open('{a}', 1); "
   f"d.GetRasterBand(4).WriteRaster(0, 0, 4096, 4096, bytes([0] * 2048 + [255] * 2048) * 4096); d = None")
m = src_d / "masked.tif"                                # 3 bands + internal mask (what a one-tile source gives)
sh("gdal_translate", "-q", "-b", "1", "-b", "2", "-b", "3", "-mask", "4", "--config", "GDAL_TIFF_INTERNAL_MASK",
   "YES", "-co", "TILED=YES", str(a), str(m))
v = src_d / "one.vrt"; subprocess.run(["gdalbuildvrt", "-q", v.name, m.name], cwd=src_d, check=True)     # relative path: masked.tif beside it
info = json.loads(subprocess.run(["gdalinfo", "-json", str(v)], capture_output=True, text=True).stdout)
check(len(info["bands"]) == 3 and "PER_DATASET" in info["bands"][0]["mask"]["flags"], "fixture: 3 bands + internal mask")
ctx = types.SimpleNamespace(log=lambda *_: None, vlog=lambda *_: None, scratch=lambda: tmpd, webp=True,
                            quality="standard", limits={"disk": 100})
try:
    sel = L._staged_overviews(ctx, v, tmpd, dict(os.environ), None)
except Exception as x:
    sel = None; check(False, f"_staged_overviews raised {type(x).__name__}: {x}")
if sel:
    i = json.loads(subprocess.run(["gdalinfo", "-json", str(sel)], capture_output=True, text=True).stdout)
    b = i.get("bands", [])
    check(len(b) == 3, f"selected VRT has 3 bands ({len(b)})")
    check(all(x.get("overviews") for x in b), "every band has overviews")
    cog = T / "out.cog.tif"                             # step 2 as build_cog runs it: COG reusing the staged ovr
    args, _ = L.cog_co(ctx, v)                          # the args build_cog derives from the source
    r = subprocess.run(["gdal_translate", "-q", "-of", "COG"] + args + [str(sel), str(cog)], capture_output=True, text=True)
    check(r.returncode == 0 and "lacks overviews" not in r.stderr, f"COG step clean ({r.stderr.strip()[:120]})")
    from osgeo import gdal
    gdal.UseExceptions(); d = gdal.Open(str(cog)); b1 = d.GetRasterBand(1); mb = b1.GetMaskBand()
    check(b1.GetMaskFlags() == gdal.GMF_PER_DATASET, "output keeps a per-dataset mask")
    check(mb.GetOverviewCount() == b1.GetOverviewCount() > 0,
          f"mask overviews {mb.GetOverviewCount()} == image overviews {b1.GetOverviewCount()}")
    o = mb.GetOverview(mb.GetOverviewCount() - 1) if mb.GetOverviewCount() else mb
    px = o.ReadRaster(0, o.YSize // 2, o.XSize, 1); w = o.XSize
    check(px[0] == 0 and px[w - 1] == 255, "no-data half stays transparent in the smallest overview")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
