#!/usr/bin/env python3
"""NSW contour batches (99a): where two capture batches overlap, only the most accurate is kept; a lone older batch
elsewhere stays; a line far off the DEM is dropped. Synthetic data, offline. Usage: t_nswdedupe.py <tmpdir>"""
import importlib.machinery, importlib.util, sys, tempfile
from pathlib import Path
from osgeo import gdal, ogr, osr
import numpy as np

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
gdal.UseExceptions(); ogr.UseExceptions()
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
ll = osr.SpatialReference(); ll.ImportFromEPSG(4326)
dem = T / "dem.tif"                                    # flat ground at 1000 m over 150.0-150.2, -33.7..-33.5
R = gdal.GetDriverByName("GTiff").Create(str(dem), 200, 200, 1, gdal.GDT_Float32)
R.SetGeoTransform((150.0, 0.001, 0, -33.5, 0, -0.001)); R.SetProjection(ll.ExportToWkt())
_a = np.full((200, 200), 1000.0, "float32"); _a[:, 170:] = 700.0   # a 300 m cliff at 150.17
R.GetRasterBand(1).WriteArray(_a); R.GetRasterBand(1).SetNoDataValue(-9999); R = None

g = T / "nsw-contours.gpkg"
ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(g)); Ly = ds.CreateLayer("contours", ll, ogr.wkbLineString)
for n_, t_ in (("objectid", ogr.OFTInteger), ("elevation_m", ogr.OFTReal), ("capturesourcecode", ogr.OFTInteger),
               ("verticalaccuracy", ogr.OFTReal)):
    Ly.CreateField(ogr.FieldDefn(n_, t_))
def line(oid, x0, y, z, code, vacc, n=11):
    f = ogr.Feature(Ly.GetLayerDefn()); ls = ogr.Geometry(ogr.wkbLineString)
    for k in range(n):
        ls.AddPoint_2D(x0 + k * 0.0003, y)             # ~280 m long, inside one 500 m cell
    f.SetGeometry(ls); f["objectid"] = oid; f["elevation_m"] = z; f["capturesourcecode"] = code
    if vacc is not None:
        f["verticalaccuracy"] = vacc
    Ly.CreateFeature(f)
# overlap cell: old 1:25k scan (+-5 m) and the 2 m programme (+-1 m) both draw 1000/1010 m
line(1, 150.0505, -33.5609, 1000, 1, 5); line(2, 150.0505, -33.5615, 1010, 1, 5)
line(3, 150.0505, -33.5608, 1000, 14, 1); line(4, 150.0505, -33.5618, 1010, 14, 1)
# overlap cell 2, accuracy unset: falls back to the code rank (14 over 1)
line(5, 150.1505, -33.6012, 1000, 1, None); line(6, 150.1505, -33.6008, 1000, 14, None); line(10, 150.1505, -33.6018, 1010, 14, None)
# a lone old-scan line elsewhere: kept (no better batch there)
line(7, 150.0105, -33.6805, 1000, 1, 5)
# tile edge: an old line running from inside the better batch's patch out past its edge -> cut, the outside kept
line(9, 150.0505, -33.5612, 1005, 1, 5, n=31)
# junk: a "1680 m" line on 1000 m ground, in a cell of its own
line(8, 150.1005, -33.6505, 1680, 14, 1)
# cliff: an accurate 850 m line on the cliff face (150 m off the 30 m DEM at most points) -> kept
line(11, 150.1695, -33.5305, 850, 14, 1, n=4)
ds = None

r = L.nsw_contours_dedupe(g, dem)
print(r)
ds = ogr.Open(str(g)); fs = [(f["objectid"], f.GetGeometryRef().GetEnvelope()) for f in ds.GetLayerByName("contours")]; ds = None
left = {o for o, _ in fs if o != 9}; ex9 = [e for o, e in fs if o == 9]; print(ex9)
check(left == {3, 4, 6, 7, 10, 11} and len(ex9) == 1 and ex9[0][0] > 150.0530 and ex9[0][1] > 150.0590, f"kept the +-1 m batch where both overlap, the lone old line, the old line cut at the patch edge (outside kept), the cliff line, not the junk (left {sorted(left)})")
check(r["dropped_overlap"] == 3 and r["cut_at_better"] == 1 and r["dropped_dem"] == 1, "counts: 3 overlap, 1 cut, 1 off-DEM")
check(L.nc_acc(14, None) < L.nc_acc(1, None) < L.nc_acc(5, None) and L.nc_acc(1, 1) == 1.0, "accuracy: field first, code rank fallback")
check(L.nsw_stated_vacc(g, (150.0, -33.7, 150.2, -33.5)) == [1.0, 5.0], "sheet note: NSW stated accuracy range read from the lines")

