#!/usr/bin/env python3
"""terrain bestroute: synthetic UTM DEMs + OSM gpkg. A steep ridge with one pass (the route must use the pass);
a river with one bridge (the route must cross at the bridge); roads ignore/avoid; CLI contract. Needs GRASS."""
import importlib.machinery, importlib.util, json, subprocess, sys, tempfile
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
E0, N0, px, W, H = 400000.0, 7870000.0, 10.0, 600, 400            # 6 x 4 km, 10 m
inv = L._utm_tr(55, True, inverse=True); fwd = L._utm_tr(55, True)
ll = lambda e, n: tuple(reversed(inv.TransformPoint(e, n)[:2]))
MID = N0 - 2000
A_, B_ = ll(E0 + 1000, MID), ll(E0 + 5000, MID)


def root(z, roads=(), rivers=()):
    dr = Path(tempfile.mkdtemp(prefix="t_bw_", dir=base)); A = dr / "data" / "australia"; (A / "osm").mkdir(parents=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(A / "dem-3d-30m.cog.tif"), W, H, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((E0, px, 0, N0, 0, -px)); sr = osr.SpatialReference(); sr.ImportFromEPSG(32755)
    ds.SetProjection(sr.ExportToWkt()); ds.GetRasterBand(1).SetNoDataValue(-32768)
    ds.GetRasterBand(1).WriteArray(z.astype("float32")); ds = None
    g = ogr.GetDriverByName("GPKG").CreateDataSource(str(A / "osm" / "osm-australia.gpkg"))
    w4 = osr.SpatialReference(); w4.ImportFromEPSG(4326)
    for name, fld, items in (("roads", "highway", roads), ("waterways", "waterway", rivers)):
        Ly = g.CreateLayer(name, w4, ogr.wkbLineString); Ly.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        Ly.CreateField(ogr.FieldDefn(fld, ogr.OFTString))
        for kind, line in items:
            f = ogr.Feature(Ly.GetLayerDefn()); ge = ogr.Geometry(ogr.wkbLineString)
            for e, n in line:
                la, lo = ll(e, n); ge.AddPoint_2D(lo, la)
            f.SetGeometry(ge); f[fld] = kind; Ly.CreateFeature(f)
    g = None
    return dr


import os
TMP = Path(tempfile.mkdtemp(prefix="t_bw_tmp_", dir=base))


def cli(dr, *a, env=None):
    c = subprocess.run([sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(dr)] + list(a),
                       capture_output=True, text=True, timeout=280, env={**os.environ, "LMAP_TMP": str(TMP), **(env or {})})
    return c.returncode, c.stdout, c.stderr


def line_en(gp):
    ds = ogr.Open(str(gp)); out = []
    for f in ds.GetLayerByName("legs"):
        ge = f.GetGeometryRef()
        out += [fwd.TransformPoint(ge.GetX(i), ge.GetY(i))[:2] for i in range(ge.GetPointCount())]
    return np.array(out)


ae = lambda p: f"--from={p[0]:.7f},{p[1]:.7f}"
be = lambda p: f"--to={p[0]:.7f},{p[1]:.7f}"

# --- 1. ridge N-S at E0+3000, 300 m high, 60+ deg sides, one 100 m pass 1200 m north of the straight line
x = E0 + (np.arange(W) + 0.5) * px; y = N0 - (np.arange(H) + 0.5) * px
X, Y = np.meshgrid(x, y)
ridge = np.clip(300 - np.abs(X - (E0 + 3000)) * 2.0, 0, None)       # 2:1 sides = 63 deg
PASS_N = MID + 800
ridge[np.abs(Y - PASS_N) < 50] = 0
d1 = root(100 + ridge)
rc, out, err = cli(d1, "terrain", "bestroute")
check(rc == 2 and json.loads(err)["error"], f"no input: exit 2 + JSON ({rc})")
rc, out, err = cli(d1, "terrain", "bestroute", ae(A_))
check(rc == 2, f"--from without --to: exit 2 ({rc})")
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--res", "1")
check(rc == 2, f"--res 1: exit 2 ({rc})")
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dry_run") and j["legs"] == 1 and j["res_m"] == 10 and j["utm_epsg"] == 32755,
      f"dry-run: 1 leg, 10 m, UTM 55S ({rc} {err[:120]})")
