#!/usr/bin/env python3
"""Multi-box sources (bboxes = [[w,s,e,n], ...]) and the dev catalog (tools/devcat.py). src_boxes; wms-tiles units
= union of each box's units (single bbox unchanged); arcgis = one query per source (envelope for one box, a ring per
box for several: deduped by object id server-side); STAC one search per box, items deduped; s2-worldcover tile
listing; copernicus tiles = / derived from bboxes; devcat keeps every source overlapping a box, inside it. Offline."""
import importlib.machinery, importlib.util, json, math, os, sys, tempfile, tomllib, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
TSV, BM = [146.74, -19.32, 146.88, -19.22], [150.25, -33.76, 150.42, -33.62]

# ---- src_boxes / src_extent
check(L.src_boxes({"bbox": TSV}) == [TSV], "bbox -> [bbox]")
check(L.src_boxes({"bboxes": [TSV, BM]}) == [TSV, BM], "bboxes -> list")
check(L.src_boxes({}) == [], "neither -> []")
check(L.src_boxes({"bbox": TSV, "bboxes": [BM]}) == [TSV], "bbox wins over bboxes")
check(L.src_extent({"bboxes": [TSV, BM]}) == [146.74, -33.76, 150.42, -19.22], "extent of the boxes")

# ---- wms-tiles: single bbox exactly as before; bboxes = union
def old_units(s):                                    # the pre-bboxes algorithm, verbatim
    w, so, e, n = s["bbox"]; st = float(s.get("step", 1)); out = []
    if st == 1:
        return [(f"s{-la:02d}e{lo:03d}", (lo, la, lo + 1, la + 1)) for la in range(math.floor(so), math.ceil(n))
                for lo in range(math.floor(w), math.ceil(e))]
    eps = 1e-9; tag = f"{st:g}".replace(".", "p")
    for j in range(math.floor(so / st + eps), math.ceil(n / st - eps)):
        for i in range(math.floor(w / st + eps), math.ceil(e / st - eps)):
            out.append((f"g{tag}_r{j}_c{i}", (round(i * st, 6), round(j * st, 6), round((i + 1) * st, 6), round((j + 1) * st, 6))))
    return out
for s in ({"bbox": [138, -29, 154, -9]}, {"bbox": [146.05, -20.3, 147.35, -18.85], "step": 0.1}, {"bbox": TSV, "step": 0.05}):
    check(list(L.wmst_tiles(s)) == old_units(s), f"single bbox units unchanged {s}")
u = list(L.wmst_tiles({"bboxes": [TSV, BM]}))
check([k for k, _ in u] == ["s20e146", "s34e150"], f"bboxes 1-deg units = union {[k for k, _ in u]}")
a = old_units({"bbox": TSV, "step": 0.1}); b = old_units({"bbox": BM, "step": 0.1})
u = list(L.wmst_tiles({"bboxes": [TSV, BM, TSV], "step": 0.1}))
check(u == a + b and len({k for k, _ in u}) == len(u), "bboxes 0.1-deg units = union, deduped (repeat box adds nothing)")
ov = list(L.wmst_tiles({"bboxes": [[146.7, -19.3, 146.9, -19.1], [146.8, -19.2, 147.0, -19.0]], "step": 0.1}))
check(len(ov) == len({k for k, _ in ov}) == 7, f"overlapping boxes (4+4-1): shared unit once ({len(ov)})")

# ---- arcgis: per-source query planning
g1 = L.arc_geo({"bbox": TSV})
check(g1 == {"geometry": "146.74,-19.32,146.88,-19.22", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
             "spatialRel": "esriSpatialRelIntersects"} and list(g1) == ["geometry", "geometryType", "inSR", "spatialRel"],
      "one bbox: the same envelope query as before")
g2 = L.arc_geo({"bboxes": [TSV, BM]}); rings = json.loads(g2["geometry"])["rings"]
check(g2["geometryType"] == "esriGeometryPolygon" and len(rings) == 2 and rings[1][0] == [150.25, -33.76]
      and all(r[0] == r[-1] for r in rings), "bboxes: one polygon query, a closed ring per box")
