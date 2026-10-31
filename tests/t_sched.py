#!/usr/bin/env python3
"""Scheduler + build-path checks (no network, no network share):
1. refresh_plan: qld-project before au-project; projects wait only for the fetches they read; no move-aside step.
2. Dag: an unrelated failed fetch doesn't skip the projects.
3. queue: a source added while the last one finishes still runs (QLOCK hand-over), stress-tested.
4. creation options per build path: fileclip, area, staged overviews, pyramid ovr (argv captured via run()).
5. copy_synced: full copy; stop() aborts mid-copy."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import argparse, importlib.machinery, importlib.util, os, sys, tempfile, threading, time, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

tmp = Path(tempfile.mkdtemp())
(tmp / "areas").mkdir()
def ctx_for(**a):
    c = types.SimpleNamespace(sources={k: {} for k in L.REFRESH_FETCH}, webp=True, log=lambda *x: None,
                              path=lambda r: tmp / r, a=argparse.Namespace(plan=None, only=None, areas="", **a))
    return c

# 1. refresh plan
steps = {s[0]: s for s in L.refresh_plan(ctx_for())}
check("qld-project" in steps["au-project"][2], "au-project waits for qld-project")
check("qld-tides" not in steps["qld-project"][2] and "qld-tides" not in steps["au-project"][2],
      "projects don't wait for unrelated fetches (qld-tides)")
check("osm-australia" in steps["qld-project"][2], "qld-project waits for osm-australia")
check(not any(callable(s[1]) for s in steps.values()) and "osm-old" not in steps, "no move-aside step")
check("--refresh" not in steps["qld-project"][1] and "--refresh" not in steps["au-project"][1],
      "projects not forced to --refresh (build signatures pick up a changed OSM)")

# 2. Dag: failed unrelated fetch, projects still run
ran = []
c = ctx_for()
dag = L.Dag(c, width=4, every=999)
for sid, argv, deps, label in steps.values():
    if sid.startswith("area-"):
        continue
    def fn(sid=sid):
        if sid == "qld-tides":
            raise L.CliError(L.E_FAIL, "tides down")
        ran.append(sid); return {}
    dag.add(sid, fn, deps=deps)
res = dag.run()
check("qld-project" in ran and "au-project" in ran, f"tides failed, projects still ran ({ran})")
check(ran.index("qld-project") < ran.index("au-project"), "qld-project ran before au-project")

# 3. queue hand-over race
lost = 0
for trial in range(300):
    todo, active, done = ["a"], {"h"}, []
    ctx = types.SimpleNamespace()
    def worker():                       # the real exit protocol, fetch replaced by a no-op
        while True:
            with L.QLOCK:
                if not todo:
                    active.discard("h"); return
                done.append(todo.pop(0))
    t = threading.Thread(target=worker); t.start()
    time.sleep(0.00001 * (trial % 7))
    with L.QLOCK:                       # the real supervisor protocol
        todo.append("b"); start = "h" not in active
        if start:
            active.add("h")
    if start:
        t2 = threading.Thread(target=worker); t2.start(); t2.join()
    t.join()
    lost += "b" not in done
check(lost == 0, f"queue add during worker exit: {lost}/300 lost")
src = lmap_source()
qw = src[src.index("def queue_worker"):src.index("def cmd_queue_run")]
check("with QLOCK" in qw and "active.discard(host)" in qw and qw.count("active.discard(host)") >= 2,
      "queue_worker discards active under QLOCK on every exit")

# 4. creation options per build path
check(L.photo_ovr_env(types.SimpleNamespace(webp=False), 95)["COMPRESS_OVERVIEW"] == "JPEG", "photo_ovr_env webp=no -> JPEG")
so = src[src.index("def _staged_overviews"):src.index("def _staged_overviews") + 3000]
check("photo_ovr_env(ctx, 95)" in so and '"COMPRESS_OVERVIEW": "WEBP"' not in so, "staged overviews follow webp=")
pb = src[src.index("def _pyramid_build"):src.index("def _pyramid_build") + 600]
check('"3" if is_float(vrt) else "2"' in pb, "pyramid ovr predictor by data type")
fc = src[src.index("def fileclip_fetch"):src.index("def fileclip_fetch") + 6000]
check("BLOCKSIZE=512" in fc and "OVERVIEW_RESAMPLING" in fc and "BuildSlot(" in fc, "fileclip: shared layout + BuildSlot")
ab = src[src.index("def _area_build_item"):src.index("def _area_build_item") + 6000]
arc = src[src.index("def area_raster_co"):src.index("def area_raster_co") + 1500]   # shared with map_reuse
check("area_raster_co(ctx, kind, src)" in ab and "BLOCKSIZE=512" in arc and arc.count("OVERVIEW_RESAMPLING") >= 3
      and "BuildSlot(" in ab and "is_float(src)" in arc,
      "area build: shared layout, resampling, predictor by type, BuildSlot")
import subprocess
fl, it = tmp / "f.tif", tmp / "i.tif"
subprocess.run(["gdal_create", "-q", "-of", "GTiff", "-outsize", "10", "10", "-ot", "Float32", str(fl)], check=True)
subprocess.run(["gdal_create", "-q", "-of", "GTiff", "-outsize", "10", "10", "-ot", "Int16", str(it)], check=True)
check(L.is_float(fl) and not L.is_float(it) and not L.is_float(tmp / "missing.tif"), "is_float: Float32 yes, Int16/missing no")

# 5. copy_synced
a, b = tmp / "big", tmp / "big.copy"
a.write_bytes(os.urandom(40 << 20))
L.copy_synced(a, b)
check(b.read_bytes() == a.read_bytes(), "copy_synced: identical copy")
n = {"i": 0}
def stop():
    n["i"] += 1; return n["i"] > 1
try:
    L.copy_synced(a, tmp / "big.stop", stop=stop); check(False, "copy_synced stop")
except L.Aborted:
    check((tmp / "big.stop").stat().st_size < a.stat().st_size, "copy_synced: stop() aborts mid-copy")

print("ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
