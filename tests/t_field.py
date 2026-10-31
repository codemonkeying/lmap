#!/usr/bin/env python3
"""field.gpkg rules against a tiny .osm file (GDAL OSM driver, default osmconf; no network, no network share):
1. keys GDAL puts in own columns (man_made, barrier, highway) match: well, tank, gate, ford.
2. other_tags keys match: fuel, hospital.
3. a plain track with no other tags is a 'track' (NULL other_tags); a grade5 one is '4WD track'.
4. skip= leaves kinds out.
5. an area osm.gpkg (geom column) gives the same result."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

tmp = Path(tempfile.mkdtemp())
nodes = [("man_made", "water_well"), ("man_made", "storage_tank"), ("barrier", "gate"), ("highway", "ford"),
         ("amenity", "fuel"), ("amenity", "hospital")]
x = ['<?xml version="1.0"?><osm version="0.6">']
for i, (k, v) in enumerate(nodes, 1):
    x.append(f'<node id="{i}" lat="-19.{i}" lon="146.{i}" version="1"><tag k="{k}" v="{v}"/><tag k="name" v="n{i}"/></node>')
for i in range(3):                                     # way nodes
    x.append(f'<node id="{100 + i}" lat="-19.5" lon="146.{i + 1}" version="1"/>')
x.append('<way id="1" version="1"><nd ref="100"/><nd ref="101"/><tag k="highway" v="track"/></way>')
x.append('<way id="2" version="1"><nd ref="101"/><nd ref="102"/><tag k="highway" v="track"/>'
         '<tag k="tracktype" v="grade5"/></way>')
x.append('</osm>')
src = tmp / "t.osm"; src.write_text("\n".join(x))
ctx = types.SimpleNamespace(log=lambda *a: None, vlog=lambda *a: None, scratch=lambda: tmp)

out = tmp / "f.gpkg"
n = L.build_field(ctx, src, out)
for k in ("well / bore", "water tank / tower", "gate", "ford", "fuel", "hospital / clinic"):
    check(n.get(k) == 1, f"{k}: {n.get(k)}")
check(n.get("track") == 1, f"plain track (no other tags) kept: {n.get('track')}")
check(n.get("4WD track") == 1, f"grade5 track is 4WD: {n.get('4WD track')}")
out2 = tmp / "f2.gpkg"
n2 = L.build_field(ctx, src, out2, skip=("gate", "track"))
check("gate" not in n2 and "track" not in n2 and n2.get("fuel") == 1, f"skip= leaves kinds out: {n2}")

# 5. area path: osm.gpkg (points/lines layers, 'geom' column) as the area OSM step writes it
g = tmp / "osm.gpkg"
for lay in ("points", "lines"):
    subprocess.run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if lay == "lines" else []) + [str(g), str(src), lay],
                   check=True, capture_output=True)
n3 = L.build_field(ctx, g, tmp / "f3.gpkg")
check(n3 == n, f"area osm.gpkg path gives the same counts: {n3 == n}")

print("ALL PASS" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