seen = []
L.arcgis_query = lambda url, p, meter=None: seen.append(p) or {"count": 7}
L.http_get = lambda u, **k: b'{"type": "Feature Layer"}'
ctx = types.SimpleNamespace(a=types.SimpleNamespace(refresh=False), path=lambda p: T / p)
r = L.arcgis_fetch(ctx, {"id": "x", "url": "https://h/MapServer", "dest": "x.gpkg", "layers": {"a": 1, "b": 2},
                         "bboxes": [TSV, BM]}, True)
check(r["features"] == {"a": 7, "b": 7} and len(seen) == 2 and all(p["geometryType"] == "esriGeometryPolygon" for p in seen),
      "arcgis dry-run: one count query per layer over the union (no double count)")
try:
    L.arc_geo({"id": "y"}); check(False, "arcgis with no box refused")
except L.CliError:
    check(True, "arcgis with no box refused")

# ---- STAC: one search per box, items deduped
urls = []
def fake_stac(u, **k):
    urls.append(u); same = {"id": "t1", "bbox": [0, 0, 1, 1], "assets": {"r": {"href": "s3://b/r.tif"}}}
    feats = [same] + ([{**same, "id": "t2"}] if "150.25" in u else [])
    return json.dumps({"features": feats, "links": []}).encode()
L.http_get = fake_stac; L.STATE = T / "state"
it = L.stac_index(ctx, {"id": "st", "bboxes": [TSV, BM], "stac": {"api": "https://x", "collection": "c", "datetime": "d", "bands": ["r"]}})
check(len(urls) == 2 and sorted(it) == ["t1", "t2"], "STAC: a search per box, items by id once")
check(json.loads((T / "state" / "st.stac.json").read_text())["key"][2] == [TSV, BM], "STAC cache keyed by the boxes")

# ---- s2-worldcover: whole-degree box = old tiles; fractional boxes find theirs
def fake_s3(u, **k):
    la = u.rstrip("/").split("/")[-1]
    return "".join(f"<Key>rgbnir/2021/{la}/ESA_{la}E{lo:03d}_S2RGBNIR.tif</Key>" for lo in range(140, 155)).encode()
L.http_get = fake_s3
def s2(s):
    d = T / s["dest"]; (d / "_tiles.txt").unlink(missing_ok=True); return L.s2wc_tiles(ctx, s)
k = s2({"dest": "s2a", "bbox": [146, -21, 148, -19]})
check([L.s2wc_name(x) for x in k] == ["S20E146.tif", "S20E147.tif", "S21E146.tif", "S21E147.tif"], f"s2wc integer box {k}")
k = s2({"dest": "s2b", "bboxes": [TSV, BM]})
check([L.s2wc_name(x) for x in k] == ["S20E146.tif", "S34E150.tif"], f"s2wc bboxes {[L.s2wc_name(x) for x in k]}")

# ---- copernicus
n = lambda s: [t.split("_")[4] + t.split("_")[6] for t in L.cop_tiles(s)]
check(n({"lat": [19, 20], "lon": [146, 147]}) == ["S19E146", "S19E147", "S20E146", "S20E147"], "lat/lon ranges unchanged")
check(n({"tiles": [[20, 146], [34, 150], [20, 146]]}) == ["S20E146", "S34E150"], "explicit tiles, deduped")
check(n({"bboxes": [TSV, BM]}) == ["S20E146", "S34E150"], "tiles derived from bboxes (south-edge naming)")
check(n({"bbox": [146.9, -19.1, 147.1, -18.9]}) == ["S20E146", "S20E147", "S19E146", "S19E147"], "box over a corner: 4 tiles")
check(next(L.cop_tiles({"res": 30, "tiles": [[20, 146]]})) == "Copernicus_DSM_COG_10_S20_00_E146_00_DEM", "30m bucket name")

