#!/usr/bin/env python3
"""Status ETA (108a): never '0m' or 'unknown' while work is left. Downloads include the build after them; build
stages report time left; refresh steps with no history get a marked guess; a passed ETA reads 'overdue'. Offline."""
import importlib.machinery, importlib.util, os, sys, tempfile, time, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(T)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
now = time.time()
D = lambda **k: {"pid": 1, "state": "running", "cmd": "x", "done": 1, "total": 2, "updated": now, **k}
check("0m" not in L.run_brief(D(eta_s=0)) and "overdue" in L.run_brief(D(eta_s=0)), "a reached ETA while running says overdue, not 0m")
check("eta 1h" in L.run_brief(D(eta_s=3700)), "normal ETA")
check("eta ~" in L.run_brief(D(eta_s=3700, eta_guess=True)), "guess marked ~")
check("(copying" in L.run_brief(D(eta_s=600, stage="copying a.tif 3.0/5.6 GB")), "build stage shown with its ETA")
s = {"id": "sat-x", "est_gb": 4.5, "compress": "WEBP", "out": "a.tif"}
e = L.build_est_s(s)
check(4000 < e < 12000, f"photo build estimate from size: {e}s for 4.5 GB (today took ~6600)")
L.RUN.d = {"parts": {}, "dag": False}; L.RUN.on = False
p = L.Progress(s, 10, 10); p.write(final=None)
check((L.RUN.d.get("eta_s") or 0) >= e - 1, "all units downloaded: ETA = the build still to come, not 0")
st = {i: types.SimpleNamespace(id=i, state="wait", t0=None) for i in ("area-new", "qld-project", "sat-z")}
g = []; r = L.dag_eta(st, 6, "refresh-test", g)
check(r and r >= 3600 and g, f"refresh with no history: guessed ETA {r}s, marked guess")
be = L.BuildEta(2.0, True); be.stage("ovr", "o"); be.stage("cog", "c"); be.stage("copy", "k"); be.done()
check(L.step_hist().get("rate|ovr") is not None, "build stages record their measured rate")
lg = T / "j.log"                                      # job status of a finished job whose kind keeps no progress file
for body, want in (("ok: True\naction: data.fetch\n", "done"), ('{"error": "x"}\n', "failed"),
                   ("stopped: drained\n", "stopped"), ('ok: False\nitems: [{"ok": true}]\n', "failed"),
                   ("note: drained the pool\nok: True\n", "done"), ("downloading 3/9\n", None),
                   ("ok: True\n\n=== 2026-01-01 00:00:00 start ===\nfetching\n", None)):
    lg.write_text("=== 2026-01-01 00:00:00 start jobs=3 ===\n" + body)
    check(L.job_log_outcome(lg) == want, f"job log outcome {want} from {body.splitlines()[0]!r}")
check(L.job_log_outcome(T / "none.log") is None, "no log: unknown (killed)")
# measured ETA (113): "estimating" until a slice is timed, then left / measured rate (+-20%), smoothed, never 0m
check("no ETA yet" in L.run_brief(D(eta_s=None)) and "unknown" not in L.run_brief(D(eta_s=None)), "no ETA reads: no ETA yet")
q = L.Progress({"id": "sat-m"}, 100, 0); t0 = q.start
check(q.measured_eta(t0 + 10) is None, "nothing done yet: estimating")
q.done = 1; check(q.measured_eta(t0 + 12) is None, "first slice shorter than 30 s: still estimating")
q.done = 10; e1 = q.measured_eta(t0 + 60)                       # 10 units / 60 s -> 90 left = 540 s
check(e1 and 0.8 * 540 <= e1 <= 1.2 * 540, f"first measured ETA {e1}s vs true 540s")
for k in range(11, 60):                                         # steady 1 unit / 6 s: stays within 20% of truth
    q.done = k; e = q.measured_eta(t0 + 6 * k)
truth = (100 - 59) * 6; check(0.8 * truth <= e <= 1.2 * truth, f"steady ETA {e}s vs true {truth}s")
q.done = 60; ej = q.measured_eta(t0 + 6 * 59 + 1)
check(abs(ej - e) < 0.25 * e, f"smoothed: one burst moves it {e}->{ej}, no jump")
st = q.measured_eta(t0 + 6 * 59 + 3000)
check(st and st >= 60, f"a stall counts down but never reads 0m ({st}s)")
w = L.Progress({"id": "aer-m"}, 4, 0); w.work(0, 1000)          # 4 huge units: measured on tiles, not units
w.measured_eta(w.start); w.work(100, 1000); ew = w.measured_eta(w.start + 60)
check(ew and 0.8 * 540 <= ew <= 1.2 * 540, f"tile-level work gives an ETA before any unit ends ({ew}s)")
check("finishing" in L.run_brief(D(eta_s=0, stage="building x")) and "overdue" not in L.run_brief(D(eta_s=0, stage="b")), "short build step past its ETA reads finishing")
check("finishing" in L.run_brief(D(eta_s=0, done=2, total=2)), "all units done past ETA reads finishing")
import multiprocessing as _mp                          # concurrent step_hist_add from many processes: none lost
def _w(i):
    for j in range(20):
        L.step_hist_add(f"par|{i}|{j}", j)
_ps = [_mp.Process(target=_w, args=(i,)) for i in range(6)]
[p_.start() for p_ in _ps]; [p_.join() for p_ in _ps]
_n = sum(k.startswith("par|") for k in L.step_hist())
check(_n == 120, f"parallel step history writes: {_n}/120 kept")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
