#!/usr/bin/env python3
"""terrain water: synthetic UTM valley (two slopes into a V draining south, a hollow) -> one main flow line down
the valley floor, wet spot in the hollow / valley, catchments, sea masked; picks finest DEM with data; CLI contract.
Needs GRASS."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr, osr

ROOT = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(ROOT / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


base = Path(sys.argv[1]) if len(sys.argv) > 1 else None
dr = Path(tempfile.mkdtemp(prefix="t_water_", dir=base)); A = dr / "data" / "australia"; A.mkdir(parents=True)
TMP = Path(tempfile.mkdtemp(prefix="t_water_tmp_", dir=base))
E0, N0, px, W, H = 400000.0, 7870000.0, 10.0, 600, 600
inv = L._utm_tr(55, True, inverse=True)
ll = lambda e, n: tuple(reversed(inv.TransformPoint(e, n)[:2]))
x = (np.arange(W) + 0.5) * px; y = (np.arange(H) + 0.5) * px          # y = metres south of N0
X, Y = np.meshgrid(x, y)
VX = 3000.0
z = 20 + 0.05 * np.abs(X - VX) + 0.02 * (6000 - Y)                     # V valley, floor falls to the south
z[Y > 5500] = -1.0                                                      # sea along the south edge
rng = np.random.default_rng(3); z += rng.uniform(-0.05, 0.05, z.shape) * (Y <= 5500)


def dem(path, arr, res, e0=E0, n0=N0):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((e0, res, 0, n0, 0, -res)); sr = osr.SpatialReference(); sr.ImportFromEPSG(32755)
    ds.SetProjection(sr.ExportToWkt()); ds.GetRasterBand(1).SetNoDataValue(-32768)
    ds.GetRasterBand(1).WriteArray(arr.astype("float32")); ds = None


dem(A / "dem-3d-30m.cog.tif", z, px)
# a "LiDAR" patch at 5 m covering only the NE corner: must NOT be picked for the centre
patch = np.full((200, 200), 50.0); dem(A / "dem-3d-5m-3dview.cog.tif", patch, 5.0, E0 + 5000, N0)
C = ll(E0 + VX, N0 - 3000)


def cli(*a):
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(dr)] + list(a),
                       capture_output=True, text=True, timeout=280, env={**os.environ, "LMAP_TMP": str(TMP)})
    return c.returncode, c.stdout, c.stderr


pos = f"--from={C[0]:.7f},{C[1]:.7f}"
rc, out, err = cli("terrain", "water")
check(rc == 2 and json.loads(err)["error"], f"no --from: exit 2 + JSON ({rc})")
rc, out, err = cli("terrain", "water", pos, "--radius-km", "0.1")
check(rc == 2, f"--radius-km 0.1: exit 2 ({rc})")
rc, out, err = cli("terrain", "water", pos, "--name", "bad/name")
check(rc == 2, f"bad --name: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "water", pos, "--radius-km", "2.9", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dry_run") and j["dem"] == "dem-3d-30m.cog.tif" and j["res_m"] == 10 and j["exists"] is False,
      f"dry-run: patch without data round the point skipped -> 30 m file at 10 m ({rc} {j.get('dem')} {err[:100]})")
check(not (A / "water").exists(), "dry-run wrote nothing")
rc, out, err = cli("--json", "terrain", "water", pos, "--radius-km", "2.9")
check(rc == 5, f"no --yes: exit 5 ({rc})")
rc, out, err = cli("--json", "terrain", "water", pos, "--radius-km", "2.9", "--name", "v", "--stream-km2", "0.02",
                   "--catchment-km2", "0.5", "--yes")
j = json.loads(out) if out else {}
check(rc == 0, f"run ok ({rc} {err[:300]})")
if rc == 0:
    check(len(out.strip().splitlines()) == 1, "--json: one document")
    g = A / "water" / "v.gpkg"; t = A / "water" / "v-wetness.cog.tif"
    check(g.is_file() and t.is_file(), "gpkg + wetness COG installed")
    ds = ogr.Open(str(g))
    S = ds.GetLayerByName("streams"); big = []; best = (0, None, None)
    for f in S:
        if f["upstream_km2"] > best[0]:
            best = (f["upstream_km2"], None, f["class"])
        if f["upstream_km2"] > 5:
            ge = f.GetGeometryRef().Clone(); big += [ge.GetPoint_2D(i) for i in range(ge.GetPointCount())]
    pts = np.array(big)
    off = np.abs(pts[:, 0] - (E0 + VX)); trunk = pts[:, 1] > N0 - 5000      # above the flat coastal strip
    rows = np.round(pts[trunk, 1] / 100)                     # every 100 m down the valley, the nearest big flow
    near = [off[trunk][rows == r_].min() for r_ in np.unique(rows)]
    check(len(near) > 30 and max(near) < 40,
          f"main flow (>5 km2) follows the valley floor all the way ({len(near)} x 100 m, worst {max(near):.0f} m off)")
    check(best[0] > 5 and best[2] in ("creek", "river"), f"valley line drains {best[0]:.1f} km2: class {best[2]}")
    sea_n = N0 - 5500
    check(all(f.GetGeometryRef().GetEnvelope()[2] > sea_n - 15 for f in ds.GetLayerByName("streams")),
          "no flow line runs into the sea")
    P = list(ds.GetLayerByName("wet_spots"))
    check(len(P) >= 1 and P[0]["rank"] == 1 and P[0]["mgrs"].startswith("55K"), f"wet spots ranked, MGRS ({len(P)})")
    if P:
        e1, n1 = P[0].GetGeometryRef().GetPoint_2D(0)
        check(abs(e1 - (E0 + VX)) < 60, f"top wet spot on the valley floor ({e1 - (E0 + VX):.0f} m off)")
    check(ds.GetLayerByName("catchments").GetFeatureCount() >= 1, "catchments written")
    check(j["streams"]["gully"] + j["streams"]["creek"] + j["streams"]["river"] == S.GetFeatureCount()
          and j["top_wet_spots"], "JSON counts match the file")
    ds = None
    r = gdal.Open(str(t)); b = r.GetRasterBand(1).ReadAsArray()
    check(r.GetRasterBand(1).GetNoDataValue() == 0 and b.max() <= 250 and (b > 0).mean() < 0.3,
          f"wetness COG: byte, 0 nodata, only the wet top shown ({(b > 0).mean():.2f})")
    gt = r.GetGeoTransform(); row = int((gt[3] - (N0 - 5800)) / gt[1])
    check(0 <= row < r.RasterYSize and b[row, :].max() == 0, "sea masked in the wetness")
    r = None
    rc5, _, _ = cli("terrain", "water", pos, "--radius-km", "2.9", "--name", "v")
    check(rc5 == 5, f"existing name without --yes: exit 5 ({rc5})")
    rc, out, err = cli("terrain", "water", pos, "--radius-km", "2.9", "--name", "v", "--yes")
    check(rc == 0 and "wet_spots" in out, "human output, overwrite with --yes")
    ds = ogr.Open(str(g))
    kx0, kx1 = E0 + VX - 2900, E0 + VX + 2900; ky0, ky1 = N0 - 3000 - 2900, N0 - 3000 + 2900
    e = [f.GetGeometryRef().GetEnvelope() for L_ in ("streams", "catchments") for f in ds.GetLayerByName(L_)]
    check(e and all(v[0] >= kx0 - 1 and v[1] <= kx1 + 1 and v[2] >= ky0 - 1 and v[3] <= ky1 + 1 for v in e),
          f"flow lines + catchments cut to --radius-km ({len(e)})")
    check(all(f["upstream_km2"] >= 0 and f["class"] == L.wf_classify(f["upstream_km2"]) for f in ds.GetLayerByName("streams")),
          "stream class matches upstream_km2")
    sp = list(ds.GetLayerByName("wet_spots"))
    check(all(f["wet_m2"] > 0 and f["upstream_km2"] > 0 and kx0 <= f.GetGeometryRef().GetX() <= kx1 for f in sp),
          "wet spot fields set, inside the box")
    check(ds.GetLayerByName("wet_spots").GetLayerDefn().GetFieldDefn(3).GetType() == ogr.OFTInteger64, "wet_m2 is Integer64")
    ds = None
    rc, out, err = cli("--json", "terrain", "water", pos, "--radius-km", "2.9", "--name", "v", "--dry-run")
    check(rc == 0 and json.loads(out)["exists"] is True, "dry-run: exists true after a run")
check([L.wf_classify(v) for v in (0.1, 2, 49.9, 50)] == ["gully", "creek", "creek", "river"], "class thresholds")
for args, what in ((["--wet-spots", "-1"], "--wet-spots -1"), (["--wet-spots", "5000"], "--wet-spots 5000"),
                   (["--catchment-km2", "0"], "--catchment-km2 0"), (["--catchment-km2", "0.01", "--stream-km2", "0.05"],
                   "--catchment-km2 below --stream-km2"), (["--res", "3"], "--res finer than half the DEM"),
                   (["--res", "100", "--stream-km2", "0.01"], "stream threshold < 5 cells"),
                   (["--name", ".."], "--name .."), (["--name", "-x"], "--name -x"), (["--name", "a-wetness"], "--name *-wetness")):
    rc, out, err = cli("terrain", "water", pos, "--radius-km", "2.9", *args, "--dry-run")
    try:
        je = json.loads(err).get("error")
    except ValueError:
        je = None
    check(rc == 2 and je, f"{what}: exit 2 + JSON error ({rc} {err[:80]})")
rc, out, err = cli("terrain", "water", pos, "--radius-km", "2.9")
check(rc == 5 and json.loads(err).get("error"), "exit 5 with a JSON error")
os.environ["LMAP_WF_MAX_PX"] = "10000"
rc, out, err = cli("terrain", "water", pos, "--radius-km", "2.9", "--res", "10", "--dry-run")
check(rc == 2 and "cells" in err, f"too many cells for a fixed --res: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "water", pos, "--radius-km", "2.9", "--dry-run")
j = json.loads(out) if rc == 0 else {}
check(rc == 0 and j["size_px"][0] * j["size_px"][1] <= 10000, f"auto --res coarsened to the cap, pad included ({j.get('size_px')})")
del os.environ["LMAP_WF_MAX_PX"]
left = [p.name for p in TMP.rglob("*") if p.name != f"lmap-{os.getuid()}"]
check(not left, f"scratch empty: {left[:5]}")
rc, out, err = cli("terrain", "water", f"--from={ll(E0 + 99000, N0)[0]},{ll(E0 + 99000, N0)[1]}", "--yes")
check(rc == 4 and json.loads(err).get("error"), f"off the terrain: exit 4 + JSON ({rc})")


def root(arr, res=10.0):
    r = Path(tempfile.mkdtemp(prefix="t_water_r_", dir=base)); (r / "data" / "australia").mkdir(parents=True)
    dem(r / "data" / "australia" / "dem-3d-30m.cog.tif", arr, res); return r


def cli_r(r, *a):
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(r)] + list(a),
                       capture_output=True, text=True, timeout=280, env={**os.environ, "LMAP_TMP": str(TMP)})
    return c.returncode, c.stdout, c.stderr


# a flat plain: no flow lines -> still ok, empty layers (no crash)
r1 = root(np.full((300, 300), 40.0) + rng.uniform(0, 0.001, (300, 300)))
C1 = ll(E0 + 1500, N0 - 1500)
rc, out, err = cli_r(r1, "--json", "terrain", "water", f"--from={C1[0]:.7f},{C1[1]:.7f}", "--radius-km", "1",
                     "--name", "flat", "--stream-km2", "0.5", "--catchment-km2", "5", "--yes")
check(rc == 0 and (r1 / "data/australia/water/flat.gpkg").is_file(), f"flat plain: no crash ({rc} {err[-200:]})")
# inland basin BELOW sea level (Lake Eyre style), ringed by land: must stay land, flow lines run into it
yy, xx = np.mgrid[0:400, 0:400] * 10.0
bowl = -15 + 0.01 * np.hypot(xx - 2000, yy - 2000) + rng.uniform(-0.02, 0.02, xx.shape)
r2 = root(bowl.astype("float32"))
C2 = ll(E0 + 2000, N0 - 2000)
rc, out, err = cli_r(r2, "--json", "terrain", "water", f"--from={C2[0]:.7f},{C2[1]:.7f}", "--radius-km", "1.5",
                     "--name", "bowl", "--stream-km2", "0.02", "--catchment-km2", "0.5", "--yes")
j = json.loads(out) if rc == 0 else {}
check(rc == 0 and j.get("land_km2", 0) > 8 and sum(j["streams"].values()) > 0,
      f"below sea level inland: land kept, flow lines ({rc} {j.get('land_km2')} {j.get('streams')} {err[-200:]})")
# all sea: exit 4 + JSON, scratch cleaned after the failure
r3 = root(np.full((300, 300), -2.0))
rc, out, err = cli_r(r3, "terrain", "water", f"--from={C1[0]:.7f},{C1[1]:.7f}", "--radius-km", "1", "--yes")
check(rc == 4 and json.loads(err).get("error"), f"all sea: exit 4 + JSON ({rc} {err[:100]})")
left = [p.name for p in TMP.rglob("*") if p.name != f"lmap-{os.getuid()}"]
check(not left, f"scratch empty after failures: {left[:5]}")
# breaching: a LEVEL road bank (flat top) across the valley; OSM road along it, OSM creek down the valley floor.
# With breaching the main flow crosses the bank at the creek; --no-breach leaves the terrain alone.
BANK_N = N0 - 2000
zb = z.copy(); band = np.abs(Y - 2000) <= 10
zb[band] = (20 + 0.02 * 4000) + 4.0 + rng.uniform(-0.01, 0.01, band.sum())   # bank top: level, 4 m above the floor
w4 = osr.SpatialReference(); w4.ImportFromEPSG(4326)


def lines_layer(g, lay, fields, rows):
    """rows: (values dict, [(e, n), ...])"""
    Ly = g.CreateLayer(lay, w4, ogr.wkbLineString)
    for fn, ft in fields:
        Ly.CreateField(ogr.FieldDefn(fn, ft))
    for vals, pts in rows:
        f = ogr.Feature(Ly.GetLayerDefn()); ge = ogr.Geometry(ogr.wkbLineString)
        for e_, n_ in pts:
            la, lo = ll(e_, n_); ge.AddPoint_2D(lo, la)
        f.SetGeometry(ge)
        for k_, v_ in vals.items():
            f[k_] = v_
        Ly.CreateFeature(f); f = None


S_ = ogr.OFTString
ROAD = ({"highway": "secondary"}, [(E0 + 200, BANK_N), (E0 + 5800, BANK_N)])
r4 = root(zb.astype("float32")); (r4 / "data" / "australia" / "osm").mkdir()
g = ogr.GetDriverByName("GPKG").CreateDataSource(str(r4 / "data" / "australia" / "osm" / "osm-australia.gpkg"))
lines_layer(g, "roads", [("name", S_), ("highway", S_)], [ROAD])
lines_layer(g, "waterways", [("name", S_), ("waterway", S_)],
            [({"waterway": "stream"}, [(E0 + VX, N0 - 1000), (E0 + VX, N0 - 3000)])])
g = None
# r5: culvert only - no OSM stream, just a short culvert line under the bank in au-breach.gpkg (no pbf: used as is)
r5 = root(zb.astype("float32")); (r5 / "data" / "australia" / "osm").mkdir()
g = ogr.GetDriverByName("GPKG").CreateDataSource(str(r5 / "data" / "australia" / "osm" / "osm-australia.gpkg"))
lines_layer(g, "roads", [("name", S_), ("highway", S_)], [ROAD]); lines_layer(g, "waterways", [("name", S_), ("waterway", S_)], [])
g = ogr.GetDriverByName("GPKG").CreateDataSource(str(r5 / "data" / "australia" / "osm" / "au-breach.gpkg"))
lines_layer(g, "x", [("waterway", S_), ("railway", S_), ("culvert", ogr.OFTInteger)],
            [({"waterway": "stream", "culvert": 1}, [(E0 + VX, BANK_N + 12), (E0 + VX, BANK_N - 12)])])
g = None


def bank_cross(gp):
    """easting(s) where the big flow (>3 km2) crosses the bank line"""
    ds = ogr.Open(str(gp)); xs = []
    for f in ds.GetLayerByName("streams"):
        if f["upstream_km2"] > 3:
            ge = f.GetGeometryRef()
            for i in range(ge.GetPointCount() - 1):
                (xa, ya), (xb, yb) = ge.GetPoint_2D(i), ge.GetPoint_2D(i + 1)
                if (ya - BANK_N) * (yb - BANK_N) <= 0 and ya != yb:
                    xs.append(xa + (BANK_N - ya) / (yb - ya) * (xb - xa))
    ds = None
    return xs


C4 = f"--from={C[0]:.7f},{C[1]:.7f}"
off = lambda xs: min(abs(x - (E0 + VX)) for x in xs) if xs else 1e9
WARGS = ("--radius-km", "2.9", "--stream-km2", "0.02", "--catchment-km2", "0.5", "--yes")
rc, out, err = cli_r(r4, "--json", "terrain", "water", C4, "--name", "bk-nb", "--no-breach", *WARGS)
j = json.loads(out) if rc == 0 else {}
xn = bank_cross(r4 / "data/australia/water/bk-nb.gpkg") if rc == 0 else []
check(rc == 0 and j.get("breach") is False and j.get("breaches") == 0 and "osm" not in j
      and j.get("breach_skipped_long") == 0, f"--no-breach: nothing cut ({rc})")
check(off(xn) > 40, f"control: without breaching the flow crosses the bank away from the creek ({[round(x - E0 - VX) for x in xn]})")
rc, out, err = cli_r(r4, "--json", "terrain", "water", C4, "--name", "bk", *WARGS)
j = json.loads(out) if rc == 0 else {}
xs = bank_cross(r4 / "data/australia/water/bk.gpkg") if rc == 0 else []
check(rc == 0 and j.get("breach") is True and j.get("breaches") == 1 and j["osm"]["roads"] == 1
      and "no au-breach" in j.get("breach_data", ""), f"breach run: 1 notch, roads-only without a pbf "
      f"({rc} {j.get('breaches')} {j.get('osm')} {err[-200:]})")
check(off(xs) < 30, f"breached: main flow crosses the bank at the creek ({[round(x - E0 - VX) for x in xs]})")
rc, out, err = cli_r(r5, "--json", "terrain", "water", C4, "--name", "cv", *WARGS)
j = json.loads(out) if rc == 0 else {}
xc = bank_cross(r5 / "data/australia/water/cv.gpkg") if rc == 0 else []
check(rc == 0 and j.get("breaches") == 1 and j["osm"]["culverts"] == 1 and j.get("breach_data") == "ok",
      f"culvert-only: the tagged culvert itself is cut ({rc} {j.get('breaches')} {j.get('osm')} {err[-200:]})")
check(off(xc) < 30, f"culvert-only: main flow goes through the culvert ({[round(x - E0 - VX) for x in xc]})")
rc, out, err = cli_r(r4, "--json", "terrain", "water", C4, "--radius-km", "2.9", "--dry-run")
check(rc == 0 and json.loads(out).get("breach") is True, "dry-run shows breach on")
left = [p.name for p in TMP.rglob("*") if p.name != f"lmap-{os.getuid()}"]
check(not left, f"scratch empty after breach runs: {left[:5]}")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
