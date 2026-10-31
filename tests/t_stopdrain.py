#!/usr/bin/env python3
"""Stop and status: (1) a stop / `timeout` on a step build (area create, map, refresh) aborts a step still running after
LMAP_DRAIN_GRACE s, so one long step can't hold the stop for hours; (2) `--label` phone maps are exported;
(3) `lmap status` lists area layers the last build could not refresh. Offline."""
import importlib.machinery, importlib.util, json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

# (1) a step build that ignores the stop (one long child step) ends within grace + abort grace after ONE SIGTERM
prog = f'''
import importlib.machinery, importlib.util, sys, time, argparse
ld = importlib.machinery.SourceFileLoader("lmap", {str(HERE / "bin" / "lmap")!r})
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
a = argparse.Namespace(fn=L.cmd_area_create, noun="area", verb="create", name="t186", dry_run=False)
L.run_begin(a, ["area", "create", "t186"]); print("ready", flush=True)
time.sleep(120)                                   # a step that never looks at DRAIN (e.g. a long gdalwarp)
'''
env = {**os.environ, "LMAP_DRAIN_GRACE": "4", "LMAP_ABORT_GRACE": "2", "LMAP_RUN_AFTER": "0"}
p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
check(p.stdout.readline().strip() == "ready", "step build registered")
t0 = time.time(); p.send_signal(signal.SIGTERM)
try:
    rc = p.wait(30)
except subprocess.TimeoutExpired:
    p.kill(); rc = None
dt = time.time() - t0; err = p.stderr.read()
check(rc == 130 and dt < 15, f"one SIGTERM (as from `timeout`) ends a stuck step build: rc {rc} after {dt:.1f} s")
check("aborting it" in err, "says why it aborted")
check(dt >= 3.5, f"a plain SIGTERM first waits the drain grace: {dt:.1f} s")

# (1b) pause (SIGUSR2, what `lmap job stop` sends to a step build): no deadline; a later SIGTERM brings it back
p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
p.stdout.readline(); p.send_signal(signal.SIGUSR2)
try:
    p.wait(8); paused_alive = False
except subprocess.TimeoutExpired:
    paused_alive = True
check(paused_alive, "pause: the running step is NOT aborted after the grace (waited 8 s, grace 2 s)")
t0 = time.time(); p.send_signal(signal.SIGTERM)
try:
    rc = p.wait(30)
except subprocess.TimeoutExpired:
    p.kill(); rc = None
dt = time.time() - t0
check(rc == 130 and dt < 3.0, f"a SIGTERM after a pause aborts at once (no 2 s drain grace first): rc {rc} {dt:.1f} s")
r = L.run_brief({"pid": 1, "state": "running", "cmd": "area create x", "updated": time.time(), "eta_s": None,
                 "stopping": "pause", "stage": "aerial"})
check("pausing" in r, f"status shows pausing: {r.strip()}")

# (1c) a 2nd `job stop` (SIGUSR2 twice) aborts at once
p = subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
p.stdout.readline(); p.send_signal(signal.SIGUSR2); time.sleep(1); t0 = time.time(); p.send_signal(signal.SIGUSR2)
try:
    rc = p.wait(30)
except subprocess.TimeoutExpired:
    p.kill(); rc = None
check(rc == 130 and time.time() - t0 < 3.0, f"a 2nd job stop after a pause aborts at once: rc {rc} {time.time() - t0:.1f} s")

# (2) phone maps with --label are picked up by export; .part and other names are not
d = T / "area"; d.mkdir()
for n in ("phone-topo.mbtiles", "phone-topo-zz.mbtiles", "phone-photo-castle-hill.mbtiles",
          "phone-topo.mbtiles.part", "phone-foo.mbtiles", "topo.mbtiles"):
    (d / n).write_bytes(b"x")
k = L.phone_map_keys(d)
check(k == ["phone-photo-castle-hill", "phone-topo", "phone-topo-zz"], f"label maps exported: {k}")
check(L.phone_map_keys(T / "nope") == [], "missing dir: none")
e = T / "phone"; e.mkdir()
for n in ("area-x~phone-topo-zz.mbtiles", "area-x~phone-topo-zz.sqlitedb", "mine.mbtiles"):
    (e / n).write_bytes(b"x")
g = L.drop_other_tiles(e, [], [])
check(sorted(g) == ["area-x~phone-topo-zz.mbtiles", "area-x~phone-topo-zz.sqlitedb"] and (e / "mine.mbtiles").is_file(),
      f"a removed label map is cleaned from the export, the user's file kept: {g}")

# (3) status: area layers not refreshed
root = T / "root"; a1 = root / "areas" / "au" / "qld" / "tsv"; a1.mkdir(parents=True)
(a1 / "area.json").write_text(json.dumps({"built": {
    "dem": {"file": "dem.tif"},
    "topo": {"skipped": "no internet (x)", "stale_kept": "topo.tif is from older inputs (rebuild skipped)"},
    "fire": {"error": "ogr2ogr failed"}, "sea": {"skipped": "box is all sea"}}}))
a2 = root / "areas" / "au" / "qld" / "bad"; a2.mkdir(parents=True); (a2 / "area.json").write_text("{not json")
class Ctx:
    def path(self, k):
        return root / k
r = L.areas_not_current(Ctx())
check([(x["area"], x["layer"]) for x in r] == [("tsv", "topo"), ("tsv", "fire")] and "older inputs" in r[0]["why"],
      f"status lists the layers not refreshed (a broken area.json is skipped): {r}")
import inspect
src = inspect.getsource(L.area_qgis) if hasattr(L, "area_qgis") else ""
check(open(HERE / "lmaplib" / "05_area.py").read().count("preexec_fn=_pdeath, args=[sys.executable") == 3
      and "child(pr)" in inspect.getsource(L.lmap_step), "QGIS/peaks/HLS children die with lmap; refresh's child lmap is killed on a hard exit")
import types
st = {"topo": types.SimpleNamespace(id="topo", state="running", t0=time.time() - 1600)}
o2 = []; L.dag_eta({"g": types.SimpleNamespace(id="sat-new", state="running", t0=time.time() - 99999)}, 1, "area create x", [], o2)
check(o2 == [], "a step with no history (first-run guess) is never called over its usual time")
L.step_hist_add("area create x|topo", 200.0); o = []
L.dag_eta(st, 1, "area create x", [], o)
r = L.run_brief({"pid": 1, "state": "running", "cmd": "area create x", "updated": time.time(), "eta_s": 60, "overdue": o})
check(o and "over usual time: topo 26m" in r and "<1m" not in r, f"a step past its usual time says so, not eta <1m: {r.strip()}")
print("ALL OK" if not fails else "SOME FAILED"); sys.exit(1 if fails else 0)
