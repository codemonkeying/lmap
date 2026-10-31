#!/usr/bin/env python3
"""area phone-map: CLI contract, tile maths, + a small real render of townsville (needs the built area).
Usage: t_phonemap.py <scratch>"""
import io, json, math, os, sqlite3, subprocess, sys
from pathlib import Path
L = str(Path(__file__).resolve().parent.parent / "bin" / "lmap")
BB = "--bbox=146.79,-19.27,146.80,-19.26"
fails = 0
def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += not c
def lm(*a):
    r = subprocess.run([L, "--json", "area", "phone-map", *a], capture_output=True, text=True, timeout=280)
    j = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("{")), {})
    return r.returncode, j

# tile_range: pure maths from the module namespace (a bound exactly on a tile edge is not the next tile)
import importlib.machinery, importlib.util
_ld = importlib.machinery.SourceFileLoader("lmap", L)
_m = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", _ld)); _ld.exec_module(_m); ns = vars(_m)
tr = ns["tile_range"]
ok(tr(1, [-180, -85, 180, 85]) == (0, 1, 0, 1), "z1 whole world = 2x2 tiles")
ok(tr(2, [0, -10, 90, 10]) == (2, 2, 1, 2), f"east bound on a tile edge: not the next tile ({tr(2, [0, -10, 90, 10])})")
ok(tr(3, [-200, -89, 200, 89]) == (0, 7, 0, 7), "out-of-world bbox clamped, no math error")
ok(ns["phone_contours"](13)[0] == "none" and ns["phone_contours"](14) == ("best", 20)
   and ns["phone_contours"](17) == ("best", 10), "contours: none <14, 20 m at 14, 10 m from 15")

# export side: an old area's phone map (+ its .sqlitedb) is dropped when not kept; bounds read from metadata
import tempfile
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    for n in ("area-gone~phone-topo.mbtiles", "area-gone~phone-topo.sqlitedb", "area-keep~phone-photo.mbtiles", "area-x-phone-topo.mbtiles", "mine.mbtiles"):
        (td / n).write_bytes(b"x")
    g = ns["drop_other_tiles"](td, {"area-keep~phone-photo.mbtiles"}, set())
    ok(sorted(g) == ["area-gone~phone-topo.mbtiles", "area-gone~phone-topo.sqlitedb", "area-x-phone-topo.mbtiles"] and (td / "mine.mbtiles").is_file(),
       f"export drops a removed area's phone map, keeps the user's files ({g})")
    m = td / "b.mbtiles"; db = sqlite3.connect(m)
    db.execute("CREATE TABLE metadata (name text, value text)"); db.execute("INSERT INTO metadata VALUES ('bounds', '1,2,3,4')")
    db.commit(); db.close()
    class _B: parent = Path("/r/areas/au/qld/x")
    class _C: parent = Path("/r/areas/au/qld/x.phone")
    ok(ns["area_tile_name"](_C, "topo", set()) + ".mbtiles" != ns["phone_map_name"](_B, "phone-topo", set()),
       "area 'x.phone' topo clip can't collide with x's phone map")
    class _A: parent = Path("/r/areas/au/qld/x-phone")
    class _B: parent = Path("/r/areas/au/qld/x")
    ok(ns["phone_map_name"](_B, "phone-topo", set()) == "area-x~phone-topo.mbtiles"
       and ns["area_tile_name"](_A, "topo", set()) + ".mbtiles" != ns["phone_map_name"](_B, "phone-topo", set()),
       "area 'x-phone' topo clip can't collide with x's phone map")
    class _D: parent = Path("/r/areas/au/qld/x")
    class _E: parent = Path("/r/areas/au/qld/qld-x")
    ok(ns["phone_map_name"](_D, "phone-topo", {"x"}) == "area-qld~x~phone-topo.mbtiles"
       and ns["phone_map_name"](_E, "phone-topo", set()) != ns["phone_map_name"](_D, "phone-topo", {"x"}),
       "x in two states vs an area named qld-x: different phone map names")
    ok(ns["mbtiles_bounds"](m) == [1, 2, 3, 4] and ns["mbtiles_bounds"](td / "mine.mbtiles") is None, "mbtiles_bounds")

if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(fails > 0)
r = subprocess.run([L, "--json", "area", "list"], capture_output=True, text=True, timeout=120)
areas = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("[")), None)
if r.returncode != 0 or not isinstance(areas, list):
    print(f"FAIL area list (rc {r.returncode}): {r.stderr[-300:]}"); sys.exit(1)