gp = d1 / "r.gpkg"
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--gpkg", str(gp))
j = json.loads(out) if out else {}
check(rc == 0, f"run ok ({rc} {err[:300]})")
if rc == 0:
    en = line_en(gp)
    near = en[np.abs(en[:, 0] - (E0 + 3000)) < 30]
    check(len(near) and np.all(np.abs(near[:, 1] - PASS_N) < 60), f"crosses the ridge in the pass "
          f"(N {near[:, 1].min() - PASS_N:.0f}..{near[:, 1].max() - PASS_N:.0f} m from it)" if len(near) else "no crossing")
    check(j["distance_m"] > 4250 and j["straight_m"] == 4000 and j["max_slope_deg"] < 20,
          f"detour longer than straight, never on the 63 deg face ({j['distance_m']} {j['straight_m']} {j['max_slope_deg']})")
    check(abs(en[0, 0] - (E0 + 1000)) < 1 and abs(en[-1, 0] - (E0 + 5000)) < 1, "line starts/ends on the given points")
    check(0.5 < j["r_walk_h"] < 3 and j["legs"][0]["r_walk_h"] == j["r_walk_h"], f"r.walk time sane ({j['r_walk_h']} h)")
    check(j["climb_m"] < 15, f"pass is flat: no climb ({j['climb_m']})")
    rc5, _, err5 = cli(d1, "terrain", "bestroute", ae(A_), be(B_), "--gpkg", str(gp))
    check(rc5 == 5, f"existing --gpkg without --yes: exit 5 ({rc5})")
    rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--gpkg", str(gp), "--dry-run")
    check(rc == 0 and json.loads(out)["writes"][0]["exists"] is True, "dry-run reports the existing output")
# via point on the far side of the ridge, south: two legs
V_ = ll(E0 + 3500, MID - 1000)
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), f"--via={V_[0]:.7f},{V_[1]:.7f}")
j = json.loads(out) if out else {}
check(rc == 0 and len(j["legs"]) == 2 and j["legs"][0]["to"] == "Via1" and j["legs"][1]["from"] == "Via1",
      f"--via: 2 legs Start->Via1->End ({rc} {err[:200]})")
if rc == 0:
    check(abs(sum(l["r_walk_h"] for l in j["legs"]) - j["r_walk_h"]) <= 0.02 and
          abs(j["legs"][0]["end_km"] * 1000 - j["legs"][0]["distance_m"]) <= 2,
          "legs join at the via point; leg r.walk times add up")
rc, out, err = cli(d1, "terrain", "bestroute", ae(A_), be(A_))
check(rc == 2 and "same place" in err, f"start == end: exit 2 'same place' ({rc} {err[:100]})")
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--margin-km", "0", "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j["margin_km"] == 0.05, f"--margin-km 0 honoured (5 cells min: {j.get('margin_km')})")
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), "--margin-km", "0.1")
j = json.loads(out) if out else {}
check(rc == 0 and j["max_slope_deg"] > 45 and j["distance_m"] < 4300,
      f"pass outside a 100 m box: goes over the ridge (steep, not blocked) ({rc} {j.get('max_slope_deg')})")
far = ll(E0 + 1000, MID - 160000)
rc, out, err = cli(d1, "terrain", "bestroute", ae(ll(E0 + 1000, MID - 80000)), be(far), f"--via={A_[0]:.7f},{A_[1]:.7f}")
check(rc == 2 and "max 150" in err, f"span counts every pair, not just from the start ({rc} {err[:100]})")
# strip loop: 64-row strips over 400 rows (overlap + short last strip) = the same line as one strip
rc, out, err = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), env={"LMAP_BW_STRIP": "64"})
j64 = json.loads(out) if out else {}
rc2, out2, _ = cli(d1, "--json", "terrain", "bestroute", ae(A_), be(B_), env={"LMAP_BW_STRIP": "7"})
j7 = json.loads(out2) if out2 else {}
check(rc == 0 and rc2 == 0 and j64["distance_m"] == j7["distance_m"] and j64["r_walk_h"] == j7["r_walk_h"]
      and j64["max_slope_deg"] < 20, f"slope strips 64 vs 7 rows agree ({j64.get('distance_m')} {j7.get('distance_m')})")
