#!/usr/bin/env python3
"""0.79 fixes for failures seen only under load:
1. write_sums re-hashes a file rewritten with the same size within seconds (old 2.5 s mtime window kept a stale hash).
2. au contours retries a failing cell (a one-off OOM kill / network-share read error no longer fails the whole run)."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
_o.environ["LMAP_BACKOFF"] = "0"
import importlib.machinery, importlib.util, tempfile, shutil, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

class C:
    def log(self, m): pass
T = Path(tempfile.mkdtemp(prefix="lmap-tff-"))
try:
    f = T / "maps" / "a.txt"; f.parent.mkdir(); f.write_text("AAAA"); old = time.time() - 3600
    _o.utime(f, (old, old)); L.write_sums(C(), T)
    st = f.stat(); f.write_text("BBBB"); _o.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))   # rsync -t repair
    n, hashed = L.write_sums(C(), T, written={"maps/a.txt"}, since=time.time())
    check((T / "SHA256SUMS").read_text().split()[0] == L.sha256(f), "repaired file lmap wrote: re-hashed (no ctime needed: FAT-safe)")
    n, hashed = L.write_sums(C(), T, since=time.time())
    check(hashed == 0, "untouched old file: cached hash reused")
    g = T / "README.txt"; g.write_text("new")
    n, hashed = L.write_sums(C(), T, since=time.time() - 1)
    check(hashed == 1, "file modified during this run: hashed")
    (T / "_at").write_text("x"); L.write_sums(C(), T); n, hashed = L.write_sums(C(), T)
    check(hashed == 0, "a file named _at doesn't break the cache")
    (T / ".lmap-sums.json").write_text('{"a.txt": {"size": 4, "mtime": 1, "sha256": "x"}}')
    n, hashed = L.write_sums(C(), T); check(hashed == n, "old cache format: everything hashed once")
finally:
    shutil.rmtree(T, ignore_errors=True)

src = (HERE / "lmaplib" / "05_area.py").read_text()
check("for k in range(3):" in src and "retry {k + 1}/2" in src and "if DRAIN.wait(RetryPolicy(3, 5, 30).delay(k))" in src, "au contours cell loop retries")
print("FAILED: " + "; ".join(fails) if fails else "OK")
raise SystemExit(1 if fails else 0)
