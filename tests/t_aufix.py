#!/usr/bin/env python3
"""Review fixes (no network, no network share; GDAL/queue file mocked):
1. queue: a stop while waiting for scratch space -> stays queued, not FAILED.
2. queue: a held source lock (hand-run fetch) -> waits for it, then fetches; not FAILED.
3. au_dem3d_stale: rebuild when an input is newer.
4. au project: a roads-extract failure stops + waits for the background builds (not via ABORT: its watchdog
   would exit the process hard); every build error is reported.
5. queue: a stop (DRAIN) while waiting for a held source lock ends the wait; source stays queued."""
import argparse, fcntl, importlib.machinery, importlib.util, os, sys, tempfile, threading, time, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

tmp = Path(tempfile.mkdtemp())
L.STATE = tmp / "state"
logs = []
q = {"ids": ["x"], "results": {}, "refresh": []}
L.queue_load = lambda: q
L.queue_edit = lambda fn: fn(q)
L.own_glob = lambda *a: []
L.source_host = lambda s: "h"
L.clean_stale = lambda c, s: 0
L.lock_path = lambda c, s: tmp / "x.lock"
ctx = types.SimpleNamespace(src=lambda i: {"id": i}, a=argparse.Namespace(), log=logs.append, vlog=logs.append,
                            scratch=lambda: tmp, prefix="")

# 1. stop while waiting for space
def sw(c, s):
    L.DRAIN.set(); raise L.Aborted()
L.space_wait = sw
q["results"].clear()
L.queue_worker(ctx, "h", ["x"], {"h"})
check("x" not in q["results"] and any("stays queued" in m for m in logs), "stop while waiting for space: stays queued")
L.DRAIN.clear(); logs.clear()

# 2. lock held by a hand-run fetch: wait, then fetch
L.space_wait = lambda c, s: None
got = []
L.do_fetch = lambda c, s, p: got.append(1) or {"ok": True}
(tmp / "x.lock").touch()
fd = os.open(tmp / "x.lock", os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
threading.Timer(1.5, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd))).start()
L.queue_worker(ctx, "h", ["x"], {"h"})
check(got and q["results"].get("x", {}).get("state") == "done", "held lock: waited, then fetched (done)")

# 3. staleness
a, b, o = tmp / "a.tif", tmp / "b.tif", tmp / "o.tif"
from osgeo import gdal
for p in (a, b, o):                                    # real rasters: an unreadable output now counts as stale
    gdal.GetDriverByName("GTiff").Create(str(p), 2, 2, 1, gdal.GDT_Byte)
bad = tmp / "bad.tif"; bad.write_text("x"); os.utime(bad, (9000, 9000))   # newest, but not a raster
check(L.au_dem3d_stale(bad, [a]), "unreadable 3D terrain: rebuild")
os.utime(a, (1000, 1000)); os.utime(o, (2000, 2000)); os.utime(b, (1500, 1500))
check(not L.au_dem3d_stale(o, [a, b]), "3D terrain newer than inputs: kept")
os.utime(b, (3000, 3000))
check(L.au_dem3d_stale(o, [a, b]), "an input newer: rebuild")
check(L.au_dem3d_stale(tmp / "none.tif", [a]), "missing: build")

# 4. au project error handling (everything heavy mocked)
root = tmp / "root"; (root / "data" / "australia").mkdir(parents=True)
def run4(roads_fail, hs_fail, d3_fail):
    go = threading.Event(); seen = {}
    def hs(ctx, dem, out):
        go.wait(5); seen["hs_abort"] = L.STOP_BUILDS.is_set()
        if hs_fail: raise L.CliError(L.E_FAIL, "hs boom", "")
    def d3(ctx, dem, out):
        go.wait(5); seen["d3_done"] = True
        if d3_fail: raise L.CliError(L.E_FAIL, "d3 boom", "")
    def roads(ctx, files, A):
        if roads_fail:
            threading.Timer(0.5, go.set).start()      # the fake builds see the stop, then end
            raise L.CliError(L.E_FAIL, "roads boom", "")
        go.set()
    L.make_hillshade, L.au_dem3d, L._au_main_roads = hs, d3, roads
    L._au_field = lambda ctx, files, A: None
    L.au_dem3d_inputs = lambda c, dem: [str(a)]
    L.best_raster = lambda c, sid: str(a) if sid == "dem-au-90m" else None
    L.newest_raster = lambda c, k: (None, None)
    L.au_dem3d_fine = lambda *x: (_ for _ in ()).throw(RuntimeError("stop-here"))
    c = types.SimpleNamespace(a=argparse.Namespace(dry_run=False, yes=True), path=lambda r: root / r,
                              sources={"osm-australia": {"dest": "none"}}, log=logs.append, vlog=logs.append)
    L.need_root = lambda c: None
    try:
        L._cmd_au_project(c); err = None
    except Exception as e:
        err = e
    seen["abort_left"] = L.ABORT.is_set() or L.STOP_BUILDS.is_set()
    return err, seen
err, seen = run4(True, True, False)
check(getattr(err, "msg", "") == "roads boom" and seen.get("hs_abort") and seen.get("d3_done"),
      "roads fail: background builds told to stop and waited for")
check(not seen["abort_left"], "roads fail: ABORT never set, STOP_BUILDS cleared after")
check(any("hs boom" in str(m) for m in logs), "roads fail: the hillshade's own error still logged")
logs.clear()
err, seen = run4(False, True, True)
check(getattr(err, "msg", "") == "hs boom" and any("d3 boom" in str(m) for m in logs), "both builds fail: both reported")
err, seen = run4(False, False, False)
check(str(err) == "stop-here", "no failures: goes on to the 10 m patch")

# 5. stop while waiting behind a hand-run fetch's lock
logs.clear(); q["results"].clear(); got.clear()
fd = os.open(tmp / "x.lock", os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
threading.Timer(1.0, L.DRAIN.set).start()
t0 = time.time()
L.queue_worker(ctx, "h", ["x"], {"h"})
check(time.time() - t0 < 8 and not got and "x" not in q["results"], "stop while waiting for a held lock: ends, stays queued")
fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd); L.DRAIN.clear()

# 6. field features failing is not fatal: project goes on (to the patch)
def bad(ctx, files, A): raise L.CliError(L.E_FAIL, "field boom", "")
_, seen = run4(False, False, False)                    # sets mocks
L._au_field = bad
logs.clear()
try:
    L._cmd_au_project(types.SimpleNamespace(a=argparse.Namespace(dry_run=False, yes=True), path=lambda r: root / r,
                      sources={"osm-australia": {"dest": "none"}}, log=logs.append, vlog=logs.append)); err = None
except Exception as e:
    err = e
check(str(err) == "stop-here" and any("field boom" in str(m) for m in logs), "field failure: logged, build goes on")

print("ALL PASS" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
