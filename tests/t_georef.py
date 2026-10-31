#!/usr/bin/env python3
"""pdf_georef_fix on a real GeoPDF (GDAL ISO32000 encoding, one /GPTS, a "Grid lines" vector layer drawn 3 m off the
true km lines): it shifts the georeference so the drawn grid lands on whole km (error ~3 m -> < 0.5 m), keeps the
file byte length and xref, writes via a .part that is gone afterwards, and leaves a file with no grid layer untouched.
Usage: t_georef.py <scratch>"""
import importlib.machinery, importlib.util, re, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


from osgeo import gdal, ogr, osr
gdal.UseExceptions(); ogr.UseExceptions()
sr = osr.SpatialReference(); sr.ImportFromEPSG(28355)           # MGA zone 55
X0, Y1, OFF = 501230, 7870000, 3.0                  # off the central meridian: corner longitudes have decimals


def make(pdf, grid=True):
    ds = gdal.GetDriverByName("MEM").Create("", 1000, 1000, 3)
    ds.SetGeoTransform((X0, 10, 0, Y1, 0, -10)); ds.SetProjection(sr.ExportToWkt())
    opts = ["GEO_ENCODING=ISO32000", "COMPRESS=DEFLATE"]
    if grid:
        g = tmp / "grid.gpkg"; g.unlink(missing_ok=True)
        v = ogr.GetDriverByName("GPKG").CreateDataSource(str(g)); lyr = v.CreateLayer("Grid lines", sr, ogr.wkbLineString)
        for k in range(1, 10):                          # 9 eastings + 9 northings, all OFF metres east/north of true
            e = (X0 // 1000 + k) * 1000 + OFF; nn = Y1 - 10000 + k * 1000 + OFF      # true km lines + OFF
            for a, b in (((e, Y1 - 10000), (e, Y1)), ((X0, nn), (X0 + 10000, nn))):
                ln = ogr.Geometry(ogr.wkbLineString); ln.AddPoint_2D(*a); ln.AddPoint_2D(*b)
                f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(ln); lyr.CreateFeature(f)
        v = None
        opts += [f"OGR_DATASOURCE={g}", "OGR_DISPLAY_LAYER_NAMES=Grid lines"]
    gdal.GetDriverByName("PDF").CreateCopy(str(pdf), ds, options=opts); ds = None


pdf = tmp / "sheet.pdf"; make(pdf)
b0 = pdf.read_bytes()
check(len(re.findall(rb"/GPTS\s*\[", b0)) == 1, "test GeoPDF has one /GPTS")
r = L.pdf_georef_fix(pdf)
b1 = pdf.read_bytes()
check(r is not None and 2.5 < r[0] < 3.5, f"grid measured ~{OFF} m off before ({r})")
check(r is not None and r[1] < 0.5, f"after the fix the drawn grid is on whole km ({r and r[1]} m)")
check(len(b1) == len(b0) and b1 != b0, "same byte length, georeference changed")
check(b1.replace(re.search(rb"/GPTS\s*\[[^\]]*\]", b1)[0], b"") == b0.replace(re.search(rb"/GPTS\s*\[[^\]]*\]", b0)[0], b""),
      "only /GPTS changed (xref offsets still valid)")
check(not Path(str(pdf) + ".part").exists(), "no .part left")
ds = gdal.Open(str(pdf)); check(ds is not None and ds.GetGeoTransform()[1] > 0, "fixed PDF still opens as a georeferenced raster"); ds = None
r2 = L.pdf_georef_fix(pdf)
check(r2 is not None and r2[0] < 0.5, f"a second pass finds nothing to fix ({r2})")

one = tmp / "one.pdf"                                  # A6-sized: a single km line each way -> shift-only fix
ds = gdal.GetDriverByName("MEM").Create("", 150, 150, 3)
ds.SetGeoTransform((X0 + 200, 10, 0, Y1 - 200, 0, -10)); ds.SetProjection(sr.ExportToWkt())
g1 = tmp / "g1.gpkg"; v = ogr.GetDriverByName("GPKG").CreateDataSource(str(g1)); lyr = v.CreateLayer("Grid lines", sr, ogr.wkbLineString)
e1 = (X0 // 1000 + 1) * 1000 + 0.7; n1 = Y1 - 1000 + 0.7
for a, b in (((e1, Y1 - 1700), (e1, Y1 - 200)), ((X0 + 200, n1), (X0 + 1700, n1))):
    ln = ogr.Geometry(ogr.wkbLineString); ln.AddPoint_2D(*a); ln.AddPoint_2D(*b)
    f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(ln); lyr.CreateFeature(f)
v = None
gdal.GetDriverByName("PDF").CreateCopy(str(one), ds, options=["GEO_ENCODING=ISO32000", "COMPRESS=DEFLATE",
                                                              f"OGR_DATASOURCE={g1}", "OGR_DISPLAY_LAYER_NAMES=Grid lines"]); ds = None
r1 = L.pdf_georef_fix(one)
check(r1 is not None and 0.6 < r1[0] < 0.8 and r1[1] < 0.1, f"one line each way (small sheet): shifted onto the km ({r1})")

far = tmp / "far.pdf"; OFF0 = OFF                     # grid drawn 600 m off: must NOT snap to the wrong km
OFF = 600.0; make(far); OFF = OFF0; f0 = far.read_bytes()
rf = L.pdf_georef_fix(far)
check(rf is not None and rf[1] is None and far.read_bytes() == f0, f"a grid ~400 m off: flagged, file untouched, no wrong-km snap ({rf})")

plain = tmp / "plain.pdf"; make(plain, grid=False); p0 = plain.read_bytes()
check(L.pdf_georef_fix(plain) is None and plain.read_bytes() == p0, "no grid layer: None, file untouched")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
