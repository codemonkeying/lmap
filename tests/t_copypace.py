#!/usr/bin/env python3
"""CopyPace slows big writes to a network share when they get slow (AIMD) and lifts the cap when they're
fast again; copy_synced paces only network filesystems and the copy is byte-identical. No network share needed."""
import importlib.machinery, importlib.util, hashlib, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

MB = 1 << 20; B = 64 * MB
p = L.CopyPace()
check(p.block(B, 0.5) == 0 and p.cap is None, "healthy blocks: unpaced")
check(p.block(B, 0.6) == 0, "small jitter: still unpaced")
w = p.block(B, 8.0)                                # 8 MB/s, 16x slower than the best
check(p.cap == 4 * MB and abs(w - (16 - 8)) < 1e-6, f"slow block: cap = half that block's rate, waits ({p.cap / MB:.0f} MB/s, {w:.1f}s)")
check(L.CopyPace().block(B, 1.5) == 0, "first block slowish but under 2 s: no cut")
q = L.CopyPace(); q.block(B, 0.5); q.block(B, 64.0)
check(q.cap == q.FLOOR, "never below the 4 MB/s floor")
c0 = p.cap; p.block(B, 0.5)
check(p.cap == c0 + p.STEP, "fast block under a cap: +8 MB/s")
for _ in range(40):
    p.block(B, 0.5)
check(p.cap is None, "keeps rising until the cap is dropped (unpaced again)")
r = L.CopyPace(); r.block(B, 0.5); r.block(B, 8.0); c1 = r.cap; r.block(B, 3.0)
check(r.cap == c1, f"a slow block faster than the cap never raises it ({r.cap / MB:.0f} MB/s)")
z = L.CopyPace(); z.block(B, 0.001); z.block(B, 8.0)       # one cache-burst block makes best_rate huge
for _ in range(16): z.block(B, 0.5)
check(z.cap is None, "16 healthy blocks in a row drop the cap even after a burst")
import threading, time
ev = threading.Event(); threading.Timer(0.3, ev.set).start(); t0 = time.time()
try:
    L.CopyPace().sleep(30, ev.is_set); ok = False
except L.Aborted:
    ok = time.time() - t0 < 0.8
check(ok, f"stop() cuts a pacing wait short ({time.time() - t0:.2f}s)")

if Path("/proc/self/mountinfo").is_file():
  check(L.mount_fstype("/proc") == "proc" and L.mount_fstype("/proc/self") == "proc", "mount_fstype: longest mount wins")
  check(L.mount_fstype("/nonexistent/x/y") == L.mount_fstype("/"), "mount_fstype: a missing path falls back to its mount")

d = Path(tempfile.mkdtemp(dir=os.environ.get("LMAP_TEST_TMP")))
mi = d / "mountinfo"
mi.write_text("28 1 254:1 / / rw - ext4 /dev/vda1 rw\n"
              "40 28 0:50 / /srv/share rw - autofs systemd-1 rw\n"
              "41 40 0:51 / /srv/share rw - cifs //server/share rw\n"
              "42 28 0:52 / /srv/share2 rw - autofs systemd-1 rw\n"
              "43 28 0:53 / /srv/my\\040disk rw - nfs4 host:/x rw\n"
              "44 28 0:54 / /srv/a\\134b\\011c rw - smb3 //h/s rw\n")
mt = lambda q: L.mount_fstype(q, table=mi)
check(mt("/srv/share/a/b") == "cifs" and mt("/srv/share") == "cifs", "network share under an autofs trigger: cifs")
check(mt("/srv/share2/x") == "ext4", "autofs trigger alone: not a network type")
check(mt("/srv/sharex") == "ext4" and mt("/srv/my disk/f") == "nfs4", "prefix needs a '/' boundary; escaped space")
check(mt("/srv/a\\b\tc/f") == "smb3", "backslash and tab escapes decoded")
check({"cifs", "nfs4"} <= L.NET_FS and "ext4" not in L.NET_FS, "NET_FS holds the kernel names")
src = d / "a.bin"; src.write_bytes(os.urandom(130 * MB))
class Spy(L.CopyPace):
    def __init__(self): super().__init__(); self.n, self.slept = 0, 0.0
    def block(self, n, secs): self.n += 1; return 0.01
    def sleep(self, s, stop=None): self.slept += s
sp = Spy(); L.copy_synced(src, d / "b.bin", pace=sp)
h = lambda f: hashlib.sha256(f.read_bytes()).hexdigest()
check(h(src) == h(d / "b.bin") and sp.n == 2 and abs(sp.slept - 0.02) < 1e-9, f"paced copy: identical bytes, 2 x 64 MB blocks timed ({sp.n})")
L.copy_synced(src, d / "c.bin")
check(h(src) == h(d / "c.bin"), "plain copy (local disk, no pacing): identical bytes")
__import__("shutil").rmtree(d)
print("ALL OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
