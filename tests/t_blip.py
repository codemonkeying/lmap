#!/usr/bin/env python3
"""A network-share blip must not throw away hours of work: the network share copy retries; a verified build whose copy still fails is
kept in scratch and the next run installs it without rebuilding; `refresh --resume` skips steps a refresh already
finished. Offline. Usage: t_blip.py <tmpdir>"""
import importlib.machinery, importlib.util, json, os, sys, tempfile, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
os.environ["XDG_STATE_HOME"] = str(T / "state")
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
L.NAS_RETRY_S = (0, 0, 0)
logs = []; ctx = types.SimpleNamespace(log=logs.append, scratch=lambda: T / "sc")
(T / "sc").mkdir(); (T / "nas").mkdir()
src = T / "sc" / "built.tif"; src.write_bytes(b"x" * 1000)
real = L.copy_resumable; n = [0]
def flaky(k):
    def f(a, b, **kw):
        n[0] += 1
        if n[0] <= k:
            raise OSError(5, "Input/output error")
        return real(a, b, **kw)
    return f
L.copy_resumable = flaky(2); L.nas_copy_retry(ctx, src, T / "nas" / "a.tif")
check((T / "nas" / "a.tif").read_bytes() == src.read_bytes() and n[0] == 3, "network-share copy: 2 blips, 3rd try lands")
n[0] = 0; L.copy_resumable = flaky(99)
try:
    L.nas_copy_retry(ctx, src, T / "nas" / "b.tif"); check(False, "4 failures raise")
except OSError:
    check(n[0] == 4 and not (T / "nas" / "b.tif").exists(), "a long outage gives up after 4 tries, no half file")
# kept build: the next build_cog installs it, never builds
out = T / "nas" / "big.tif"; kd = T / "sc" / "kept"; kd.mkdir()
L.cog_co = lambda c, s: ([], "webp")
import hashlib; kn = hashlib.sha1(str(out).encode()).hexdigest()[:12] + "-big.tif"
(kd / kn).write_bytes(b"y" * 500)
(kd / (kn + ".json")).write_text(json.dumps({"out": str(out), "src": "/x/src.vrt", "need": 123, "label": "webp"}))
L.copy_resumable = real
L._run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt"))
L.BuildSlot = None                                     # would fail if the build path were entered
r = L.build_cog(ctx, "/x/src.vrt", out, 123)
check(r == "webp" and out.read_bytes() == b"y" * 500 and not (kd / kn).exists(), "kept verified build installed next run, no rebuild")
(kd / kn).write_bytes(b"z"); (kd / (kn + ".json")).write_text(json.dumps({"out": str(out), "src": "/x/OTHER.vrt", "need": 123, "label": "webp"}))
try:
    L.build_cog(ctx, "/x/src.vrt", out, 123); check(False, "mismatched kept file ignored")
except Exception as e:
    check(("NoneType" in str(e) or "not callable" in str(e)) and not (kd / kn).exists(), "a kept file for other inputs is not used, and deleted (build path entered)")
# refresh --resume
rd = L.STATE / "refresh" / "20260101-000000"; rd.mkdir(parents=True)
L.refresh_mark_done(rd, "a", ["data", "fetch", "a", "--refresh"]); L.refresh_mark_done(rd, "c", ["area", "create", "c", "--bbox=1"])
plan = [("a", ["data", "fetch", "a", "--refresh"], [], ""), ("b", ["x"], [], ""), ("c", ["area", "create", "c", "--bbox=2"], [], "")]
sk = L.refresh_last_done(plan)
check(set(sk) == {"a"}, f"resume skips finished steps only, and not one whose command changed ({sorted(sk)})")
# resumable copy: a partial dst is continued, a torn last chunk is redone, a wrong dst restarts
L.COPY_CHUNK = 1000
big = T / "sc" / "big.bin"; big.write_bytes(os.urandom(10500))
dp = T / "nas" / "big.bin.part"; dp.write_bytes(big.read_bytes()[:6400])
seen = []
L.copy_resumable(big, dp, prog=seen.append)
check(dp.read_bytes() == big.read_bytes() and seen and seen[0] > 5000, f"copy continued from ~5 kB, not 0 (first progress {seen[:1]})")
dp.write_bytes(big.read_bytes()[:5000] + b"\0" * 1400)    # torn tail
L.copy_resumable(big, dp); check(dp.read_bytes() == big.read_bytes(), "torn tail redone, file exact")
hb = bytearray(big.read_bytes()[:8400]); hb[1000:2000] = b"\0" * 1000; dp.write_bytes(bytes(hb))   # lost write mid-file
L.copy_resumable(big, dp); check(dp.read_bytes() == big.read_bytes(), "a hole before the resume point is caught (whole-file compare)")
c_ = T / "sc" / "c.vrt"; c_.write_text("<VRT/>"); i1 = L.src_ident(c_); c_.write_text("<VRX/>")
check(i1 != L.src_ident(c_), "checkpoint identity changes when the source changes at the same path/size")
dp.write_bytes(os.urandom(6400)); L.copy_resumable(big, dp); check(dp.read_bytes() == big.read_bytes(), "a different partial file: restarted from 0, exact")
# hot copy: deleted when the last reader is done
ctx2 = types.SimpleNamespace(log=print, vlog=lambda m: None, scratch=lambda: T / "sc", limits={"disk": 100})
L._dev = lambda p: 1 if str(p).startswith(str(T / "nas")) else 2
hs = T / "nas" / "in.gpkg"; hs.write_bytes(b"h" * 3000)
with L.hot_copy(ctx2, hs) as hp:
    inside = Path(hp) != hs and Path(hp).is_file()
