#!/usr/bin/env python3
"""Reservation - space claimed per step, refused up front, gone the moment its process dies.
Usage: t_space.py <scratch>"""
import importlib.machinery, importlib.util, os, shutil, signal, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
os.environ["XDG_STATE_HOME"] = str(T / "xs")
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
ok = True


def check(c, m):
    global ok
    print(("PASS " if c else "FAIL ") + m); ok = ok and c


class Ctx:
    def log(self, m): pass


G = L.Reservation.__init__.__globals__
real_free = G["free_bytes"]
G["free_bytes"] = lambda p, raw=False: 10e9            # pretend 10 GB usable everywhere
W = T / "disk"; W.mkdir()
locks = L.STATE / "locks"
try:
    with L.Reservation(Ctx(), "a", 6e9, W) as r1:
        check(r1.f.is_file() and r1.f.read_text().startswith("6000000000 "), "claim written while held")
        check(L.BuildSlot.reserved(W) == 6e9, "counted by others (BuildSlot.reserved)")
        try:
            with L.Reservation(Ctx(), "b", 5e9, W):
                check(False, "second claim over the rest refused")
        except L.CliError as e:
            check("needs ~5.0 GB" in e.msg and "claimed by other running lmap steps" in e.msg, f"second claim refused: {e.msg}")
        with L.Reservation(Ctx(), "c", 3e9, W / "not" / "made"):   # missing dir: its existing parent's disk
            check(L.BuildSlot.reserved(W) == 9e9, "two claims add up; a not-yet-made dir counts on its parent's disk")
    check(L.BuildSlot.reserved(W) == 0 and not list(locks.glob("space.*.*")), "released on exit, files gone")

    # a process holding a claim is killed (-9): its claim stops counting at once, its file is tidied next time
    child = subprocess.Popen([sys.executable, "-c", f"""
import importlib.machinery, importlib.util, os, sys, time
ld = importlib.machinery.SourceFileLoader("lmap", {str(HERE / "bin" / "lmap")!r})
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
L.Reservation.__init__.__globals__["free_bytes"] = lambda p, raw=False: 10e9
class C:
    def log(self, m): pass
with L.Reservation(C(), "child", 8e9, {str(W)!r}):
    print("held", flush=True); time.sleep(60)
"""], stdout=subprocess.PIPE, text=True, env={**os.environ})
    check(child.stdout.readline().strip() == "held", "child holds a claim")
    check(L.BuildSlot.reserved(W) == 8e9, "other process's claim counted")
    try:
        with L.Reservation(Ctx(), "d", 4e9, W):
            check(False, "refused while the child holds 8 GB")
    except L.CliError:
        check(True, "refused while the child holds 8 GB")
    child.send_signal(signal.SIGKILL); child.wait()
    check(L.BuildSlot.reserved(W) == 0, "killed child: its claim no longer counts (nothing to clean up)")
    left = list(locks.glob("space.*.*"))
    with L.Reservation(Ctx(), "e", 4e9, W):
        check(left and not any(f.exists() for f in left), "dead process's leftover file tidied by the next claim")

    # nothing needed: never refused, even with no room
    G["free_bytes"] = lambda p, raw=False: -1
    with L.Reservation(Ctx(), "zero", 0, W):
        check(True, "a 0-byte claim never refuses")
finally:
    G["free_bytes"] = real_free

# leftover build temp from before the last reboot is removed even if a live process now has that pid
bt = L.boot_time()
check(bt > 0 or not Path("/proc/stat").exists(), f"boot time read ({bt})")
if bt:
    old = W / f"x.cog.tif.{os.getppid()}.part.tif"; old.write_bytes(b"x")
    os.utime(old, (bt - 7200, bt - 7200))
    live = W / f"y.cog.tif.{os.getppid()}.part.tif"; live.write_bytes(b"x")
    L._sweep_build_tmp(W)
    check(not old.exists() and live.exists(), "pre-reboot leftover removed; a live pid's current file kept")
shutil.rmtree(T, ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
