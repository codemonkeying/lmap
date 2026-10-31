#!/usr/bin/env python3
"""terrain los: MGRS round trip, synthetic-DEM line of sight with known answers, curvature, Fresnel, CLI contract.
Offline; builds its own small DEMs."""
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


# --- MGRS: round trip in all three QLD/NSW zones and both input forms; LatLonTools gave 55KDU8108470615
check(L.to_mgrs(-19.258, 146.82) == "55K DU 81084 70615", "to_mgrs matches LatLonTools (example point)")
for s in ("55K DU 81084 70615", "55KDU8108470615", "54H VE 12345 67890", "56J LM 00500 99500"):
    la, lo = L.parse_pos(s)
    check(L.to_mgrs(la, lo).replace(" ", "") == s.replace(" ", ""), f"MGRS round trip {s}")
la, lo = L.parse_pos("55K DU 810 706")                      # 100 m ref -> centre of that square
check(L.to_mgrs(la, lo) == "55K DU 81050 70650", "short MGRS = centre of its square")
check(L.parse_pos("-19.5, 146.25") == (-19.5, 146.25), "lat,lon accepted")
for bad in ("55K DU 8108 7061 5", "55K DU 123", "99Z AA 1 1", "hello", "-95,146"):
    try:
        L.parse_pos(bad); check(False, f"rejects {bad!r}")
    except L.CliError as e:
        check(e.code == L.E_USAGE, f"rejects {bad!r}")

# --- synthetic DEM in UTM 55S: flat 100 m ground with a 30 m ridge 2 km east of the observer
d = Path(tempfile.mkdtemp(prefix="t_los_"))
E0, N0, px = 400000.0, 7870000.0, 2.0