check(len(out.strip().splitlines()) == 1, "--json: one line, one document")
png = d1 / "p.png"
rc, out, err = cli(d1, "terrain", "bestroute", ae(A_), be(B_), "--png", str(png))
check(rc == 0 and png.stat().st_size > 5000, f"--png written ({rc})")
# unreachable: end on an island of terrain inside a nodata moat
zz = 100 + ridge.copy(); ex, ey = int(4000 / px), int(2000 / px)
zz[ey - 20:ey + 21, ex - 20:ex + 21] = -32768; zz[ey - 5:ey + 6, ex - 5:ex + 6] = 100
d3 = root(zz)
rc, out, err = cli(d3, "terrain", "bestroute", ae(A_), be(ll(E0 + 4005, MID - 5)))
check(rc == 4, f"end cut off by a nodata moat: exit 4 ({rc} {err[:120]})")
# outside the DEM
rc, out, err = cli(d1, "terrain", "bestroute", ae(A_), be(ll(E0 + 9000, MID)))
check(rc == 4, f"point off the terrain: exit 4 ({rc} {err[:120]})")

# --- 2. flat, river N-S at E0+3000, one bridge (track) 400 m north of the straight line
river = [("river", [(E0 + 3000, N0 - 10), (E0 + 3000, N0 - 3990)])]
BR_N = MID + 400
road = [("track", [(E0 + 2900, BR_N), (E0 + 3100, BR_N)])]
d2 = root(np.full((H, W), 50.0), roads=road, rivers=river)
gp2 = d2 / "r.gpkg"
rc, out, err = cli(d2, "--json", "terrain", "bestroute", ae(A_), be(B_), "--gpkg", str(gp2))
j = json.loads(out) if out else {}
check(rc == 0 and j["friction_from"].get("rivers") == 1 and j["friction_from"].get("roads") == 1,
      f"river + road burned into the friction ({rc} {j.get('friction_from')} {err[:200]})")
if rc == 0:
    en = line_en(gp2); c = en[np.argmin(np.abs(en[:, 0] - (E0 + 3000)))]
    check(abs(c[1] - BR_N) < 20, f"crosses the river at the bridge ({c[1] - BR_N:.0f} m off)")
rc, out, err = cli(d2, "--json", "terrain", "bestroute", ae(A_), be(B_), "--gpkg", str(gp2), "--yes", "--roads", "ignore")
j = json.loads(out) if out else {}
if rc == 0:
    en = line_en(gp2); c = en[np.argmin(np.abs(en[:, 0] - (E0 + 3000)))]
    check("roads" not in j["friction_from"] and abs(c[1] - MID) < 30,
          f"--roads ignore: wades straight across ({c[1] - MID:.0f} m off the line)")
else:
    check(False, f"--roads ignore ran ({rc} {err[:200]})")
rc, out, err = cli(d2, "--json", "terrain", "bestroute", ae(A_), be(B_), "--friction-weight", "0")
j = json.loads(out) if out else {}
check(rc == 0 and abs(j["distance_m"] - 4000) < 30, f"--friction-weight 0: terrain only, straight ({j.get('distance_m')})")
rc, out, err = cli(d2, "terrain", "bestroute", ae(A_), be(B_))
check(rc == 0 and "best route" in out and "r.walk estimate" in out, "human output")

left = [p.name for p in TMP.rglob("*") if p.is_file() or p.name.startswith(("bw", "grass", "tmp"))]
check(not left, f"scratch empty after all runs (incl. failures): {left[:5]}")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