check(inside and not Path(hp).exists(), f"hot copy used locally, then deleted (no accumulation): {inside}")
# clean: checkpoint with a live source = resume, without = delete
sc = T / "sc"; c1 = sc / ".aaa-x.ckpt"; c1.mkdir(); (c1 / "key.json").write_text(json.dumps({"src": str(big)}))
c2 = sc / ".bbb-y.ckpt"; c2.mkdir(); (c2 / "key.json").write_text(json.dumps({"src": str(T / "gone.vrt")}))
cx = types.SimpleNamespace(scratch=lambda: sc, sources={}, root=T / "nas", tiles_dir=None, tiles=lambda: sc / "tiles", tmp=str(T / "sc"), path=lambda x: Path(x))
try:
    pl = {str(x): a_ for x, _, a_, _ in L.clean_plan(cx)}
    check(pl.get(str(c1)) == "resume" and pl.get(str(c2)) == "delete", f"clean: live checkpoint kept, orphan deleted")
except Exception as e:
    check(False, f"clean_plan on checkpoints: {e!r}")
# a stop (Ctrl-C / drain) during the copy retries keeps the verified build + the partial network-share copy (open low)
class _Slot:
    def __init__(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def free(self, d): return 1e15
    def reserve(self, *a): pass
    def threads(self): return 2
out2 = T / "nas" / "stop.tif"; ctx3 = types.SimpleNamespace(log=lambda *a: None, vlog=lambda m: None, scratch=lambda: T / "sc",
                                                           limits={"disk": 100}, webp=True, quality="standard")
L.BuildSlot = _Slot; L.cog_co = lambda c, s: ([], "deflate"); L._sweep_build_tmp = lambda d: None
L.verify_cog = lambda *a, **k: None
L._run = lambda cmd, *a, **k: Path(cmd[-1]).write_bytes(b"b" * 4000)
def _cs(src, dst, stop=None, prog=None):
    Path(dst).write_bytes(b"b" * 1500); raise OSError("NAS blip")
L.copy_synced = _cs
def _ncr(*a, **k):
    Path(str(out2) + ".part").write_bytes(b"b" * 2500); raise L.Aborted()
L.nas_copy_retry = _ncr
_od = L.os.stat
def _st(p, *a, **k):                                   # out2's folder reads as another disk (the network share)
    r = _od(p, *a, **k)
    if str(p) != str(out2.parent):
        return r
    v = list(r); v[2] = r.st_dev + 1; return L.os.stat_result(v)
L.os.stat = _st
try:
    L.build_cog(ctx3, "/x/src2.vrt", out2, 4000); check(False, "stop raised")
except L.Aborted:
    pass
finally:
    L.os.stat = _od
kn2 = hashlib.sha1(str(out2).encode()).hexdigest()[:12] + "-stop.tif"
check((T / "sc" / "kept" / kn2).is_file(), "stopped during copy retry: the verified build is kept")
check(Path(str(out2) + ".part").is_file(), "stopped during copy retry: the partial network-share copy is kept for the next run")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