def dem(name, fn, w=3000, h=200):
    ds = gdal.GetDriverByName("GTiff").Create(str(d / name), w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((E0, px, 0, N0, 0, -px)); sr = osr.SpatialReference(); sr.ImportFromEPSG(32755)
    ds.SetProjection(sr.ExportToWkt())
    x = E0 + (np.arange(w) + 0.5) * px              # nodata BEFORE writing: GDAL skips all-zero blocks of a new
    ds.GetRasterBand(1).SetNoDataValue(-32768)      # file, which would then read back as nodata
    ds.GetRasterBand(1).WriteArray(np.tile(fn(x), (h, 1)).astype("float32"))
    ds = None
    return L._Dem(d / name)


def ll(e, n=N0 - 200):
    lon, lat, _ = L._utm_tr(55, True, inverse=True).TransformPoint(e, n)
    return lat, lon


ridge = dem("ridge.tif", lambda x: 100 + 30 * np.exp(-((x - (E0 + 2000)) / 40) ** 2))
A, B = ll(E0 + 100), ll(E0 + 5000)
p = L.los_profile([ridge], A, B)
check(abs(p["D"] - 4900) < 1 and p["step"] <= 2.01, f"profile along the grid line, 2 m steps ({p['D']:.1f} m, {p['step']:.2f})")
r = L.los_solve(p, 1.7, 1.7, 0.143)
check(not r["visible"], "30 m ridge blocks 1.7 m -> 1.7 m")
i = r["blocked_at_i"]
check(abs(p["d"][i] - 1900) < 10, f"blocking point is the ridge top ({p['d'][i]:.0f} m along)")
check(not r["seen"][-1] and r["seen"][len(p["d"]) // 4], "dead ground behind the ridge, not before it")
r = L.los_solve(p, 60, 1.7, 0.143)
check(r["visible"], "60 m mast sees over the ridge")
# target height needed: eye 101.7 at 0, ridge 130 at 1900 -> line to the target at 4900 m
need = 101.7 + (130 - 101.7) * 4900 / 1900 - 100
r = L.los_solve(p, 1.7, need + 1, 1.0)                     # k=1: (1-k)=0 -> no curvature, plain geometry
check(r["visible"] and abs(r["target_seen_m"] - 1) < 0.2, f"partly seen target: top ~1 m shows ({r['target_seen_m']:.2f})")

# --- earth curvature, flat sea-level ground. Horizon of a 1.7 m eye with k=0.143: sqrt(2*1.7*R/(1-k)) = 5.03 km,
# so two 1.7 m people just see each other at 10 km and not at 13 km; 20 m masts (17.2 km each) do at 13 km.
flat = dem("flat.tif", lambda x: np.zeros_like(x), w=7000)
p = L.los_profile([flat], ll(E0 + 100), ll(E0 + 13100))
check(not L.los_solve(p, 1.7, 1.7, 0.143)["visible"], "earth curve hides a 1.7 m target at 13 km over flat ground")
check(L.los_solve(p, 20, 20, 0.143)["visible"], "20 m masts see each other at 13 km")
p = L.los_profile([flat], ll(E0 + 100), ll(E0 + 9100))
check(L.los_solve(p, 1.7, 1.7, 0.143)["visible"], "...but 1.7 m to 1.7 m at 9 km is still in view")
p = L.los_profile([flat], ll(E0 + 100), ll(E0 + 10100))
g = L.los_solve(p, 1.7, 1.7, 0.0)["ge"]
check(abs((g[0] - g[-1]) - 10000 ** 2 / (2 * L.EARTH_R)) < 0.05, f"curvature drop at 10 km = {g[0] - g[-1]:.2f} m (7.85)")

# --- Fresnel: 477 MHz over 5 km, midpoint r1 = sqrt(lambda d1 d2 / D) = sqrt(0.6285*2500*2500/5000) = 28.0 m
p = L.los_profile([flat], ll(E0 + 100), ll(E0 + 5100))
r = L.los_solve(p, 10, 10, 0.25, mhz=477)
mid = len(p["d"]) // 2
check(abs(r["r1"][mid] - 28.03) < 0.1, f"first Fresnel radius at midpoint {r['r1'][mid]:.2f} m (28.03)")
check(r["visible"] and r["fresnel"]["margin_m"] < 0, "10 m masts over 5 km: line clear but Fresnel short -> marginal")
check(L.los_solve(p, 40, 40, 0.25, mhz=477)["fresnel"]["margin_m"] > 0, "40 m masts: 60% Fresnel clear")

# --- nodata on the line is an error, not a silent pass
try:
    L.los_profile([flat], ll(E0 + 100), ll(E0 + 30000)); check(False, "off-DEM line refused")
except L.CliError as e:
    check(e.code == L.E_NOTFOUND, "off-DEM line refused (not found)")

# --- points file parser
pf = d / "ops.txt"
pf.write_text("# comment\nOP1, 55K DU 81084 70615\nOP2, -19.30, 146.80, 5m\n\nOP3, 55K DU 810 706  # trailing\n")
pts = L.read_points(str(pf))
check([p["name"] for p in pts] == ["OP1", "OP2", "OP3"] and pts[1]["height"] == 5.0 and pts[0]["height"] is None,
      "points file: names, MGRS/lat,lon, per-point height, comments")

# --- review fixes: GPX reads waypoints only (not track fixes); DEM's outer half pixel is used; wrong zone refused
gx = d / "ops.gpx"
gx.write_text('''<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">
<wpt lat="-19.30" lon="146.80"><name>W1</name></wpt><wpt lat="-19.31" lon="146.81"><name>W2</name></wpt>
<trk><trkseg><trkpt lat="-19.2" lon="146.7"/><trkpt lat="-19.21" lon="146.71"/><trkpt lat="-19.22" lon="146.72"/></trkseg></trk>
</gpx>''')
check([p["name"] for p in L.read_points(str(gx))] == ["W1", "W2"], "GPX: waypoints only, track fixes ignored")
edge = dem("edge.tif", lambda x: np.full_like(x, 50.0), w=100, h=100)
lat_e, lon_e = ll(E0 + 0.6, N0 - 0.6)                        # inside the first pixel's outer half
check(abs(edge.sample([lon_e], [lat_e])[0] - 50) < 1e-6, "DEM edge: outer half pixel sampled (clamped), not dropped")
try:
    L.parse_pos("54K DU 81084 70615"); check(False, "wrong zone for the square refused")
except L.CliError:
    check(True, "MGRS with a wrong zone refused")

# --- CLI contract (no DEM needed): usage errors exit 2 with JSON, dry-run, refuse overwrite without --yes
lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(d)]


def cli(*a):
    c = subprocess.run(lm + list(a), capture_output=True, text=True); return c.returncode, c.stdout, c.stderr


rc, out, err = cli("terrain", "los", "--from", "55K DU 1 1")
check(rc == 2 and json.loads(err)["error"], f"missing --to: exit 2 + JSON error ({rc})")
rc, out, err = cli("--json", "terrain", "los", "--from", "55K DU 81084 70615", "--to=-19.30,146.80", "--dry-run")
j = json.loads(out)
check(rc == 0 and j["dry_run"] and j["points"][1]["mgrs"].startswith("55K DU"), "dry-run JSON lists both points")
(d / "x.png").write_text("x")
rc, out, err = cli("terrain", "los", "--from", "55K DU 81084 70615", "--to=-19.30,146.80", "--png", str(d / "x.png"))
check(rc == 5, f"refuses to overwrite --png without --yes ({rc})")
rc, out, err = cli("terrain", "los", "--from", "55K DU 81084 70615", "--to=-19.30,146.80", "--radio", "0")
check(rc == 2, "bad --radio is a usage error")

rc, out, err = cli("terrain", "los", "--from", "55K DU 81084 70615", "--to=-19.30,146.80", "--refraction", "3")
check(rc == 2, "--refraction out of range is a usage error")

# --- review 2: KML 'Name', BOM, network via the CLI with one bad pair, --gpkg in single mode
km = d / "ops.kml"
km.write_text('''<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>K1</name><Point><coordinates>146.80,-19.30</coordinates></Point></Placemark>
<Placemark><name>K2</name><Point><coordinates>146.81,-19.31</coordinates></Point></Placemark></Document></kml>''')
check([p["name"] for p in L.read_points(str(km))] == ["K1", "K2"], "KML names kept (field 'Name')")
bom = d / "bom.txt"; bom.write_bytes("\ufeffOP1, -19.3, 146.8\nOP2, -19.31, 146.81\n".encode("utf-8"))
check(L.read_points(str(bom))[0]["name"] == "OP1", "BOM from Excel stripped")
bad = d / "bad.txt"; bad.write_text("OP1, -19.3, 146.8, 900m\nOP2, -19.31, 146.81\n")
try:
    L.read_points(str(bad)); check(False, "per-point height > 500 m refused")
except L.CliError:
    check(True, "per-point height > 500 m refused")
# a root whose australia/ holds the synthetic DEM (as the 30 m file), so the CLI finds terrain
root = d / "root"; (root / "data" / "australia").mkdir(parents=True)
gdal.Translate(str(root / "data" / "australia" / "dem-3d-30m.cog.tif"), str(d / "ridge.tif"))
lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--root", str(root)]
pa, pb, pc = ll(E0 + 100), ll(E0 + 5000), ll(E0 + 900)
net = d / "net.txt"
net.write_text(f"West, {pa[0]:.6f}, {pa[1]:.6f}\nEast, {pb[0]:.6f}, {pb[1]:.6f}\nNear, {pc[0]:.6f}, {pc[1]:.6f}\n"
               f"Sea, -19.0, 150.0\n")
rc, out, err = cli("--json", "terrain", "los", "--points", str(net), "--gpkg", str(d / "net.gpkg"), "--yes")
j = json.loads(out) if out else {}
check(rc == 0 and j.get("pairs") == 6 and j.get("failed_pairs") == 3,
      f"network: 6 pairs, the 3 to the off-DEM point fail alone ({rc}, {j.get('failed_pairs')}, {err[:80]})")
vis = {(r["from"], r["to"]): r["visible"] for r in j.get("lines", [])}
check(vis.get(("West", "East")) is False and vis.get(("West", "Near")) is True, "network: ridge blocks West-East, not West-Near")
from osgeo import ogr
ds = ogr.Open(str(d / "net.gpkg")) if (d / "net.gpkg").is_file() else None
check(ds is not None and ds.GetLayerByName("sightlines").GetFeatureCount() == 3 and
      ds.GetLayerByName("points").GetFeatureCount() == 4, "network gpkg: 3 good sightlines + 4 points")
rc, out, err = cli("terrain", "los", "--points", str(net))
check(rc == 0 and "ERROR" in out and "SEES" in out, "network human output shows SEES and the failed pair")
rc, out, err = cli("--json", "terrain", "los", "--from", f"{pa[0]:.6f},{pa[1]:.6f}", "--to", f"{pb[0]:.6f},{pb[1]:.6f}",
                   "--gpkg", str(d / "one.gpkg"), "--png", str(d / "one.png"))
j = json.loads(out) if out else {}
check(rc == 0 and (d / "one.gpkg").is_file() and (d / "one.png").is_file() and j.get("blocked_by"),
      f"single line: --gpkg and --png written, blocked_by reported ({rc} {err[:80]})")

print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
