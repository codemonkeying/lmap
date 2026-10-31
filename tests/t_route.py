#!/usr/bin/env python3
"""terrain route: synthetic UTM DEMs with known climb/distance; hysteresis on noise; Naismith/Tobler maths;
GPX track + points file input; CLI contract. Offline."""
import importlib.machinery, importlib.util, json, math, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from osgeo import gdal, osr

ROOT = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(ROOT / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
gdal.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


# --- pure maths
z = np.array([0, 1, 0, 1, 0, 1, 0, 10, 9, 10, 20, 15, 30], float)
check(L.climb_descent(z, 3) == (35.0, 5.0), f"hysteresis 3 m: 1 m noise ignored, 35 up / 5 down {L.climb_descent(z, 3)}")
check(L.climb_descent(z, 0)[0] == 3 + 10 + 1 + 10 + 15, "hysteresis 0: every wiggle counts")
d = np.linspace(0, 5000, 2501); flat = L.route_stats(d, np.zeros_like(d), 3, False)
check(flat["naismith_h"] == 1.0, f"Naismith: 5 km flat = 1:00 ({flat['naismith_h']})")
check(abs(flat["tobler_h"] - 5 / (6 * math.exp(-0.175))) < 0.01, f"Tobler flat = 5.04 km/h ({flat['tobler_h']})")
off = L.route_stats(d, np.zeros_like(d), 3, True)
check(abs(off["tobler_h"] - flat["tobler_h"] / 0.6) < 0.01, "off track = Tobler x 0.6 speed")
ramp = L.route_stats(d, d * 0.12, 3, False)                 # 600 m up over 5 km
check(ramp["climb_m"] == 600 and abs(ramp["naismith_h"] - 2.0) < 0.005, f"Naismith: 5 km + 600 m = 2:00 ({ramp['naismith_h']})")
check(abs(ramp["ground_m"] - math.hypot(5000, 600)) < 1, "ground distance = slope length")
cliff = L.route_stats(d[:51], np.arange(51) * 2.0 * 3, 3, False)   # 72 deg wall
check(1 < cliff["tobler_h"] < 3 and cliff["over_30deg_m"] == 100,
      f"cliff 300 m up in 100 m: Tobler capped at 45 deg (~2 h, not days), flagged >30 deg ({cliff['tobler_h']})")
nz = np.random.default_rng(2).uniform(-1, 1, len(d))
check(L.climb_descent(nz, 3)[0] <= 2 and L.climb_descent(nz, 0)[0] > 500, "+-1 m noise: 0 climb at 3 m, lots at 0")

# --- synthetic DEM in UTM 55S: ramp rising 10 m per 100 m eastward, plus +-1 m noise
dr = Path(tempfile.mkdtemp(prefix="t_route_", dir=sys.argv[1] if len(sys.argv) > 1 else None))
A = dr / "data" / "australia"; A.mkdir(parents=True)
E0, N0, px, W, H = 400000.0, 7870000.0, 10.0, 1200, 300
ds = gdal.GetDriverByName("GTiff").Create(str(A / "dem-3d-30m.cog.tif"), W, H, 1, gdal.GDT_Float32)
ds.SetGeoTransform((E0, px, 0, N0, 0, -px)); sr = osr.SpatialReference(); sr.ImportFromEPSG(32755)
ds.SetProjection(sr.ExportToWkt()); ds.GetRasterBand(1).SetNoDataValue(-32768)
rng = np.random.default_rng(1)
ds.GetRasterBand(1).WriteArray((100 + 0.1 * (np.arange(W) * px)[None, :] + rng.uniform(-1, 1, (H, W))).astype("float32"))
ds = None
ll = lambda e, n=N0 - 1500: tuple(reversed(L._utm_tr(55, True, inverse=True).TransformPoint(e, n)[:2]))
P = [ll(E0 + 500), ll(E0 + 5500), ll(E0 + 5500, N0 - 500)]       # 5 km east (+500 m), then 1 km north (flat)
lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(dr)]


ENV = {**__import__("os").environ, "XDG_STATE_HOME": str(dr / "state"), "XDG_CONFIG_HOME": str(dr / "cfg"),
       "XDG_CACHE_HOME": str(dr / "cache")}     # own state/config: a live lmap on this machine can't interfere


def cli(*a):
    c = subprocess.run(lm + list(a), capture_output=True, text=True, env=ENV); return c.returncode, c.stdout, c.stderr


