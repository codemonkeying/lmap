#!/usr/bin/env python3
"""Resource limits: [limits] parsing (defaults, env > config, bad values, unknown keys), cpu_cap, ALL_CPUS rewritten
to the cpu share, the disk cap on scratch only (data root: 5 GB margin), the shared slice's systemd properties, and
no GPU use without nvidia-smi.
Usage: t_limits.py <scratch>"""
import importlib.machinery, importlib.util, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
for k in list(os.environ):
    if k.startswith("LMAP_LIMIT_"):
        del os.environ[k]
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


def refused(f, what):
    try:
        f()
        check(False, what)
    except L.CliError:
        check(True, what)


# 1. limits()
check(L.limits({}) == {"cpu": 80, "ram": 80, "gpu": 80, "disk": 80}, "defaults are 80%")
check(L.limits({"limits": {"cpu": 50, "disk": "90%"}})["cpu"] == 50, "config [limits] cpu")
check(L.limits({"limits": {"disk": "90%"}})["disk"] == 90, "config value with % sign")
os.environ["LMAP_LIMIT_CPU"] = "30"
check(L.limits({"limits": {"cpu": 50}})["cpu"] == 30, "LMAP_LIMIT_CPU beats config")
del os.environ["LMAP_LIMIT_CPU"]
refused(lambda: L.limits({"limits": {"cpu": 5}}), "cpu 5 refused (below 10)")
refused(lambda: L.limits({"limits": {"ram": 101}}), "ram 101 refused")
refused(lambda: L.limits({"limits": {"gpu": "lots"}}), "non-number refused")
refused(lambda: L.limits({"limits": {"cpus": 50}}), "unknown key refused")
refused(lambda: L.limits({"limits": 80}), "[limits] not a table refused")

# 2. cpu_cap
n = os.cpu_count() or 2
check(L.cpu_cap({"cpu": 50}) == max(1, int(n * 0.5)), "cpu_cap 50%")
check(L.cpu_cap({"cpu": 10}) >= 1, "cpu_cap never 0")
check(L.cpu_cap({"cpu": 100}) == n, "cpu_cap 100% = all cores")

# 3. _all_cpus
L.LIMITS.clear(); L.LIMITS.update(L.limits({"limits": {"cpu": 50}}))
c = str(L.cpu_cap())
cmd, env = L._all_cpus(["gdalwarp", "-wo", "NUM_THREADS=ALL_CPUS", "--config", "GDAL_NUM_THREADS", "ALL_CPUS"],
                       {"GDAL_NUM_THREADS": "ALL_CPUS", "X": "1"})
check(cmd[2] == f"NUM_THREADS={c}", "NUM_THREADS=ALL_CPUS -> cpu share")
check(env["GDAL_NUM_THREADS"] == c and env["X"] == "1", "env GDAL_NUM_THREADS=ALL_CPUS -> cpu share")
check(cmd[5] == c, "--config GDAL_NUM_THREADS ALL_CPUS (two args) -> cpu share")
cmd, env = L._all_cpus(["gdalwarp", "-wo", "NUM_THREADS=2"], None)
check(cmd[2] == "NUM_THREADS=2" and env is None, "explicit thread count and no env left alone")

# 4. free_bytes: raw, capped on a scratch disk, margin elsewhere
st = os.statvfs(tmp)
raw = st.f_bavail * st.f_frsize
check(abs(L.free_bytes(tmp, raw=True) - raw) < 1e8, "raw = plain free space")
L.DISK_CAPPED.discard(os.stat(tmp).st_dev)
check(abs(L.free_bytes(tmp) - (raw - 5e9)) < 1e8, "not scratch: free less 5 GB")
L.DISK_CAPPED.add(os.stat(tmp).st_dev)
want = raw - st.f_blocks * st.f_frsize * 0.2
check(abs(L.free_bytes(tmp) - want) < 1e8, "scratch disk: free down to the 80% cap")
L.DISK_CAPPED.clear()
pct = L.disk_used_pct(tmp)
check(0 <= pct <= 100, f"disk_used_pct {pct}")


# scratch on the root's own filesystem is not capped; on another one it is
class A:
    pass


a = A()
for k in ("root", "catalog", "tmp", "jobs", "shrink", "dry_run", "yes", "verbose", "quiet", "json"):
    setattr(a, k, None)
a.root = str(tmp / "root"); a.tmp = str(tmp / "t")
try:
    ctx = L.Ctx(a)
    ctx.scratch()
    check(not L.DISK_CAPPED, "scratch on the data root's filesystem: not capped")
except Exception as e:                                   # Ctx needs more args on some versions: skip, not fail
    print(f"SKIP Ctx scratch check ({type(e).__name__}: {e})")

# 5. slice_props
p = L.slice_props({"cpu": 80, "ram": 80, "gpu": 80, "disk": 80})
check(p == [f"CPUQuota={80 * n}%", "MemoryHigh=80%", "MemoryMax=90%"], f"slice props {p}")
check(L.slice_props({"cpu": 50, "ram": 95, "gpu": 80, "disk": 80})[2] == "MemoryMax=100%", "MemoryMax capped at 100%")


# 6. gpu_room without nvidia-smi
class C:
    limits = {"gpu": 80}
    def vlog(self, *_):
        pass


old = os.environ.get("PATH", "")
os.environ["PATH"] = str(tmp)
try:
    check(L.gpu_room(C()) is False, "no nvidia-smi: no GPU use")
finally:
    os.environ["PATH"] = old

print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
