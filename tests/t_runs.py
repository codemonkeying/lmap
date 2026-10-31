#!/usr/bin/env python3
"""Run registry (97a): a long command is listed (lmap status / job list / job status <pid>) with steps done/total and
the running steps, its log is readable (job log <pid>), `job stop <pid>` drains it, a failed command records its exit,
a killed one reads as "died". Offline; a private XDG_STATE_HOME. Usage: t_runs.py <tmpdir>"""
import json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LM = str(HERE / "bin" / "lmap")
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
ENV = dict(os.environ, XDG_STATE_HOME=str(T / "state"), LMAP_RUN_AFTER="0")
R = T / "state" / "lmap" / "runs"
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
def lmap(*a):
    r = subprocess.run([sys.executable, LM, "--json", *a], env=ENV, capture_output=True, text=True, timeout=120)
    try:
        return r.returncode, json.loads(r.stdout or "null")
    except ValueError:
        return r.returncode, None

# a stand-in long build: the real Dag + registry + signal handlers, steps that wait for a drain
CHILD = r'''
import importlib.machinery, importlib.util, sys, time, types
ld = importlib.machinery.SourceFileLoader("lmap", sys.argv[1])
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
L.RUN.start(["area", "create", "fake"], "area create fake"); L.install_signals()
ctx = types.SimpleNamespace(log=lambda m: L.RUN.logline("t " + m))
dag = L.Dag(ctx, width=2, every=1)
def slow():
    while not (L.DRAIN.is_set() or L.ABORT.is_set()):
        time.sleep(0.2)
dag.add("quick", lambda: None); dag.add("slow-a", slow, deps=["quick"]); dag.add("slow-b", slow, deps=["quick"])
dag.add("after", lambda: None, deps=["slow-a", "slow-b"])
dag.run()
L.RUN.finish(L.E_DRAINED if L.DRAIN.is_set() else L.E_OK)
'''
p = subprocess.Popen([sys.executable, "-c", CHILD, LM], env=ENV)
d = None
for _ in range(60):
    time.sleep(0.5)
    try:
        d = json.loads((R / f"{p.pid}.json").read_text())
        if d.get("running"):
            break
    except (OSError, ValueError):
        pass
time.sleep(11)                                         # one registry write throttle period
rc, st = lmap("status")
runs = (st or {}).get("status", {}).get("runs", [])
me = next((r for r in runs if r["pid"] == p.pid), None)
check(me is not None and me["state"] == "running", f"lmap status lists the run as running ({me and me['state']})")
check(me is not None and me.get("total") == 4 and me.get("done") == 1 and sorted(me.get("running") or []) == ["slow-a", "slow-b"],
      f"steps done/total + running steps recorded ({me and (me.get('done'), me.get('total'), me.get('running'))})")
check(me is not None and me.get("drain") is True, "the run says it drains on stop")
rc, js = lmap("job", "status", str(p.pid))
check(rc == 0 and js["runs"][0]["pid"] == p.pid, "job status <pid>")
rc, jl = lmap("job", "list")
check(rc == 0 and any(j.get("run") and j["pid"] == p.pid for j in jl), "job list includes the run")
hr = subprocess.run([sys.executable, LM, "job", "list"], env=ENV, capture_output=True, text=True, timeout=120)
check(hr.returncode == 0 and str(p.pid) in hr.stdout, f"human job list prints the run (rc {hr.returncode} {hr.stdout[-200:]!r})")
rc, lg = lmap("job", "log", str(p.pid))
check(rc == 0 and any("slow-a: start" in l for l in lg["lines"]), "job log <pid> shows its log lines")
rc, sp = lmap("job", "stop", str(p.pid), "--yes", "--wait", "20")
check(rc == 0 and sp.get("mode") == "drain" and sp.get("exited") is True, f"job stop <pid> drains a non-step run (no SIGUSR2) ({sp})")
p.wait(30)
rc, js = lmap("job", "status", str(p.pid))
check(js and js["runs"][0]["state"] == "stopped" and js["runs"][0]["exit"] == 75, "final state stopped, exit 75")

# a real command line that fails: registered with its exit code (LMAP_RUN_AFTER=0)
q = subprocess.run([sys.executable, LM, "--json", "job", "start", "no-such-source", "--yes"], env=ENV,
                   capture_output=True, text=True, timeout=120)
rec = [json.loads(f.read_text()) for f in R.glob("*.json") if f.stem.isdigit()]
check(any(r.get("cmd", "").startswith("job start") and r.get("final") == "failed" and r.get("exit") == q.returncode != 0
          for r in rec), f"a failed command is recorded with its exit code ({q.returncode})")

# killed hard: died
k = subprocess.Popen([sys.executable, "-c", CHILD, LM], env=ENV)
for _ in range(60):
    time.sleep(0.5)
    if (R / f"{k.pid}.json").is_file():
        break
time.sleep(1); k.send_signal(signal.SIGKILL); k.wait(10)
rc, js = lmap("job", "status", str(k.pid))
check(js and js["runs"][0]["state"] == "died", f"kill -9 reads as died ({js and js['runs'][0]['state']})")

# job start passes --refresh through
rc, ds = lmap("job", "start", "osm-au", "--refresh", "--dry-run")
check(rc == 0 and "--refresh" in ds.get("argv", []), "job start --refresh reaches data fetch")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
