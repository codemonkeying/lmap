#!/usr/bin/env python3
"""lmap status progress: phone-map / print / image report step x/y, % and an ETA from the measured rate;
before a run has measured anything the ETA comes from past runs' rate, and with none it says "no ETA yet" (never
a guess). The OVERALL line covers everything running + queued. Offline, fake clock."""
import importlib.machinery, importlib.util, inspect, os, sys, tempfile, time as _t
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)


class Clock:                                   # lmap's `time` module with a settable now
    def __init__(self):
        self.now = 1_800_000_000.0
    def time(self):
        return self.now
    def __getattr__(self, k):
        return getattr(_t, k)
C = Clock(); L.time = C


class Run:                                     # stands in for RUN: keeps what was reported
    def __init__(self):
        self.d = {}
    def update(self, force=False, **kw):
        self.d.update(kw)
R = Run()
brief = lambda: L.run_brief({"pid": 1, "state": "running", "cmd": "area phone-map x", "updated": C.now, **R.d})

# first run ever: nothing measured, no history -> no ETA, never a guess. Phone map: 2 styles x 3 zooms (children)
# costing 6 s start-up + 0.1 s/tile each: zoom sizes 4, 16, 64 tiles
Z = (4, 16, 64)
e = L.WorkEta([("phone-topo", 84, "phone-map topo", 3), ("phone-photo", 84, "phone-map photo", 3)], run=R)
check(e.left() is None, "pre-start with no past runs: no ETA")
e.phase(0); e.child(4); C.now += 3; e.progress(0.5, " z12 tiles 2/4")
check(R.d["eta_s"] is None and "no ETA yet" in brief(), f"under 30 s and no history: 'no ETA yet' ({brief()!r})")
check(R.d["done"] == 2 and R.d["total"] == 168 and "2/168 1%" in brief(), f"step x/y and % shown ({brief()!r})")
check("phone-map topo z12 tiles 2/4 (1/2)" in R.d["stage"], f"stage names style, zoom, tiles, phase ({R.d['stage']!r})")
C.now += 3.4; e.child_end()
for n in Z[1:]:
    e.child(n); C.now += 6 + 0.1 * n; e.child_end()
check(R.d["eta_s"] is None, "topo measured but photo never run: still no ETA (photo's rate unknown)")
e.phase(1)
pd = dict(e.pend); h = (pd.get("start|phone-topo"), pd.get("rate|phone-topo"))
check(h[0] is not None and abs(h[0] - 6) < 0.05 and abs(h[1] - 0.1) < 0.005, f"topo start-up + s/tile fitted from the zooms: {h}")
e.child(4); C.now += 6.4; e.child_end(); e.child(16); C.now += 7.6; e.child_end()     # photo: same costs
e.child(64); C.now += 20; e.progress(0.25)                                            # 31.4 s in: own fit
check(R.d["eta_s"] == 4, f"ETA from this run's fit, never below what the child's tiles left need: {R.d['eta_s']} s (4.8)")
e.finish()
check(L.WorkEta([]).hist("phone-photo") is not None, "photo start-up + rate recorded on finish")
check(R.d["eta_s"] == 0 and R.d["stage"] is None, "finish clears the stage")

# second run, bigger: pre-start ETA from history = zooms x start-up + tiles x rate, not tiles x average
R2 = Run(); e2 = L.WorkEta([("phone-topo", 4 + 16 + 64 + 256 + 1024, "phone-map topo", 5)], run=R2)
true2 = 5 * 6 + 0.1 * 1364
check(abs(e2.left() - true2) <= 2, f"pre-start ETA from past runs: {e2.left()} s (true {true2:.0f})")
e2.phase(0); e2.child(4); C.now += 3; e2.progress(0.5)
check(abs(R2.d["eta_s"] - (true2 - 3)) <= 2, f"before its own rate: history counts down ({R2.d['eta_s']} s)")
C.now += 3.4; e2.child_end()
for n in (16, 64):                              # this run is twice as slow per tile: 0.2 s/tile
    e2.child(n); C.now += 6 + 0.2 * n; e2.child_end()
e2.child(256); C.now += 1; e2.progress(0.0)
true_l = 6 + 0.2 * 256 - 1 + 6 + 0.2 * 1024
check(abs(R2.d["eta_s"] - true_l) <= 0.05 * true_l, f"after 30 s this run's fit wins over history ({R2.d['eta_s']} s, true {true_l:.0f})")
C.now += 200; e2.child_end(); e2.child(1024); C.now += 211; e2.finish()

# phases with no progress of their own (print render, COG pack): rate recorded when the next one starts
R3 = Run(); e3 = L.WorkEta([("print-topo-A4", 1, "print"), ("image-cog", 10, "cog")], run=R3)
e3.phase(0); C.now += 90; e3.phase(1); C.now += 1; e3.finish()
check(L.step_hist().get("rate|print-topo-A4") == 90, "a phase without progress lines is recorded when the run finishes")
check(L.WorkEta([("print-topo-A4", 1, "print")], run=Run()).left() == 90, "next print: pre-start ETA 90 s")
check(L.tile_line_frac("tile 3/12") == 0.25 and L.tile_line_frac("Warning: x") is None, "child 'tile n/m' lines parsed")
fl = L.fit_line([(1, 6.1), (10, 7.0)])
check(L.fit_line([(1, 5), (1, 6)]) is None and abs(fl[0] - 6.0) < 1e-9 and abs(fl[1] - 0.1) < 1e-9, "line fit needs 2 sizes")

