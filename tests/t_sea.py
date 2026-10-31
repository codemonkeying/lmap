#!/usr/bin/env python3
"""Sea on topo prints: the osm-water source (kind vector-zip) plans its download, converts a zipped shapefile
into an indexed gpkg (layer sea, clipped to the bbox), and SEA_SQL merges the split squares inside a box (no seam
lines). Offline: builds a tiny 2-square shapefile zip and runs vzip_fetch on it via a file:// url."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, types, zipfile
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
from osgeo import ogr
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

import tomllib
for cat in ("sources.toml", "dev/sources-dev.toml"):
    if not (HERE / cat).is_file():                 # dev catalog is gitignored: absent on a clean checkout
        continue
    s = tomllib.load(open(HERE / cat, "rb"))["source"].get("osm-water")
    check(bool(s) and s["kind"] == "vector-zip" and s["layer"] == "sea" and s["kind"] in L.KINDS, f"{cat}: osm-water declared")

# a world file of two touching 1-degree squares (as the split polygons are), zipped like the real download
src = T / "w" / "water-polygons-split-4326"; src.mkdir(parents=True)
wkt = ["POLYGON((146 -20,147 -20,147 -19,146 -19,146 -20))", "POLYGON((147 -20,148 -20,148 -19,147 -19,147 -20))"]
csv = src / "w.csv"; csv.write_text("WKT\n" + "".join(f'"{w}"\n' for w in wkt))
subprocess.run(["ogr2ogr", "-q", "-a_srs", "EPSG:4326", "-oo", "GEOM_POSSIBLE_NAMES=WKT", "-oo", "KEEP_GEOM_COLUMNS=NO",
                str(src / "water_polygons.shp"), str(csv)], check=True)
z = T / "w.zip"
with zipfile.ZipFile(z, "w") as zf:
    for f in src.glob("water_polygons.*"):
        zf.write(f, f"water-polygons-split-4326/{f.name}")

root = T / "root"; root.mkdir()
s = {"id": "osm-water", "kind": "vector-zip", "urls": [f"file://{z}"], "shp": "water-polygons-split-4326/water_polygons.shp",
     "layer": "sea", "bbox": [146.5, -19.8, 147.5, -19.2], "dest": "data/australia/osm-water-polygons.gpkg"}
ctx = types.SimpleNamespace(a=types.SimpleNamespace(refresh=False), path=lambda p: root / p, scratch=lambda: T / "scr",
                            log=print, vlog=print, jobs=3, shrink=1, verbose=False)
(T / "scr").mkdir()
r = L.vzip_fetch(ctx, s, True)
check(r.get("would_fetch") == ["w.zip"] and "would_build" in r, f"plan: would_fetch {r}")
out = root / s["dest"]
try:
    (T / "scr" / "osm-water").mkdir(); import shutil; shutil.copy(z, T / "scr" / "osm-water" / "w.zip")   # as if downloaded
    r = L.vzip_fetch(ctx, s, False)
    ds = ogr.Open(str(out)); lay = ds.GetLayerByName("sea")
    check(lay is not None and lay.GetFeatureCount() == 2, f"gpkg layer sea, both squares in the bbox ({r})")
    q = L.SEA_SQL.format(w=146.5, s=-19.8, e=147.5, n=-19.2)
    res = ds.ExecuteSQL(q, dialect="SQLITE"); f = res.GetNextFeature(); g = f.GetGeometryRef()
    check(g is not None and abs(g.GetArea() - 0.6) < 1e-9 and g.GetGeometryCount() == 1,
          f"SEA_SQL: one merged polygon cut to the box (area {g and g.GetArea()})")
    ds.ReleaseResultSet(res)
    res = ds.ExecuteSQL(L.SEA_SQL.format(w=10, s=10, e=11, n=11), dialect="SQLITE"); f = res.GetNextFeature()
    check(f is None, "SEA_SQL: a box with no sea -> no row (no null geometry)")
    ds.ReleaseResultSet(res); ds = None
    e0 = T / "empty.gpkg"                              # the area item's real ogr2ogr call on a no-sea box
    subprocess.run(["ogr2ogr", "-f", "GPKG", "-spat", "10", "10", "11", "11", "-nln", "sea", e0, out, "-dialect", "SQLITE",
                    "-sql", L.SEA_SQL.format(w=10, s=10, e=11, n=11)], check=True, capture_output=True)
    eds = ogr.Open(str(e0)); el = eds and eds.GetLayerByName("sea")
    check(eds is not None and (el is None or el.GetFeatureCount() == 0), "ogr2ogr no-sea box: opens, 0 features")
    eds = None
    check(L.vzip_fetch(ctx, s, False) == {"unchanged": True}, "second fetch: unchanged")
except Exception as e:
    check(False, f"vzip_fetch: {e!r}")
sys.exit(1 if fails else 0)
