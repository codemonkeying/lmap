#!/usr/bin/env python3
"""hot_copy: a big input on another filesystem is copied once into <scratch>/hot and reused while its size + mtime
match; a changed source is copied again; a source on scratch's own filesystem is read in place; no room -> the
source itself. Usage: t_hotcopy.py <scratch>   (the "other filesystem" is faked by patching _dev)"""
import importlib.machinery, importlib.util, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
os.environ.update(LMAP_ROOT=str(tmp / "root"), LMAP_TMP=str(tmp / "scr"), XDG_STATE_HOME=str(tmp / "state"),
                  XDG_CONFIG_HOME=str(tmp / "cfg"))
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


(tmp / "root").mkdir()
ctx = L.Ctx(L.build_parser().parse_args(["status"]))
nas = tmp / "nas"; nas.mkdir(); src = nas / "big.osm.pbf"; src.write_bytes(os.urandom(3_000_000))
scr = ctx.scratch()
real_dev = L._dev
L._dev = lambda p: 999 if str(p).startswith(str(nas)) else real_dev(p)      # nas/ = "another filesystem"

import fcntl


def hc(p):
    with L.hot_copy(ctx, p) as q:
        return q


with L.hot_copy(ctx, src) as a:
    check(a != src and a.parent == scr / "hot" and a.read_bytes() == src.read_bytes(), "copied into scratch/hot, identical")
    o = os.open(a, os.O_RDONLY)
    try:
        fcntl.flock(o, fcntl.LOCK_EX | fcntl.LOCK_NB); held = False
    except OSError:
        held = True
    os.close(o)
    check(held, "the copy is locked (shared) while in use")
check(not a.exists(), "deleted when its last reader is done (scratch doesn't accumulate)")
with L.hot_copy(ctx, src) as r1:                               # a reader holds the old version...
    old = r1.read_bytes()
    src.write_bytes(os.urandom(3_000_000)); os.utime(src, ns=(time.time_ns() + 10**9,) * 2)
    with L.hot_copy(ctx, src) as r2:                           # ...while the source changes
        check(r2 != r1 and r2.read_bytes() == src.read_bytes(), "changed source -> a new version")
    check(r1.exists() and r1.read_bytes() == old, "the old version stays while a reader holds it")
check(not r1.exists() and not list((scr / "hot").glob("*.pbf")), "every version dropped once nobody reads it")
twin = tmp / "nas2" / "big.osm.pbf"; twin.parent.mkdir(); twin.write_bytes(os.urandom(1000))
L._dev = lambda p: 999 if "/nas" in str(p) else real_dev(p)
with L.hot_copy(ctx, twin) as t1, L.hot_copy(ctx, src) as t2:
    check(t1 != t2 and t1.read_bytes() == twin.read_bytes() and t2.read_bytes() == src.read_bytes(),
          "two sources with the same file name don't collide")
check(not list((scr / "hot").glob("*.part")), "no .part left")
loc = scr / "local.pbf"; loc.write_bytes(b"x")
check(hc(loc) == loc, "source on scratch's filesystem -> read in place")
real_free = L.free_bytes
L.free_bytes = lambda p, raw=False: 0
src2 = nas / "other.pbf"; src2.write_bytes(b"y" * 1000)
check(hc(src2) == src2 and not list((scr / "hot").glob("*other.pbf")), "no room -> the source itself")
L.free_bytes = real_free
check(hc(nas / "missing.pbf") == nas / "missing.pbf", "missing source -> returned as is (caller errors)")
# clean: an unused-for-weeks copy is deleted, a fresh one kept, one in use kept
with L.hot_copy(ctx, twin) as t_:
    cold = t_.with_name(t_.name.split("-")[0] + "-cafe1234-big.osm.pbf")
cold.write_bytes(b"c"); os.utime(cold, (time.time() - 30 * 86400,) * 2)   # left by a killed run
with L.hot_copy(ctx, src) as busy:
    os.utime(busy, (time.time() - 30 * 86400,) * 2)
    plan = {str(p): a for p, _, a, _ in L.clean_plan(ctx)}
    check(plan.get(str(busy)) == "keep", "clean keeps a copy in use, however old")
check(plan.get(str(cold)) == "delete", "clean deletes a copy unused for weeks")
hd = scr / "hot"; lk = next(hd.glob("*.lock")); pre = lk.name[:-5]
stray = hd / f"{pre}-deadbeef-x.pbf.part"; stray.write_bytes(b"z")
lfd = os.open(lk, os.O_RDWR); fcntl.flock(lfd, fcntl.LOCK_EX)          # a copier at work
plan = {str(p): a for p, _, a, _ in L.clean_plan(ctx)}
check(plan.get(str(stray)) == "keep" and plan.get(str(lk)) == "keep", "clean leaves a .part being copied (and locks)")
fcntl.flock(lfd, fcntl.LOCK_UN); os.close(lfd)
plan = {str(p): a for p, _, a, _ in L.clean_plan(ctx)}
check(plan.get(str(stray)) == "delete", "clean deletes a dead copy's .part")
with L.hot_copy(ctx, src) as busy2:                            # in use when clean deletes: kept
    check(L._hot_unlink(busy2) is False and busy2.exists(), "clean's delete step skips a copy in use")
check(not busy2.exists(), "removed once unused")
with L.hot_copy(ctx, src, max_bytes=1000) as capped:           # over the size cap: read in place
    check(capped == src, "an input over the size cap is read in place")
with L.hot_copy(ctx, src, max_bytes=src.stat().st_size) as atcap:
    check(atcap != src, "an input exactly at the size cap is copied")
wal = Path(str(src) + "-wal"); wal.write_bytes(b"x")
with L.hot_copy(ctx, src) as walp:
    check(walp == src, "a source with an unmerged SQLite -wal is read in place")
wal.unlink()
try:                                                            # an error inside the with reaches the caller
    with L.hot_copy(ctx, loc):
        raise OSError("boom")
    check(False, "caller's OSError propagates")
except OSError as e:
    check(str(e) == "boom", "caller's OSError propagates (not swallowed)")
print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
