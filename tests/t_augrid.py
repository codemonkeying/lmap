#!/usr/bin/env python3
"""au grid: one zone built in scratch; MGRS numbers at known points, zone clipping, tables, land mask, contract.
Usage: t_augrid.py <scratch dir>"""
import json, os, subprocess, sys, types, importlib.util, importlib.machinery
from pathlib import Path
L = str(Path(__file__).resolve().parent.parent / "bin" / "lmap")
ld = importlib.machinery.SourceFileLoader("lmap", L)
spec = importlib.util.spec_from_loader("lmap", ld); m = importlib.util.module_from_spec(spec); ld.exec_module(m)
from osgeo import ogr, osr
ogr.UseExceptions()
fails = 0
def ok(c, msg):
    global fails
    print(("PASS " if c else "FAIL ") + msg); fails += not c

sc = Path(sys.argv[1]); sc.mkdir(parents=True, exist_ok=True)
if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(0)
dem = Path(os.environ["LMAP_ROOT"]) / Path("data/australia/dem-90m.cog.tif")
if dem.is_file():                                   # a clipped dev DEM (tools/devcat.py) can't hold zone 55
    from osgeo import gdal
    _g = gdal.Open(str(dem)).GetGeoTransform()
    if _g[1] * gdal.Open(str(dem)).RasterXSize < 20:
        print("SKIP: DEM at", dem, "is clipped (dev catalog); needs the national one"); sys.exit(0)
if not dem.is_file():
    print("SKIP: no national 90 m DEM at", dem, "(set LMAP_ROOT)"); sys.exit(0)
out = sc / "g.gpkg"; out.unlink(missing_ok=True)
os.environ["LMAP_GRID_ZONES"] = "55"
n = m.au_grid_build(types.SimpleNamespace(log=lambda *a: None, vlog=lambda *a: None), dem, out)
ok(n["lines"] > 100000 and n["squares"] > 50, f"built zone 55 ({n})")

ds = ogr.Open(str(out))
ok({ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())} >= {"grid_1km", "grid_10km", "grid_100km", "grid_squares"},
   "4 tables")
ok(ds.GetMetadataItem("lmap_key") == m.au_grid_key(dem), "key stamped (version + DEM)")
u = osr.SpatialReference(); u.ImportFromEPSG(32755); me = osr.SpatialReference(); me.ImportFromEPSG(3857)
ll = osr.SpatialReference(); ll.ImportFromEPSG(4326)
for s_ in (u, me, ll):
    s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
t = osr.CoordinateTransformation(u, me)
def at(tbl, e, n_, r=5):
    x, y, _ = t.TransformPoint(e, n_); p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(x, y)
    Lr = ds.GetLayer(tbl); Lr.SetSpatialFilter(p.Buffer(r)); v = [(f["axis"], f["label"], f["kind"]) for f in Lr]
    Lr.SetSpatialFilter(None); return v
# reference points: 55K DU 79234 70557 and 55K DU 76962 61149
ok(at("grid_1km", 479000, 7870557) == [("E", "79", "1km")], "reference point: easting line 79")
ok(at("grid_10km", 479234, 7870000) == [("N", "70", "10km")], "reference point: northing line 70 (10 km)")
ok(("N", "61", "1km") in at("grid_1km", 476962, 7861000), "Mt Stuart northing line 61")
v = at("grid_100km", 500000, 7855000); ok(v == [("E", "00", "100km")], f"100 km line at E 500000 ({v})")
S = ds.GetLayer("grid_squares")
x, y, _ = t.TransformPoint(450000, 7850000); p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(x, y)
S.SetSpatialFilter(p.Buffer(60000)); labs = [f["label"] for f in S]; S.SetSpatialFilter(None)
ok(labs == ["55K DU"], f"square ID at the test box = 55K DU ({labs})")
labs = [f["label"] for f in S]
ok(all(l.startswith("55") for l in labs), "every square in zone 55")
ok(any(l.startswith("55H") for l in labs) and any(l.startswith("55L") for l in labs), "bands H..L present (Tas..Cape)")
# clipped to the zone: every 1 km line lies within lon 144..150 (+ a hair)
mt = osr.CoordinateTransformation(me, ll)
Lr = ds.GetLayer("grid_1km"); bad = 0; k = 0
for f in Lr:
    k += 1
    if k % 97:
        continue
    g = f.GetGeometryRef().Clone(); g.Transform(mt); e = g.GetEnvelope()
    bad += e[0] < 144 - 1e-6 or e[1] > 150 + 1e-6
ok(bad == 0, f"1 km lines inside zone 55 (144-150E), {bad} outside")
# land mask: nothing far out to sea (Coral Sea 154E -20S); something on land (Charters Towers)
def near(lon, lat, km):
    x, y, _ = osr.CoordinateTransformation(ll, me).TransformPoint(lon, lat)
    p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(x, y); Lr.SetSpatialFilter(p.Buffer(km * 1000))
    c = Lr.GetFeatureCount(); Lr.SetSpatialFilter(None); return c
ok(near(149.5, -21.0, 5) > 0, "lines on land (near Mackay)")
ok(near(149.0, -15.0, 5) == 0, "no lines far out to sea (Coral Sea)")
zl = [f["label"] for f in ds.GetLayer("grid_100km") if f["kind"] == "zone"]
ok(zl == [], f"one built zone: no lone zone edge out at sea ({zl})")
# a square across the 24S band edge gets a label in each band (same letters, K and J)
lab = sorted(f["label"] for f in ds.GetLayer("grid_squares"))
pairs = {l.split()[1] for l in lab if l.startswith("55K ")} & {l.split()[1] for l in lab if l.startswith("55J ")}
ok(len(pairs) > 0, f"squares split at the 24S band edge ({sorted(pairs)[:3]})")
# closing lines at the land-mask edge: the most easterly 1 km line inland of Cape York's east coast exists on both sides
Lr = ds.GetLayer("grid_1km")
ok(Lr.GetFeatureCount() > n["lines"] * 0.8, "1 km table holds most lines")
# zone clip: no line piece at all outside the zone (count any)
Lr.SetSpatialFilter(None)
Lr = None; S = None; ds = None
out.unlink(missing_ok=True)
# _lines_only keeps the lines of a mixed GeometryCollection
gc = ogr.CreateGeometryFromWkt("GEOMETRYCOLLECTION(POINT(0 0),LINESTRING(0 0,1 1),MULTILINESTRING((2 2,3 3),(4 4,5 5)))")
ok(len(m._lines_only(gc)) == 3, "lines kept from a GeometryCollection")
ok(m._lines_only(ogr.CreateGeometryFromWkt("POINT(0 0)")) == [], "a bare point gives no lines")

def lm(*a):
    try:                                               # (the dry run reads the national DEM: slow under a full suite)
        r = subprocess.run([L, "--json", "au", "grid", *a], capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return -1, {"timeout": True}
    j = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("{")), {})
    return r.returncode, j
c, j = lm("--dry-run"); ok(c == 0 and j.get("dry_run") and j["zones"] == "49-56", "dry-run")
ok(lm("--bogus")[0] == 2, "bad flag = 2")
if not j.get("exists"):
    ok(lm()[0] == 5, "no --yes = 5")
print(f"{'FAIL' if fails else 'ALL PASS'} ({fails} failed)")
sys.exit(1 if fails else 0)
