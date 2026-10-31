#!/usr/bin/env python3
"""Height products keep real heights.
1. source scan: no cubicspline anywhere; every DEM gdalwarp uses DEM_RESAMPLE (cubic).
2. real warp: a synthetic summit resampled to half the cell size keeps its peak height exactly with DEM_RESAMPLE;
   cubicspline loses height on the same grid (documents why).
3. contours / dem3d carry the resample kernel in their signature code -> rebuild once."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import importlib.machinery, importlib.util, json, re, subprocess, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

src = lmap_source()
check('"cubicspline"' not in src, "no cubicspline in lmap")
check(L.DEM_RESAMPLE == "cubic", "DEM_RESAMPLE is cubic")
check(len(re.findall(r'"-r", DEM_RESAMPLE', src)) == 7 and "smooth_grid(json.loads" in src, "all DEM warps use DEM_RESAMPLE")

T = Path(tempfile.mkdtemp(prefix="lmap-trs-"))
try:
    from osgeo import gdal
    gdal.UseExceptions()
    n = 21; ds = gdal.GetDriverByName("GTiff").Create(str(T / "dem.tif"), n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([500000, 30, 0, 7800000, 0, -30]); ds.SetProjection("EPSG:32755")
    import math
    rows = [[100 + 400 * math.exp(-((x - 10) ** 2 + (y - 10) ** 2) / 8.0) for x in range(n)] for y in range(n)]
    b = ds.GetRasterBand(1); b.SetNoDataValue(-32768)
    b.WriteRaster(0, 0, n, n, __import__("struct").pack(f"{n*n}f", *[v for r in rows for v in r])); ds = None
    top = max(max(r) for r in rows)
    info = json.loads(subprocess.run(["gdalinfo", "-json", str(T / "dem.tif")], capture_output=True, text=True).stdout)
    te, h = L.smooth_grid(info)
    def peak(k, grid=None):
        o = T / f"{k}.tif"
        # area contours: lmap's own smooth_grid (half cell, shifted); 3D: a third of the cell
        g = ["-te", *te, "-tr", h, h] if grid is None else ["-tr", grid, grid]
        subprocess.run(["gdalwarp", "-q", "-overwrite", "-r", k, *g, "-srcnodata", "-32768",
                        "-dstnodata", "-32768", str(T / "dem.tif"), str(o)], check=True)
        return json.loads(subprocess.run(["gdalinfo", "-json", "-mm", str(o)], capture_output=True, text=True,
                                         check=True).stdout)["bands"][0]["computedMax"]
    pc, ps = peak(L.DEM_RESAMPLE), peak("cubicspline")
    print(f"     summit {top:.2f}  cubic {pc:.2f}  cubicspline {ps:.2f}")
    check(abs(pc - top) < 0.01, "contour smoothing grid (smooth_grid) + cubic keeps the summit height")
    check(abs(peak(L.DEM_RESAMPLE, "10") - top) < 0.01, "3D grid (a third of the cell) + cubic keeps it")
    check(ps < top - 1.0, "cubicspline would lower it (why it's banned)")
finally:
    subprocess.run(["rm", "-rf", str(T)])

check(L._item_code("contours") == "cubic/grid2" and L._item_code("dem3d") == "cubic", "contours/dem3d signatures carry it")
bb = (146.0, -19.1, 146.1, -19.0)
for k in ("contours", "dem3d"):
    check(f'"code": "{L._item_code(k)}"' in L.item_sig(Path("/nonexist"), k, k, "", {}, bb, "EPSG:3857")[2],
          f"{k} signature carries its resampling")
print("FAILED: " + "; ".join(fails) if fails else "OK")
raise SystemExit(1 if fails else 0)
