#!/usr/bin/env python3
"""The machine-wide bandwidth cap is split only over processes downloading right now (idle ones don't count)."""
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
ok = True


def check(c, m):
    global ok
    print(("PASS " if c else "FAIL ") + m); ok = ok and c


d = L.STATE / "bw"; d.mkdir(parents=True, exist_ok=True)
# locks are per open file description: a child holds them so this process sees them as "other" processes
child = subprocess.Popen([sys.executable, "-c", f"""
import fcntl, os, time, sys
d = {str(d)!r}
for name, age in (("p900001", 2), ("p900002", 300)):
    fd = os.open(d + "/" + name, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(fd, fcntl.LOCK_EX)
    t = time.time() - age; os.utime(d + "/" + name, (t, t))
print("ready", flush=True); time.sleep(60)
"""], stdout=subprocess.PIPE, text=True)
child.stdout.readline()
try:
    b = L.TokenBucket(1e6)
    check(b.share() == 2, f"active + idle other: split 2 ways, idle not counted (got {b._n})")
    check((d / f"p{os.getpid()}").is_file(), "own registration file made")
    os.utime(d / "p900002"); b._st = 0
    check(b.share() == 3, f"idle one starts downloading again: counted (got {b._n})")
    child.kill(); child.wait(); b._st = 0
    check(b.share() == 1 and not (d / "p900001").exists(), f"dead processes: files removed, share 1 (got {b._n})")
    mine = d / f"p{os.getpid()}"; t = time.time() - 100; os.utime(mine, (t, t)); b._touch = 0; b.share()
    check(time.time() - mine.stat().st_mtime < 5, "own file re-stamped while downloading")
finally:
    child.kill(); child.wait()
    shutil.rmtree(os.environ["XDG_STATE_HOME"], ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