# a one-zoom phone map starts its child itself (auto=False): no phantom 0 s child, done never > total
R4 = Run(); e4 = L.WorkEta([("one-z", 100, "phone-map topo", 1)], run=R4)
e4.phase(0, auto=False); e4.child(100); C.now += 10; e4.progress(1.0); e4.child_end()
check(R4.d["done"] == 100 and R4.d["total"] == 100, f"one zoom: done {R4.d['done']}/{R4.d['total']}")
e4.finish(); check(abs(L.step_hist()["rate|one-z"] - 0.1) < 1e-6, "one zoom: s/tile recorded from the real child only")
e4.progress(0.5); check(R4.d["eta_s"] == 0, "a late progress line after finish is ignored")
R5 = Run(); e5 = L.WorkEta([("zero", 0, "image: drawing")], run=R5); e5.phase(0); C.now += 40; e5.progress(0.5)
check(True, "0 units: no division by zero")
check(L.NullRun().update(done=1) is None, "NullRun keeps nothing (print inside lmap map)")

# history start-up + this run's rate (2 small children, >30 s): a from history, b measured
L.step_hist_add("rate|hs", 1.0); L.step_hist_add("start|hs", 10.0)
R6 = Run(); e6 = L.WorkEta([("hs", 100, "phone-map topo", 4)], run=R6); e6.phase(0, auto=False)
e6.child(2); C.now += 11; e6.child_end(); e6.child(4); C.now += 12; e6.child_end(); C.now += 8
check(e6.left() == int(2 * 10 + 0.5 * 94), f"history start-up + measured 0.5 s/tile: {e6.left()} (67)")

# OVERALL line
n = C.now
r1 = {"pid": 11, "state": "running", "started": n - 300, "updated": n, "eta_s": 300}
r2 = {"pid": 12, "state": "running", "started": n - 100, "updated": n - 50, "eta_s": 950}
o = L.status_overall([r1, r2, {"pid": 13, "state": "ok"}], [], 0, now=n)
check(o["running"] == 2 and o["eta_s"] == 900, f"overall end = the latest running ETA ({o['eta_s']} s)")
check(o["pct"] == int(100 * 400 / (400 + 300 + 900)) and "all done" in o["text"], f"overall % by time ({o['text']!r})")
o = L.status_overall([r1, dict(r2, eta_s=None)], [], 0, now=n)
check(o["eta_s"] is None and "no ETA yet" in o["text"] and "measuring" in o["text"], f"one unmeasured: {o['text']!r}")
o = L.status_overall([r1], [], 2, now=n)
check(o["eta_s"] is None and "2 queued" in o["text"], f"queued without an ETA: {o['text']!r}")
o = L.status_overall([r1], [{"running": True, "pid": 11, "eta_s": 99999, "elapsed_s": 5}], 0, now=n)
check(o["running"] == 1 and o["eta_s"] == 300, "a download job run by a registered run is not counted twice")
check(L.status_overall([], [], 0, now=n)["text"] == "nothing running or queued", "idle")

# each long command is wired: phases started, finished, and a pre-start ETA in the plan / dry run
for fn, need in ((L.cmd_area_phone_map, ("WorkEta(", "eta.phase(si, auto=False)", "eta.child(", "eta.child_end()", "eta.progress(",
                                         "eta.finish()", 'plan["eta_s"]')),
                 (L.cmd_area_image, ("WorkEta(", "eta.phase(", "eta.progress(", "eta.finish()", 'plan["eta_s"]')),
                 (L.print_sheet, ('eta_run', "WorkEta(", "eta.phase(0, t0=eta.t_start)", "eta.finish()", '"eta_s": eta.left()'))):
    src = inspect.getsource(fn); miss = [x for x in need if x not in src]
    check(not miss, f"{fn.__name__} reports progress {miss or ''}")
check("NullRun(time.time())" in inspect.getsource(L.cmd_map), "lmap map: the print's ETA does not replace the map's")
check("(in <1m)" in L.status_overall([{"state": "running", "pid": 9, "started": C.now - 10, "updated": C.now, "eta_s": 20}], [], now=C.now)["text"], "OVERALL under a minute reads <1m, not 0m")
# set-up before phase 0 counts (timed from the command start in RUN)
R7 = Run(); R7.d["started"] = C.now; C.now += 20; e7 = L.WorkEta([("su", 1, "print")], run=R7)
e7.phase(0, t0=e7.t_start); C.now += 25; e7.finish()
check(L.step_hist().get("rate|su") == 45, f"whole command timed (20 s set-up + 25 s render): {L.step_hist().get('rate|su')}")
check(L.WorkEta([("su", 1, "p")], run=L.NullRun()).t_start > 0, "no RUN start (NullRun): starts now")
check(L.WorkEta([("su", 1, "p")], run=L.NullRun(C.now - 30)).t_start == C.now - 30, "print in lmap map: timed from its own start")
L.step_hist_add("rate|fz", 5.0); R8 = Run(); e8 = L.WorkEta([("fz", 1, "a"), ("fz2", 1, "b")], run=R8)
e8.phase(0); C.now += 50; e8.phase(1); C.now += 5
check(L.step_hist().get("rate|fz") == 5.0 and "rate|fz2" not in L.step_hist(), "a run that never finishes records nothing")
check("eta.t_start += time.time() - tw" in inspect.getsource(L.cmd_area_image), "image lock wait not timed")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