# a missing 2 m tile inside others stays open (old lines kept there); a junk accurate line makes no
# footprint; MultiLineString parts are cut separately (no fake segment across the gap); NaN nodata is a void, not junk
dem2 = T / "dem2.tif"
R = gdal.GetDriverByName("GTiff").Create(str(dem2), 200, 200, 1, gdal.GDT_Float32)
R.SetGeoTransform((150.0, 0.001, 0, -33.5, 0, -0.001)); R.SetProjection(ll.ExportToWkt())
_a = np.full((200, 200), 1000.0, "float32"); _a[:, 140:] = np.nan
R.GetRasterBand(1).WriteArray(_a); R.GetRasterBand(1).SetNoDataValue(float("nan")); R = None
g2 = T / "nsw2.gpkg"
ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(g2)); Ly = ds.CreateLayer("contours", ll, ogr.wkbLineString)
for n_, t_ in (("objectid", ogr.OFTInteger), ("elevation_m", ogr.OFTReal), ("capturesourcecode", ogr.OFTInteger),
               ("verticalaccuracy", ogr.OFTReal)):
    Ly.CreateField(ogr.FieldDefn(n_, t_))
def seg(oid, xa, xb, y, z, code, vacc, parts=None):
    f = ogr.Feature(Ly.GetLayerDefn())
    def ls_(a_, b_):
        ls = ogr.Geometry(ogr.wkbLineString); n = max(2, int((b_ - a_) / 0.0003) + 1)
        for k in range(n):
            ls.AddPoint_2D(a_ + (b_ - a_) * k / (n - 1), y)
        return ls
    if parts:
        g_ = ogr.Geometry(ogr.wkbMultiLineString); [g_.AddGeometry(ls_(a_, b_)) for a_, b_ in parts]
    else:
        g_ = ls_(xa, xb)
    f.SetGeometry(g_); f["objectid"] = oid; f["elevation_m"] = z; f["capturesourcecode"] = code; f["verticalaccuracy"] = vacc
    Ly.CreateFeature(f)
oid = 100                                              # 2 m-programme tiles around a ~4 km2 hole (150.04-150.06, -33.65..-33.63)
for k in range(21):
    y = -33.66 + k * 0.002
    for a_, b_ in (((150.02, 150.04), (150.06, 150.08)) if -33.65 < y < -33.63 else ((150.02, 150.08),)):
        seg(oid, a_, b_, y, 1000, 14, 1); oid += 1
seg(20, 150.035, 150.065, -33.641, 1000, 1, 5)         # old line across the hole
seg(30, 150.10, 150.13, -33.55, 1680, 14, 1)           # junk accurate line ...
seg(31, 150.10, 150.13, -33.5505, 1000, 1, 5)          # ... beside a good old line 55 m away
seg(41, 150.0995, 150.1035, -33.58, 1000, 14, 1)       # better line under part A of a 2-part old line
seg(40, 0, 0, -33.58, 1000, 1, 5, parts=[(150.100, 150.103), (150.115, 150.118)])
seg(50, 150.16, 150.19, -33.60, 1000, 14, 1)           # over a NaN void of the DEM
ds = None
r2 = L.nsw_contours_dedupe(g2, dem2); print(r2)
ds = ogr.Open(str(g2)); fs = [(f["objectid"], f.GetGeometryRef().GetEnvelope()) for f in ds.GetLayerByName("contours")]; ds = None
e20 = [e for o, e in fs if o == 20]; print(e20)
check(any(e[0] <= 150.045 and e[1] >= 150.055 for e in e20), "missing tile inside others: the old line across the hole is kept there")
check(30 not in {o for o, _ in fs} and [e for o, e in fs if o == 31] == [(150.10, 150.13, -33.5505, -33.5505)],
      "junk accurate line dropped and leaves the good old line beside it whole")
e40 = [e for o, e in fs if o == 40]; print(e40)
check(len(e40) == 1 and e40[0][0] >= 150.114 and e40[0][1] <= 150.119, "2-part line: covered part cut, other part kept, no segment across the gap")
check(50 in {o for o, _ in fs}, "line over a NaN DEM void kept (void is not junk)")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