pf = dr / "walk.txt"
pf.write_text("".join(f"{n}, {la:.7f}, {lo:.7f}\n" for n, (la, lo) in zip(("S", "M", "E"), P)))
rc, out, err = cli("terrain", "route")
check(rc == 2 and json.loads(err)["error"], f"no input: exit 2 + JSON ({rc})")
rc, out, err = cli("terrain", "route", "--via", f"{P[0][0]},{P[0][1]}")
check(rc == 2, f"one waypoint: exit 2 ({rc})")
rc, out, err = cli("--json", "terrain", "route", "--points", str(pf), "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("dry_run") and j["legs"] == 2, f"dry-run: 2 legs ({rc} {err[:80]})")
rc, out, err = cli("--json", "terrain", "route", "--points", str(pf))
j = json.loads(out) if out else {}
check(rc == 0 and abs(j.get("distance_m", -1e9) - 6000) <= 2, f"distance 6.00 km ({j.get('distance_m')}) {err[:100]} {out[:300]}")
check(abs(j["climb_m"] - 500) <= 15 and j["descent_m"] <= 15, f"climb ~500 m despite +-1 m noise ({j['climb_m']}/{j['descent_m']})")
l1, l2 = j["legs"]
check(l1["from"] == "S" and l2["to"] == "E" and abs(l1["distance_m"] - 5000) <= 2, "legs named, 5 km first leg")
check(abs(l1["bearing"]["grid_mils"] - 1600) <= 1 and l2["bearing"]["grid_mils"] in (0, 6399, 1), "leg bearings: E 1600, N 0 mils")
check("mag_mils" in l1["bearing"], "magnetic bearing given")
rc0, out0, _ = cli("--json", "terrain", "route", "--points", str(pf), "--hysteresis", "0")
check(json.loads(out0)["climb_m"] > j["climb_m"], "hysteresis 0 counts more climb")

gpx = dr / "t.gpx"
gpx.write_text('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>x</name><trkseg>'
               + "".join(f'<trkpt lat="{la:.7f}" lon="{lo:.7f}"/>' for la, lo in P) + "</trkseg></trk></gpx>")
rc, out, err = cli("--json", "terrain", "route", "--points", str(gpx), "--off-track", "--png", str(dr / "p.png"),
                   "--gpkg", str(dr / "r.gpkg"))
g = json.loads(out) if out else {}
check(rc == 0 and abs(g.get("distance_m", 0) - 6000) <= 2, f"GPX track read ({rc} {err[:100]})")
check(g.get("tobler_h", 0) > j["tobler_h"] * 1.5, "--off-track slower")
check((dr / "p.png").stat().st_size > 10000, "--png written")
from osgeo import ogr
o = ogr.Open(str(dr / "r.gpkg"))
check(o.GetLayerByName("legs").GetFeatureCount() == 2 and o.GetLayerByName("waypoints").GetFeatureCount() == 3,
      "--gpkg: 2 legs + 3 waypoints")
o = None
rc, out, err = cli("terrain", "route", "--points", str(gpx), "--png", str(dr / "p.png"))
check(rc == 5, f"refuse overwrite --png without --yes ({rc})")
rc, out, err = cli("terrain", "route", "--via=-19.3,160", "--via=-19.31,160.01")
check(rc == 4, f"off the DEM: exit 4 ({rc})")
rc, out, err = cli("terrain", "route", "--points", str(pf))
check(rc == 0 and "Naismith" in out and "S -> M" in out, "human output: times + leg table")

# review: dry-run with an existing --png, repeated waypoint, 2-segment GPX, gpkg vertices, single-leg bearing
rc, out, err = cli("--json", "terrain", "route", "--points", str(pf), "--png", str(dr / "p.png"), "--dry-run")
j = json.loads(out) if out else {}
check(rc == 0 and j["writes"][0]["exists"] is True, f"dry-run with existing --png: exit 0, exists reported ({rc})")
rp = dr / "rep.txt"; rp.write_text(pf.read_text().splitlines()[0] + "\n" + pf.read_text())
rc, out, err = cli("--json", "terrain", "route", "--points", str(rp))
j = json.loads(out) if out else {}
check(rc == 0 and len(j["legs"]) == 3 and "bearing" not in j["legs"][0] and j["legs"][0]["distance_m"] == 0,
      f"repeated waypoint: zero leg, no fake bearing ({rc} {err[:80]})")
far = ll(E0 + 9000, N0 - 2500)
gpx2 = dr / "t2.gpx"
gpx2.write_text('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                + "".join(f'<trkpt lat="{la:.7f}" lon="{lo:.7f}"/>' for la, lo in P[:2]) + "</trkseg><trkseg>"
                + f'<trkpt lat="{far[0]:.7f}" lon="{far[1]:.7f}"/>' + "</trkseg></trk></gpx>")
rc, out, err = cli("--json", "terrain", "route", "--points", str(gpx2))
j = json.loads(out) if out else {}
check(rc == 0 and abs(j["distance_m"] - 5000) <= 2, f"GPX: first segment only, the gap is not walked ({j.get('distance_m')})")
o = ogr.Open(str(dr / "r.gpkg")); ft0 = o.GetLayerByName("legs").GetNextFeature(); f0 = ft0.GetGeometryRef()
pts_ = [f0.GetPoint(i) for i in range(f0.GetPointCount())]
check(len(pts_) == len(set(pts_)) and f0.GetCoordinateDimension() == 3 and pts_[-1][2] > pts_[0][2] + 300,
      "gpkg leg: no duplicate vertex, Z climbs ~500 m")
o = None
rc, out, err = cli("terrain", "route", f"--via={P[0][0]},{P[0][1]}", f"--via={P[1][0]},{P[1][1]}")
check(rc == 0 and "mils grid" in out, "single leg: bearing in human output")
trk = dr / "long.gpx"
xs = np.linspace(E0 + 500, E0 + 5500, 3001)                # 3000 fixes ~1.7 m apart: thinned to 10 m
trk.write_text('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
               + "".join('<trkpt lat="%.7f" lon="%.7f"/>' % ll(x) for x in xs) + "</trkseg></trk></gpx>")
rc, out, err = cli("--json", "terrain", "route", "--points", str(trk))
j = json.loads(out) if out else {}
check(rc == 0 and j["points"] < 600 and len(j["legs"]) == 1 and j["legs"][0]["bearing"].get("straight_line")
      and abs(j["distance_m"] - 5000) <= 3, f"long track thinned, one start->end leg ({j.get('points')}, {err[:80]})")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