# ---- devcat
dl = importlib.machinery.SourceFileLoader("devcat", str(HERE / "tools" / "devcat.py"))
D = importlib.util.module_from_spec(importlib.util.spec_from_loader("devcat", dl)); dl.exec_module(D)
cat = tomllib.loads((HERE / "sources.toml").read_text())
out = T / "sources-dev.toml"
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()) as so_:
    D.main([str(out), "--catalog", str(HERE / "sources.toml")])
dev = tomllib.loads(out.read_text())["source"]
check("est total" in so_.getvalue(), "devcat prints the summary")
inside = lambda x, b: b[0] <= x[0] and b[1] <= x[1] and x[2] <= b[2] and x[3] <= b[3]
bad = []
for sid, s in dev.items():
    for b in L.src_boxes(s):
        if not any(inside(b, B) for B in (TSV, BM)):
            bad.append(sid)
    if s.get("kind") == "copernicus":
        if "lat" in s or not s.get("tiles"):
            bad.append(sid)
        for la, lo in s["tiles"]:
            if not any(D.inter([lo, -la, lo + 1, -la + 1], B) for B in (TSV, BM)):
                bad.append(sid)
    if s.get("statewide"):
        bad.append(sid)
check(not bad, f"every kept source lies inside a box {sorted(set(bad))}")
lost = [sid for sid, s in cat["source"].items() if sid not in dev and D.boxes_of(s)
        and any(D.inter(B, x) for x in D.boxes_of(s) for B in (TSV, BM))]
check(not lost, f"no source overlapping a box dropped {lost}")
check(not any(i.startswith("moon") for i in dev) and "tas-topo" not in dev, "moon / other states dropped")
check(dev["dem-au-90m"]["tiles"] == [[20, 146], [34, 150]] and dev["lights-au"]["bboxes"] == [TSV, BM], "AU-wide -> both boxes")
check(dev["qld-roads-state"]["bbox"] == TSV and dev["nsw-roads-state"]["bbox"] == BM, "QLD -> TSV, NSW -> BM")
check(sorted(dev["esa-worldcover"]["files"]) == ["S21E144_Map.tif", "S36E150_Map.tif"], "worldcover: the 2 tiles over the boxes")
check(dev["osm-au"] == cat["source"]["osm-au"], "unclippable whole-file download kept as-is")
check(json.dumps(dev["osm-australia"]["layers"], sort_keys=True) == json.dumps(cat["source"]["osm-australia"]["layers"], sort_keys=True),
      "nested tables round-trip")
v = T / "v.vrt"; v.write_text('<VRTDataset><SourceFilename relativeToVRT="1">t/a.tif</SourceFilename><SourceFilename relativeToVRT="0">/x/b.tif</SourceFilename></VRTDataset>')
L.vrt_abs_sources(v, "/data/src")
check('relativeToVRT="0">/data/src/t/a.tif<' in v.read_text() and ">/x/b.tif<" in v.read_text(), "staged VRT: relative tile paths made absolute")
# a multi-box template's coverage is the union, not a sum: overlapping halves don't make it "full"
(T / "x.xml").write_text("<x/>")
tc = types.SimpleNamespace(path=lambda p: T / p, sources={
    "st": {"template": True, "role": "sat", "xml": "x.xml", "max_res": 1, "bboxes": [[0, 0, 0.6, 1], [0.1, 0, 0.6, 1]]},
    "au": {"template": True, "role": "sat", "xml": "x.xml", "max_res": 10, "bbox": [-10, -10, 10, 10]}})
check(L.state_template(tc, "sat", [0, 0, 1, 1])[0] == "au", "overlapping boxes covering 60% (sum 110%): national wins")
tc.sources["st"]["bboxes"] = [[0, 0, 0.5, 1], [0.5, 0, 1, 1]]
check(L.state_template(tc, "sat", [0, 0, 1, 1])[0] == "st", "boxes that tile the area: the finer state wins")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
