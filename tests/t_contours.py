#!/usr/bin/env python3
"""contours_best: QLD LiDAR lines kept (10 m only, clipped to the area box), then LiDAR-DEM lines (contours_lidar)
where QLD has none, approx DEM lines only outside both; area print --contours/--interval choices + errors; real QGIS
print (10 m and 20 m) when QGIS is installed."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
from osgeo import ogr, osr

ROOT = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(ROOT / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
ogr.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


base = Path(sys.argv[1]) if len(sys.argv) > 1 else None
dr = Path(tempfile.mkdtemp(prefix="t_contours_", dir=base))
bbox = [146.70, -19.40, 146.80, -19.30]                    # ~10.5 x 11 km, zone 55
ll = osr.SpatialReference(); ll.ImportFromEPSG(4326); ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
mga = osr.SpatialReference(); mga.ImportFromEPSG(7855); mga.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)


def layer(path, name, srs):
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    lyr = ds.CreateLayer(name, srs, ogr.wkbLineString); lyr.CreateField(ogr.FieldDefn("elevation_m", ogr.OFTReal))
    return ds, lyr


def line(lyr, pts, z):
    f = ogr.Feature(lyr.GetLayerDefn()); g = ogr.Geometry(ogr.wkbLineString)
    for x, y in pts:
        g.AddPoint_2D(x, y)
    f.SetGeometry(g); f["elevation_m"] = z; lyr.CreateFeature(f)


# LiDAR (lon/lat like the QLD file): the west half only, every 5 m; one line runs far outside the box
ds, ly = layer(dr / "qld-contours.gpkg", "contour_lidar_5m", ll)
for i in range(40):
    lat = -19.39 + i * 0.002
    line(ly, [(146.701, lat), (146.745, lat)], 5.0 * i)
line(ly, [(146.72, -19.35), (146.95, -19.35)], 100.0)      # crosses out of the box to the east
line(ly, [(146.705, -19.3305), (146.74, -19.3305)], 209.99999)   # float noise: must still be the 210 line
ds = None
# DEM contours (MGA): full width, every 10 m
tr = osr.CoordinateTransformation(ll, mga)
ds, ly = layer(dr / "contours-dem.gpkg", "contours", mga)
for i in range(40):
    lat = -19.39 + i * 0.002 + 0.001
    a = tr.TransformPoint(146.702, lat)[:2]; b = tr.TransformPoint(146.798, lat)[:2]
    line(ly, [a, b], 10.0 * i)
ds = None
out = dr / "contours-best.gpkg"
st = L.contours_best(dr / "qld-contours.gpkg", dr / "contours-dem.gpkg", out, "EPSG:7855", bbox)
check(st["lidar_lines"] > 0 and st["approx_lines"] > 0, f"both sources present {st}")
check(30 < st["lidar_area_pct"] < 75, f"LiDAR outline about the west half ({st['lidar_area_pct']}%)")
ds = ogr.Open(str(out)); C = ds.GetLayerByName("contours"); A = ds.GetLayerByName("lidar_area")
cov = None
for f in A:
    cov = f.GetGeometryRef().Clone() if cov is None else cov.Union(f.GetGeometryRef())
lid = [f for f in C if f["src"] == "lidar"]; C.ResetReading(); apx = [f for f in C if f["src"] == "approx"]
check(all(isinstance(f["elevation_m"], int) and f["elevation_m"] % 10 == 0 for f in lid), "LiDAR: 10 m lines only, whole metres")
check(sum(1 for f in lid if f["elevation_m"] == 210) == 1, "209.99999 m stored as 210 (QGIS % 10 filter keeps it)")
east = tr.TransformPoint(146.80, -19.35)[0]
check(all(f.GetGeometryRef().GetEnvelope()[1] <= east + 50 for f in lid), "LiDAR clipped to the area box")
inside = sum(f.GetGeometryRef().Intersection(cov).Length() for f in apx)
check(inside < 1.0, f"no approx line inside the LiDAR outline ({inside:.2f} m)")
mid = tr.TransformPoint(146.77, -19.35)[0]
check(any(f.GetGeometryRef().GetEnvelope()[1] > mid for f in apx), "approx lines fill the east half")
ds = None
check([str(x) for x in L.wf_lines(ogr.CreateGeometryFromWkt("MULTILINESTRING((0 0,1 1),(2 2,3 3))"))]
      == ["LINESTRING (0 0,1 1)", "LINESTRING (2 2,3 3)"], "wf_lines splits multilines")

# no DEM line within the LiDAR next to the box edge (the closing used to shave 500 m off every edge)
ds = ogr.Open(str(out)); C = ds.GetLayerByName("contours")
west = tr.TransformPoint(146.70, -19.35)[0]
edge = [f for f in C if f["src"] == "approx" and f.GetGeometryRef().GetEnvelope()[0] < west + 400
        and f.GetGeometryRef().GetEnvelope()[2] < tr.TransformPoint(146.75, -19.312)[1]]
check(not edge, f"no approx lines along the LiDAR side of the box edge ({len(edge)})")
ds = None
# tiny (street) area ~600 m: outline must survive
tb = [146.710, -19.360, 146.716, -19.354]
st2 = L.contours_best(dr / "qld-contours.gpkg", dr / "contours-dem.gpkg", dr / "tiny.gpkg", "EPSG:7855", tb)
check(st2["lidar_area_pct"] > 80 and st2["approx_lines"] == 0, f"600 m area inside LiDAR: all LiDAR, no approx {st2}")
# a second layer first in the file + the right one picked by name
mx = dr / "multi.gpkg"; d2 = ogr.GetDriverByName("GPKG").CreateDataSource(str(mx))
d2.CreateLayer("aaa_other", ll, ogr.wkbPoint); d2 = None
src_ = ogr.Open(str(dr / "qld-contours.gpkg")); d2 = ogr.Open(str(mx), 1); d2.CopyLayer(src_.GetLayer(0), "contour_lidar_5m"); d2 = src_ = None
st3 = L.contours_best(mx, dr / "contours-dem.gpkg", dr / "m.gpkg", "EPSG:7855", bbox)
check(st3["lidar_lines"] == st["lidar_lines"], f"contour layer picked by name, not position ({st3['lidar_lines']})")
nc = dr / "nocrs.gpkg"; d2 = ogr.GetDriverByName("GPKG").CreateDataSource(str(nc)); d2.CreateLayer("contour_lidar_5m", None, ogr.wkbLineString); d2 = None
try:
    L.contours_best(nc, dr / "contours-dem.gpkg", dr / "n.gpkg", "EPSG:7855", bbox); ok = False
except L.CliError:
    ok = True
check(ok, "no CRS on the LiDAR layer: CliError, not a crash")

# area item: rebuilt when an input is newer
class _A: refresh = False
class _C:
    a = _A()
    def scratch(self): return dr
    def vlog(self, *x): pass
    def log(self, *x): pass
ad = dr / "area"; ad.mkdir()
for n_ in ("qld-contours.gpkg", "contours-dem.gpkg"):
    (ad / n_).write_bytes((dr / n_).read_bytes())
r1 = L.area_build_item(_C(), ad, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
r2 = L.area_build_item(_C(), ad, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
t_ = (ad / "contours-best.gpkg").stat().st_mtime + 5; os.utime(ad / "contours-dem.gpkg", (t_, t_))
r3 = L.area_build_item(_C(), ad, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
check("lidar_lines" in r1 and r2.get("unchanged") and "lidar_lines" in r3, f"built, kept, rebuilt after a newer input")

# --- contours from the LiDAR DEM (synthetic 3857 DEM: a 0..300 m ramp west->east, nodata -32768;
#     coverage = the east half of the box) + their place in contours-best (between QLD LiDAR and approx)
from osgeo import gdal
import numpy as np
gdal.UseExceptions()
wm = osr.SpatialReference(); wm.ImportFromEPSG(3857); wm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
t_wm = osr.CoordinateTransformation(ll, wm)
x0, y1 = t_wm.TransformPoint(146.69, -19.29)[:2]; x1, y0 = t_wm.TransformPoint(146.81, -19.41)[:2]
W_, H_ = int((x1 - x0) / 10), int((y1 - y0) / 10)
dt = dr / "ldem.tif"; r_ = gdal.GetDriverByName("GTiff").Create(str(dt), W_, H_, 1, gdal.GDT_Float32)
r_.SetGeoTransform((x0, 10, 0, y1, 0, -10)); r_.SetProjection(wm.ExportToWkt())
arr = np.tile(np.linspace(-5, 300, W_, dtype="float32"), (H_, 1)); arr[:, :3] = -32768
r_.GetRasterBand(1).WriteArray(arr); r_.GetRasterBand(1).SetNoDataValue(-32768); r_ = None
cp = dr / "cov.gpkg"; d2 = ogr.GetDriverByName("GPKG").CreateDataSource(str(cp)); cl_ = d2.CreateLayer("cov", wm, ogr.wkbPolygon)
ex0, ey0 = t_wm.TransformPoint(146.75, -19.42)[:2]; ex1, ey1 = t_wm.TransformPoint(146.82, -19.28)[:2]
cf = ogr.Feature(cl_.GetLayerDefn())
cf.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({ex0} {ey0},{ex1} {ey0},{ex1} {ey1},{ex0} {ey1},{ex0} {ey0}))"))
cl_.CreateFeature(cf); cf = cl_ = None; d2 = None
class _CC:
    class a: refresh = False
    def scratch(self): return dr
    def log(self, *x): pass
    def vlog(self, *x): pass
cl = dr / "contours-lidar.gpkg"
sl = L.contours_lidar(_CC(), dt, cp, cl, "EPSG:7855", bbox)
ds = ogr.Open(str(cl)); CL_ = ds.GetLayerByName("contours")
zs = sorted({f["elevation_m"] for f in CL_})
check(sl["lines"] > 5 and zs and all(z % 10 == 0 and z > 0 for z in zs), f"LiDAR-DEM lines every 10 m, none at 0 m ({sl}, {zs[:4]})")
mid_x = tr.TransformPoint(146.75, -19.35)[0]; CL_.ResetReading()
check(all(f.GetGeometryRef().GetEnvelope()[0] >= mid_x for f in CL_), "LiDAR-DEM lines only inside the coverage")
check(ds.GetLayerByName("area").GetFeatureCount() == 1, "LiDAR-DEM layer `area` written")
CL_ = None; ds = None
b3 = dr / "best3.gpkg"
s3 = L.contours_best(dr / "qld-contours.gpkg", dr / "contours-dem.gpkg", b3, "EPSG:7855", bbox, cl)
check(s3["lidar_lines"] == st["lidar_lines"] and s3["lidar_dem_lines"] > 0, f"3 sources: QLD kept, LiDAR-DEM added {s3}")
def apx_len(pth):
    d_ = ogr.Open(str(pth)); l_ = d_.GetLayerByName("contours"); t_ = 0.0
    for f_ in l_:
        if f_["src"] == "approx":
            t_ += f_.GetGeometryRef().Length()
    f_ = l_ = None; d_ = None; return t_
check(apx_len(b3) < 0.3 * apx_len(out) and s3["lidar_area_pct"] > 85,
      f"approx only where neither LiDAR is ({apx_len(b3):.0f} m vs {apx_len(out):.0f} m)")
ds = ogr.Open(str(b3)); C3 = ds.GetLayerByName("contours")
srcs = {f["src"] for f in C3}; C3.ResetReading()
check(srcs == {"lidar", "lidar-dem", "approx"} or srcs == {"lidar", "lidar-dem"}, f"src values {srcs}")
qa = None
ql = ogr.Open(str(out))
for f in ql.GetLayerByName("lidar_area"):
    qa = f.GetGeometryRef().Clone() if qa is None else qa.Union(f.GetGeometryRef())
f = None; ql = None
over = sum(f.GetGeometryRef().Intersection(qa).Length() for f in C3 if f["src"] == "lidar-dem"); C3.ResetReading()
check(over < 1.0, f"no LiDAR-DEM line where QLD has lines ({over:.2f} m)")
check(all(f["elevation_m"] % 10 == 0 for f in C3), "contours-best stored every 10 m (20 m = filter)")
C3 = None; ds = None
# coverage that misses the box: empty, not an error
cf2 = dr / "cov2.gpkg"; d2 = ogr.GetDriverByName("GPKG").CreateDataSource(str(cf2)); l2_ = d2.CreateLayer("cov", wm, ogr.wkbPolygon)
fx0, fy0 = t_wm.TransformPoint(147.5, -20.0)[:2]; fx1, fy1 = t_wm.TransformPoint(147.6, -19.9)[:2]
f2_ = ogr.Feature(l2_.GetLayerDefn())
f2_.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({fx0} {fy0},{fx1} {fy0},{fx1} {fy1},{fx0} {fy1},{fx0} {fy0}))"))
l2_.CreateFeature(f2_); f2_ = l2_ = None; d2 = None
se = L.contours_lidar(_CC(), dt, cf2, dr / "cl-empty.gpkg", "EPSG:7855", bbox)
check(se["lines"] == 0 and se["area_km2"] == 0, f"coverage outside the box: 0 lines, no error {se}")
ds = ogr.Open(str(cl)); CL_ = ds.GetLayerByName("contours"); ar_ = ds.GetLayerByName("area")
ag = None
for f in ar_:
    ag = f.GetGeometryRef().Clone()
out_ = sum(f.GetGeometryRef().Difference(ag.Buffer(1)).Length() for f in CL_)
check(out_ < 1.0, f"LiDAR-DEM lines clipped to their coverage ({out_:.1f} m outside)")
f = CL_ = ar_ = None; ds = None
s4 = L.contours_best(None, dr / "contours-dem.gpkg", dr / "best4.gpkg", "EPSG:7855", bbox, cl)
check(s4["lidar_lines"] == 0 and s4["lidar_dem_lines"] > 0 and s4["approx_lines"] > 0, f"no QLD file (other states) {s4}")

ad2 = dr / "area2"; ad2.mkdir()
ex_l = {"from": "x", "cov": str(cp)}
q1 = L.area_build_item(_C(), ad2, "contours-lidar", "contours-lidar", dt, ex_l, bbox, "EPSG:7855")
q2 = L.area_build_item(_C(), ad2, "contours-lidar", "contours-lidar", dt, ex_l, bbox, "EPSG:7855")
bb2 = [146.71, -19.40, 146.80, -19.30]
q3 = L.area_build_item(_C(), ad2, "contours-lidar", "contours-lidar", dt, ex_l, bb2, "EPSG:7855")
t_ = (ad2 / "contours-lidar.gpkg").stat().st_mtime + 5; os.utime(cp, (t_, t_))
q4 = L.area_build_item(_C(), ad2, "contours-lidar", "contours-lidar", dt, ex_l, bb2, "EPSG:7855")
check("lines" in q1 and q2.get("unchanged") and "lines" in q3 and "lines" in q4,
      f"contours-lidar: built, kept, rebuilt for a new box, rebuilt after newer coverage")
L.area_build_item(_C(), ad2, "contours-lidar", "contours-lidar", dt, ex_l, bbox, "EPSG:7855")
for n_ in ("qld-contours.gpkg", "contours-dem.gpkg"):
    (ad2 / n_).write_bytes((dr / n_).read_bytes())
b1 = L.area_build_item(_C(), ad2, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
b2 = L.area_build_item(_C(), ad2, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
(ad2 / "contours-lidar.gpkg").unlink()
b3 = L.area_build_item(_C(), ad2, "contours-best", "contours-best", None, {"from": "x"}, bbox, "EPSG:7855")
check(b1.get("lidar_dem_lines", 0) > 0 and b2.get("unchanged") and b3.get("lidar_dem_lines") == 0,
      f"contours-best rebuilt when a source disappears ({b1.get('lidar_dem_lines')}, {b3.get('lidar_dem_lines')})")

# area_plan: contours-lidar only where the 2 m DEM + coverage exist and overlap; not at overview
class _P:
    sources = {}
    a = type("a", (), {"sat_res": None, "sat_2m4": False})()
    def __init__(s, root): s.root = root
    def path(s, p): return s.root / p
pr = dr / "planroot"; (pr / "data" / "australia" / "lidar").mkdir(parents=True)
gdal.Translate(str(pr / "data" / "australia" / "dem-3d-2m-3dview.cog.tif"), str(dt))
(pr / "data" / "australia" / "lidar" / "lidar-coverage.gpkg").write_bytes(cp.read_bytes())
_orig = (L.best_raster, L.raster_ll_box)
L.best_raster = lambda ctx, sid: dt if sid == "dem-qld-30m" else None
try:
    ks = [k for k, *_ in L.area_plan(_P(pr), bbox, "local")]
    ko = [k for k, *_ in L.area_plan(_P(pr), bbox, "overview")]
    kf = [k for k, *_ in L.area_plan(_P(pr), [150.0, -30.0, 150.1, -29.9], "local")]
finally:
    L.best_raster = _orig[0]
check("contours-lidar" in ks and "contours-best" in ks, f"plan: contours-lidar + best where LiDAR DEM overlaps ({ks})")
check("contours-lidar" not in ko, "plan: no contours-lidar at overview level")
check("contours-lidar" not in kf and "contours-best" not in kf, f"plan: none far from the LiDAR DEM ({kf})")

# area print --contours (dry-run: no QGIS)
root = Path(tempfile.mkdtemp(prefix="t_contours_root_", dir=base)); a = root / "areas" / "t"; a.mkdir(parents=True)
(a / "area.json").write_text(json.dumps({"bbox": bbox, "crs": "EPSG:7855", "items": []})); (a / "t.qgz").write_bytes(b"x")


def cli(*args):
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root), "--json", "area", "print", "t",
                        "--dry-run", *args], capture_output=True, text=True, timeout=120)
    return c.returncode, c.stdout, c.stderr


rc, o, e = cli()
check(rc == 0 and json.loads(o)["contours"] == "none", f"best with no contour files -> none ({rc} {e[:100]})")
(a / "contours-dem.gpkg").write_bytes((dr / "contours-dem.gpkg").read_bytes())
rc, o, e = cli()
check(rc == 0 and json.loads(o)["contours"] == "dem", "best with only DEM contours -> dem")
rc, o, e = cli("--contours", "lidar")
check(rc == 4 and json.loads(e).get("error"), f"--contours lidar without the file: exit 4 + JSON ({rc})")
(a / "contours-best.gpkg").write_bytes(out.read_bytes())
rc, o, e = cli()
j = json.loads(o) if rc == 0 else {}
check(rc == 0 and j["contours"] == "best" and "APPROX" in j["contour_note"], "best: note names LiDAR + APPROX")
check(not list(a.glob("print-*")), "dry-run wrote no sheet")
rc, o, e = cli("--contours", "bogus")
check(rc == 2, f"bad --contours: exit 2 ({rc})")
rc, o, e = cli("--interval", "20")
check(rc == 0 and json.loads(o)["interval"] == 20, "--interval 20: plan says 20 (the sheet's CI line is measured from the drawn lines)")
rc, o, e = cli("--interval", "5")
check(rc == 2, f"--interval 5: exit 2 ({rc})")
rc, o, e = cli("--interval", "20", "--contours", "lidar")
check(rc == 4, f"--interval 20 --contours lidar (no LiDAR file): not-found 4 (14102f3: 20 m works on LiDAR) ({rc})")
rc, o, e = cli("--interval", "20", "--contours", "dem")
check(rc == 0 and json.loads(o)["interval"] == 20, f"--interval 20 with --contours dem: thinned to 20 m ({rc})")
# real print through QGIS (skipped without it): best sheet, and lidar asked of a project with no such layer
try:
    import qgis.core  # noqa: F401
    have_qgis = True
except ImportError:
    have_qgis = False
if have_qgis:
    (a / "qld-contours.gpkg").unlink(missing_ok=True); (a / "t.qgz").unlink()
    q = L.area_qgis(_C(), a, "t", "EPSG:7855", bbox, "local")
    check((a / "t.qgz").stat().st_size > 100, f"area project built ({q})")
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root), "--json", "area", "print", "t",
                        "--scale", "50000", "--paper", "A4", "--plain-pdf", "--dpi", "72", "--yes"],
                       capture_output=True, text=True, timeout=600, env={**os.environ, "LMAP_TMP": str(dr)})
    check(c.returncode == 0 and (a / "print-A4-50k.pdf").is_file(), f"best sheet printed ({c.returncode} {c.stderr[-200:]})")
    import zipfile
    qs = zipfile.ZipFile(a / "t.qgz").read(next(n_ for n_ in zipfile.ZipFile(a / "t.qgz").namelist() if n_.endswith(".qgs"))).decode()
    check("Contours 10m, index 50m" in qs and "Contours 20m, index 100m" in qs, "project: 10 m and 20 m views of contours-best")
    import xml.etree.ElementTree as ET   # layers are opened read-only while building; the saved project must not be
    mls = list(ET.fromstring(qs).iter("maplayer"))
    check(mls and all(m.get("readOnly", "0") == "0" for m in mls), "project layers stay editable for the user")
    check(all((m.findtext("srs/spatialrefsys/authid") or "") for m in mls), "every project layer keeps its CRS")
    check(not list(a.glob("*.gpkg-wal")) and not list(a.glob("*.gpkg-shm")), "building the project wrote no gpkg sidecars")
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root), "--json", "area", "print", "t",
                        "--scale", "50000", "--paper", "A4", "--plain-pdf", "--dpi", "72", "--interval", "20", "--label", "i20",
                        "--yes"], capture_output=True, text=True, timeout=600, env={**os.environ, "LMAP_TMP": str(dr)})
    check(c.returncode == 0 and (a / "print-i20.pdf").is_file(), f"20 m sheet printed ({c.returncode} {c.stderr[-200:]})")
    import shutil
    oq = Path(tempfile.mkdtemp(prefix="oldq_", dir=base)); zf = zipfile.ZipFile(a / "t.qgz")
    qn = next(n_ for n_ in zf.namelist() if n_.endswith(".qgs")); zf.extractall(oq); zf.close()
    q_ = (oq / qn).read_text()
    q_ = q_.replace("Contours 10m, index 50m", "Old best 10").replace("Contours 20m, index 100m", "Old best 20")
    (oq / qn).write_text(q_); shutil.copy(a / "t.qgz", oq / "keep.qgz")
    with zipfile.ZipFile(a / "t.qgz", "w") as z_:
        for n_ in os.listdir(oq):
            if n_ != "keep.qgz":
                z_.write(oq / n_, n_)
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root), "--json", "area", "print", "t",
                        "--scale", "50000", "--paper", "A4", "--plain-pdf", "--dpi", "72", "--interval", "20", "--label", "old20",
                        "--yes"], capture_output=True, text=True, timeout=600, env={**os.environ, "LMAP_TMP": str(dr)})
    check(c.returncode == 4 and not (a / "print-old20.pdf").exists(), f"old project (no 20 m view) + --interval 20: exit 4 ({c.returncode})")
    shutil.copy(oq / "keep.qgz", a / "t.qgz")
    (a / "qld-contours.gpkg").write_bytes((dr / "qld-contours.gpkg").read_bytes())
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root), "--json", "area", "print", "t",
                        "--scale", "50000", "--paper", "A4", "--plain-pdf", "--dpi", "72", "--contours", "lidar",
                        "--label", "nl", "--yes"], capture_output=True, text=True, timeout=600, env={**os.environ, "LMAP_TMP": str(dr)})
    check(c.returncode == 4 and not (a / "print-nl.pdf").exists(), f"--contours lidar, layer not in project: exit 4 ({c.returncode})")
else:
    print("SKIP QGIS print checks (no qgis)")
# NSW 5 m DEM lines (contours-nsw5.gpkg: contours + area) as the ONE provider: nothing else mixed in, the 30 m DEM
# only outside its data area (east half here)
ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(dr / "contours-nsw5.gpkg"))
ly = ds.CreateLayer("contours", mga, ogr.wkbLineString); ly.CreateField(ogr.FieldDefn("elevation_m", ogr.OFTInteger))
for i in range(40):
    lat = -19.39 + i * 0.002
    a_ = tr.TransformPoint(146.701, lat)[:2]; b_ = tr.TransformPoint(146.749, lat)[:2]
    line(ly, [a_, b_], 10 * i)
al = ds.CreateLayer("area", mga, ogr.wkbMultiPolygon); ring = ogr.Geometry(ogr.wkbLinearRing)
for x, y in ((146.70, -19.40), (146.75, -19.40), (146.75, -19.30), (146.70, -19.30), (146.70, -19.40)):
    ring.AddPoint_2D(*tr.TransformPoint(x, y)[:2])
pg = ogr.Geometry(ogr.wkbPolygon); pg.AddGeometry(ring); f_ = ogr.Feature(al.GetLayerDefn())
f_.SetGeometry(ogr.ForceToMultiPolygon(pg)); al.CreateFeature(f_); ds = None
st5 = L.contours_best(None, dr / "contours-dem.gpkg", dr / "b5.gpkg", "EPSG:7855", bbox, nsw5_p=dr / "contours-nsw5.gpkg")
_d5 = ogr.Open(str(dr / "b5.gpkg")); srcs = {f["src"] for f in _d5.GetLayer("contours")}; _d5 = None
check(srcs == {"nsw-5m", "approx"} and st5["nsw_5m_lines"] > 0, f"nsw-5m provider: only 5 m lines + DEM fill outside ({srcs})")
check(set(L.CONTOUR_PROVIDERS) >= {"auto", "nsw-5m", "nsw-lines", "lidar", "dem30"}
      and L.CONTOUR_PROVIDERS["nsw-5m"] == {"contours-nsw5.gpkg"}, "provider table: one source each")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
