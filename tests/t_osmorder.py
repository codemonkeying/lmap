#!/usr/bin/env python3
"""osm_fetch stores rows by place: a small view's features sit in a few contiguous runs, same rows/columns.
Uses the real pbf clipped to SE QLD (needs the Australia pbf under $LMAP_ROOT). Usage: t_osmorder.py <tmpdir>"""
import os, importlib.machinery, importlib.util, sqlite3, subprocess, sys, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
_ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap")); L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", _ld)); _ld.exec_module(L)
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg, flush=True)
    ok or fails.append(msg)

if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(0)
pbf = Path(os.environ["LMAP_ROOT"]) / Path("data/australia/osm/australia-latest.osm.pbf")
if not pbf.is_file():
    print("SKIP: no Australia OSM pbf at", pbf, "(set LMAP_ROOT)"); sys.exit(0)
BB = [153.2, -28.2, 153.6, -27.8]        # Gold Coast: osm_fetch's own bbox keeps the test small
s = {"id": "t-osm", "from": str(pbf), "dest": str(d / "out.gpkg"), "indexes": ["roads(highway)"], "bbox": BB,
     "layers": {"roads": {"from": "lines", "where": "highway IS NOT NULL", "select": "name,highway"},
                "places": {"from": "points", "where": "place IS NOT NULL", "select": "name,place"}}}
ctx = types.SimpleNamespace(a=types.SimpleNamespace(refresh=True), scratch=lambda: d, path=Path,
                            log=lambda *a: None, vlog=lambda *a: None)
(d / "out.gpkg").unlink(missing_ok=True)
r = L.osm_fetch(ctx, s, False)
check(r.get("built"), f"built {r}")
c = sqlite3.connect(d / "out.gpkg")
cols = [x[1] for x in c.execute("pragma table_info(roads)")]
check(cols == ["fid", "geom", "name", "highway"], f"roads columns {cols}")
n = c.execute("select count(*) from roads").fetchone()[0]
raw = d / "ref.gpkg"; raw.unlink(missing_ok=True)       # the old (unsorted) way, for the row count + comparison
subprocess.run(["ogr2ogr", "-f", "GPKG", "-spat", *map(str, BB), "-nln", "roads", "-where", "highway IS NOT NULL",
                "-select", "name,highway", str(raw), str(pbf), "lines"], check=True,
               env={**L.os.environ, "OSM_MAX_TMPFILE_SIZE": "2000", "CPL_TMPDIR": str(d)})
ref = sqlite3.connect(raw).execute("select count(*) from roads").fetchone()[0]
check(n == ref and n > 1000, f"roads rows {n} == unsorted {ref}")
check(c.execute("select count(*) from places").fetchone()[0] > 10, "places layer present")
check(c.execute("select count(*) from sqlite_master where name='roads_highway'").fetchone()[0] == 1, "index kept")
check(c.execute("select count(*) from sqlite_master where name='rtree_roads_geom'").fetchone()[0] == 1, "rtree kept")
# order: one small box -> fids in few runs (unsorted pbf order gives hundreds)
ids = sorted(x[0] for x in c.execute("select id from rtree_roads_geom where minx<153.45 and maxx>153.40 "
                                     "and miny<-28.00 and maxy>-28.05"))
runs = 1 + sum(1 for a, b in zip(ids, ids[1:]) if b - a > 50)
rid = sorted(x[0] for x in sqlite3.connect(raw).execute("select id from rtree_roads_geom where minx<153.45 and "
                                                         "maxx>153.40 and miny<-28.00 and maxy>-28.05"))
rruns = 1 + sum(1 for a, b in zip(rid, rid[1:]) if b - a > 50)
check(len(ids) == len(rid) > 100 and runs * 5 <= rruns, f"{len(ids)} roads in a 5km box: {runs} runs (unsorted {rruns})")
check(not list(d.glob("t-osm.*")), "no raw/part files left in scratch")
print("ALL PASS" if not fails else f"{len(fails)} FAIL")
