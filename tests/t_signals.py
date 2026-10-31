#!/usr/bin/env python3
"""A SIGTERM handled as abort (pyramid fix/create: one long GDAL step) gets the full clean-up grace counted from
the signal, not from process start: a run older than the grace must still reach its `except Aborted` clean-up."""
import os, signal, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


mark = tmp / "cleaned"
CHILD = f'''
import importlib.machinery, importlib.util, signal, sys, time
ld = importlib.machinery.SourceFileLoader("lmap", {str(HERE / "bin" / "lmap")!r})
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
L.install_signals(); signal.signal(signal.SIGTERM, L.abort_signal)   # as cmd_pyramid_fix/create do
print("ready", flush=True)
try:
    while not L.ABORT.wait(0.1):
        pass
    raise L.Aborted()
except L.Aborted:
    time.sleep(1.5)                                    # clean-up that takes a moment (partial output removed)
    open({str(mark)!r}, "w").write("ok")
    sys.exit(130)
'''
env = {**os.environ, "LMAP_ABORT_GRACE": "3", "XDG_STATE_HOME": str(tmp / "state")}
p = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True, env=env)
check(p.stdout.readline().strip() == "ready", "child up")
time.sleep(4)                                          # older than the 3 s grace before the stop arrives
p.send_signal(signal.SIGTERM)
rc = p.wait(20)
check(mark.is_file(), f"SIGTERM-as-abort on a run older than the grace: clean-up ran (rc {rc})")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