if not any(a.get("name") == "townsville" for a in areas):
    print("SKIP needs a built 'townsville' area (LMAP_ROOT)"); sys.exit(fails > 0)

c, j = lm("townsville", "--dry-run")
ok(c == 0 and j.get("styles") == ["topo", "photo"] and j.get("zoom") == [12, 17], "dry-run: both styles, z12-17")
ok(len(j.get("files", [])) == 4 and all(Path(f).suffix in (".mbtiles", ".sqlitedb") for f in j["files"]), "4 files planned")
z17 = next((q for q in j.get("zooms", []) if q["z"] == 17), {})
ok(abs(z17.get("m_per_px", 0) - 1.13) < 0.05, f"z17 ~1.13 m/px at the test box ({z17.get('m_per_px')})")
_tv = Path(j["files"][0]).parent
ok(j.get("max_zoom") == {"topo": 17, "photo": 17 if (_tv / "aerial.tif").exists() else 16},
   f"photo to z17 only with an aerial ({j.get('max_zoom')})")
if any(a.get("name") == "blue-mountains" for a in areas):     # no aerial: photo stops at z16, unless --zoom says so
    c2, j2 = lm("blue-mountains", "--dry-run"); c3, j3 = lm("blue-mountains", "--zoom", "12-17", "--dry-run")
    ok(j2.get("max_zoom") == {"topo": 17, "photo": 16} and j3.get("max_zoom") == {"topo": 17, "photo": 17},
       f"no aerial = photo z16 by default, an explicit --zoom kept ({j2.get('max_zoom')} / {j3.get('max_zoom')})")
ok(lm("townsville", "--zoom", "12-19", "--yes")[0] == 2, "over the tile cap = 2")
ok(lm("townsville", "--zoom", "12-19", "--dry-run")[0] == 0, "dry-run still shows a big plan")
ok(lm("townsville", BB)[0] == 5, "no --yes = 5")
ok(lm("townsville", "--zoom", "17-12", "--dry-run")[0] == 2, "bad --zoom = 2")
ok(lm("townsville", "--dpi", "20", "--dry-run")[0] == 2, "bad --dpi = 2")
ok(lm("townsville", "--bbox=10,10,11,11", "--dry-run")[0] == 2, "bbox outside area = 2")
ok(lm("townsville", "--label", "a/b", "--dry-run")[0] == 2, "bad label = 2")
ok(lm("nosuch-area", "--dry-run")[0] == 4, "missing area = 4")

# real render: tiny box, z15-16 (contours on), both styles
c, j = lm("townsville", BB, "--zoom", "15-16", "--label", "zz-test", "--yes")
ok(c == 0 and j.get("ok"), f"render exit 0 ({c} {j.get('error')})")
ok(j.get("warnings") == [], f"no warnings (contours drawn) ({j.get('warnings')})")
fs = [Path(f) for f in j.get("files", [])]
try:
    from PIL import Image
    for f in fs:
        ok(f.is_file(), f"{f.name} written")
        if not f.is_file():
            continue
        db = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        if f.suffix == ".mbtiles":
            md = dict(db.execute("SELECT name, value FROM metadata"))
            ok(md.get("format") == ("png" if "topo" in f.name else "jpg"), f"{f.name}: format {md.get('format')}")
            n = dict(db.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY 1"))
            want = {q["z"]: q["n"] for q in j["zooms"]}
            ok(n == want, f"{f.name}: tiles per zoom = plan ({n} vs {want})")
            # georef: the tile holding the bbox centre at z16 is in the file (TMS row)
            z = 16; lon, lat = 146.795, -19.265; m = 2 ** z
            x = int((lon + 180) / 360 * m); y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * m)
            t = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                           (z, x, m - 1 - y)).fetchone()
            ok(t is not None, f"{f.name}: centre tile at its TMS row")
            if t:
                im = Image.open(io.BytesIO(t[0])).convert("RGB")
                ok(im.size == (256, 256) and len(set(getattr(im, "get_flattened_data", im.getdata)())) > 20, f"{f.name}: 256 px, drawn (not blank)")
        else:
            info = db.execute("SELECT minzoom, maxzoom, tilenumbering FROM info").fetchone()
            ok(info == (1, 2, "BigPlanet"), f"{f.name}: OsmAnd info z15-16 inverted ({info})")
        db.close()
finally:
    for f in fs:
        f.unlink(missing_ok=True)
print("ALL PASS" if not fails else f"SOME FAILED ({fails})")
sys.exit(fails > 0)
