#!/usr/bin/env python3
"""0.48 scratch cleanup: lmap clean (what it deletes / keeps), build_cog refuses to build on the data root when
scratch is short, tides index built in scratch. Offline, temp dirs only."""
import argparse, importlib.machinery, importlib.util, os, subprocess, sys, tempfile, time
from pathlib import Path

ld = importlib.machinery.SourceFileLoader("lmap", str(Path(__file__).resolve().parent.parent / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = 0


def until(pred, t=20):
    """Wait for a condition (a child taking its lock / starting), not a fixed sleep: a busy machine is slower."""
    end = time.time() + t
    while not pred() and time.time() < end:
        time.sleep(0.05)
    return pred()


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


T = Path(tempfile.mkdtemp())
L.STATE = T / "state"; (L.STATE / "locks").mkdir(parents=True)
root = T / "root"; (root / "data").mkdir(parents=True)


class C:
    pass


ctx = C()
ctx.root, ctx.tmp, ctx.tiles_dir, ctx.prefix = root, str(T / "tmp"), None, ""
ctx.sources = {"qld-roads-state": {"kind": "arcgis", "dest": "data/qld-roads.gpkg"},
               "sat-qld-2m4": {"kind": "wms-tiles", "dest": str(T / "tmp" / f"lmap-{os.getuid()}" / "tiles" / "sat"),
                               "out": "data/sat.cog.tif"},
               "sat-qld-2m4-2025": {"kind": "wms-tiles", "dest": "x"}}
ctx.a = argparse.Namespace(dry_run=True, yes=False, resume=False, quiet=True, verbose=False)
ctx.log = ctx.vlog = lambda *a: None
ctx.scratch = lambda: L.Ctx.scratch(ctx)
ctx.tiles = lambda: L.Ctx.tiles(ctx)
ctx.path = lambda r: root / r
S = ctx.scratch()
L.best_raster = lambda c, sid: None

for n in ("area-x-osm.gpkg", "clip-a-1-2-3.geojson", "hillshade-30m.cog.999999.part.tif", "mine.txt",
          "qld-roads-state.part.gpkg", "qld-roads-state.resume.json", "sat-qld-2m4-2025-u1.cache"):
    (S / n).write_text("x")
(S / "sat-qld-2m4-u1.tiles").mkdir(); (S / "sat-qld-2m4-u1.tiles" / "a.png").write_text("x")
live = subprocess.Popen(["bash", "-c", f"exec -a {Path(L.__file__).resolve()} sleep 30"])
until(lambda: L._lmap_pid(live.pid))                 # after the exec: argv[0] is the lmap script
(S / f"area-y.{live.pid}.part.tif").write_text("x")

plan = {p.name: (a, w) for p, b, a, w in L.clean_plan(ctx)}
check(plan["area-x-osm.gpkg"][0] == "delete" and plan["clip-a-1-2-3.geojson"][0] == "delete"
      and plan["hillshade-30m.cog.999999.part.tif"][0] == "delete", "dead runs' leftovers: delete")
check(plan["mine.txt"][0] == "keep", "a file lmap didn't make: kept")
check(plan[f"area-y.{live.pid}.part.tif"][0] == "keep", "a running lmap's file (pid in the name): kept")
check(plan["qld-roads-state.part.gpkg"][0] == "resume" and plan["sat-qld-2m4-u1.tiles"][0] == "resume",
      "resume points: kept unless --resume")
check(plan["sat-qld-2m4-2025-u1.cache"] == ("resume", "resume point for sat-qld-2m4-2025"),
      "longer source id wins (sat-qld-2m4-2025 not taken for sat-qld-2m4)")

(root / "data" / "qld-roads.gpkg").write_text("done")          # finished AFTER the resume files were made
old = time.time() - 3600
for n in ("qld-roads-state.part.gpkg", "qld-roads-state.resume.json"):
    os.utime(S / n, (old, old))
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx)}
check(plan["qld-roads-state.part.gpkg"] == "delete", "resume point older than the finished file: delete")

lk = L.Lock(L.STATE / "locks" / "qld-roads-state.lock"); lk.__enter__()
fake = subprocess.Popen([sys.executable, "-c", "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR);"
                         "fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(30)", str(L.STATE / "locks" / "other.lock")]) \
    if (L.STATE / "locks" / "other.lock").write_text("") is not None else None
lk.__exit__(None, None, None)
until(lambda: L.lock_held(L.STATE / "locks" / "other.lock"))
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx)}
check(plan["area-x-osm.gpkg"] == "keep", "another lmap job holds a lock: unnamed leftovers kept")
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx, mine=("other",))}
check(plan["area-x-osm.gpkg"] == "delete", "the caller's own lock (refresh) doesn't block it")
fake.kill(); fake.wait()

ex = subprocess.Popen([sys.executable, "-c", "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT);"
                       "fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(30)", str(L.STATE / "export.lock")])
until(lambda: L.lock_held(L.STATE / "export.lock"))
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx)}
check(plan["area-x-osm.gpkg"] == "keep", "export.lock (top-level STATE) held: unnamed leftovers kept")
ex.kill(); ex.wait()

q = subprocess.Popen([sys.executable, "-c", "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT);"
                      "fcntl.flock(fd,fcntl.LOCK_EX);time.sleep(30)", str(L.STATE / "locks" / "queue.lock")])
until(lambda: L.lock_held(L.STATE / "locks" / "queue.lock"))
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx)}
check(plan["area-x-osm.gpkg"] == "delete", "a long-running queue supervisor alone doesn't block the clean")
q.kill(); q.wait()

(S / "sat-qld-2m4-2025.xml").write_text("x"); (S / "sat-qld-2m4-u9.xml").write_text("x")
got = sorted(p.name for p in L.own_glob(ctx, S, "sat-qld-2m4", "*.xml"))
check(got == ["sat-qld-2m4-u9.xml"], f"own_glob skips a longer id's '<id>.xml' ({got})")
(S / "sat-qld-2m4-2025.xml").unlink(); (S / "sat-qld-2m4-u9.xml").unlink()

xt = T / "ext-tiles"; (xt / "my-photos").mkdir(parents=True)
ctx.tiles_dir = str(xt); ctx.tiles = lambda: xt
plan = {p.name: a for p, b, a, w in L.clean_plan(ctx)}
check(plan.get("my-photos") == "keep", "tiles dir outside scratch: unknown folder kept")
ctx.tiles = lambda: L.Ctx.tiles(ctx); ctx.tiles_dir = None

try:
    L.cmd_clean(ctx.__class__.__new__(C) if False else ctx)
    check(True, "dry-run returns")
except L.CliError:
    check(False, "dry-run returns")
ctx.a.dry_run = False
try:
    L.cmd_clean(ctx); check(False, "no --yes: refused")
except L.CliError as e:
    check(e.code == L.E_REFUSED, "no --yes: refused")
ctx.a.yes = True
r = L.cmd_clean(ctx)
left = sorted(p.name for p in S.iterdir())
check("area-x-osm.gpkg" not in left and "mine.txt" in left and f"area-y.{live.pid}.part.tif" in left
      and "sat-qld-2m4-u1.tiles" in left and r["ok"], f"--yes: deleted only what it listed ({left})")
ctx.a.resume = True; L.cmd_clean(ctx)
left = sorted(p.name for p in S.iterdir())
check("sat-qld-2m4-u1.tiles" not in left and "mine.txt" in left, "--resume: resume points gone, other files stay")
live.kill(); live.wait()

# build_cog: short scratch -> refuse (no fallback to building beside out)
class Slot:
    def __init__(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def free(self, where): return 1e9
    def reserve(self, *a): pass
    def threads(self): return 1
L.BuildSlot = Slot
L.cog_co = lambda c, src: ([], "deflate")
out = root / "data" / "x.cog.tif"
try:
    L.build_cog(ctx, "src.vrt", out, 50e9); check(False, "short scratch: refused")
except L.CliError as e:
    check("scratch" in e.msg and not any(out.parent.glob("*.part.tif")), "short scratch: refused, nothing on the data root")

# tides index built in the given tmp, not /tmp
seen = []
real_run = L.subprocess.run
def fake_run(cmd, *a, **k):
    seen.append(cmd); Path(cmd[5]).write_text("g"); return argparse.Namespace(returncode=0)
L.subprocess.run = fake_run
L.install_file = lambda p, o: o.write_text(Path(p).read_text())
td = T / "tides"; td.mkdir()
L.tides_index(td, S)
L.subprocess.run = real_run
check(seen and str(S) in seen[0][5], f"tides index temp in scratch ({seen[0][5] if seen else None})")

# STATE growth - old refresh logs, files of sources gone from the catalog, unused lock files
import fcntl, threading
old_t = time.time() - 40 * 86400
def mk(p, t=old_t, d=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir() if d else p.write_text("x"); os.utime(p, (t, t)); return p
runs = [mk(L.STATE / "refresh" / f"2026010{i}-000000", d=True) for i in range(1, 8)]
gone = mk(L.STATE / "gone-src.json"); gone_log = mk(L.STATE / "gone-src.log.2")
keep = [mk(L.STATE / n) for n in ("queue.json", "queue.log", "qld-gov.log", "au-contours.json", "qld-roads-state.log",
                                  "gone-src.start")]
lk_late = mk(L.STATE / "locks" / "map-late.lock")
fresh = mk(L.STATE / "fresh-gone.json", t=time.time())
lk_old = mk(L.STATE / "locks" / "map-old.lock"); lk_build = mk(L.STATE / "locks" / "build.3.lock")
lk_held = mk(L.STATE / "locks" / "out-busy.lock")
hfd = os.open(lk_held, os.O_RDWR); fcntl.flock(hfd, fcntl.LOCK_EX)
plan = {x: (a_, w) for x, _, a_, w in L.clean_plan(ctx)}
dl = {x for x, (a_, _) in plan.items() if a_ == "delete"}
check(dl >= set(runs[:2]) and not dl & set(runs[2:]), "refresh logs: the newest 5 are kept, older ones deleted")
check({gone, gone_log, lk_old} <= dl, "files of a source gone from the catalog and an unused lock: deleted")
check(not dl & {*keep, fresh, lk_build, lk_held},
      "queue/tier/au-contours/catalog files, start gates, recent files, build slots and held locks: kept")
check(lk_late in dl, "map-late.lock planned for delete")
lfd = os.open(lk_late, os.O_RDWR); fcntl.flock(lfd, fcntl.LOCK_EX)     # taken after the plan: must survive
r = L.cmd_clean(ctx); os.close(lfd)
check(r["ok"] and not lk_old.exists() and lk_held.exists() and not gone.exists() and not gone_log.exists()
      and not runs[0].exists() and runs[-1].exists() and all(k.exists() for k in keep),
      f"clean --yes removed exactly those ({r.get('failed')})")
lfd = os.open(lk_late, os.O_RDWR); fcntl.flock(lfd, fcntl.LOCK_EX)
check(lk_late.exists() and not L._lock_unlink(lk_late) and lk_late.exists(),
      "a lock held between plan and delete is not deleted")
os.close(lfd); os.utime(lk_late, (old_t, old_t))
check(L._lock_unlink(lk_late) and not lk_late.exists(), "a free, old lock is deleted")
lk_late = mk(L.STATE / "locks" / "map-late.lock")
os.utime(lk_late, None); lfd = os.open(lk_late, os.O_RDWR)
check(not L._lock_unlink(lk_late) and lk_late.exists(), "a lock used recently is not deleted even if free")
os.close(lfd)
os.close(hfd)

# a Lock waiting on a file that clean unlinks (while holding it) re-opens the new file, not the orphan
lp = mk(L.STATE / "locks" / "area-race.lock"); hfd = os.open(lp, os.O_RDWR); fcntl.flock(hfd, fcntl.LOCK_EX)
got = {}
waiting = []
class W:
    log = staticmethod(lambda *a: waiting.append(1))      # Lock says it is waiting: it has the old fd open
def take():
    lk = L.Lock(lp, wait=W()); lk.__enter__(); got["same"] = L._same_inode(lp, lk.fd); lk.__exit__(None, None, None)
th = threading.Thread(target=take); th.start(); check(until(lambda: waiting), "the Lock is waiting on the old file")
lp.unlink(); os.close(hfd); th.join(30)
check(got.get("same") is True and lp.exists(), "a waiting Lock moves off an unlinked lock file onto the new one")

# area/map folders on the data root - orphan signatures and dead runs' half-written files
md = root / "maps" / "au" / "qld" / "m1"; md.mkdir(parents=True); (md / "area.json").write_text("{}")
orphan = mk(md / "contours-lidar.gpkg.lmap-sig"); orphan.write_text('{"in": []}'); os.utime(orphan, (old_t, old_t))
empty_sig = mk(md / "qld-parks.gpkg.lmap-sig"); empty_sig.write_text("EMPTY {}")
paired = mk(md / "osm.gpkg.lmap-sig"); mk(md / "osm.gpkg"); qgz_sig = mk(md / "m1.qgz.lmap-sig")
dead = mk(md / ".dem.tif.99999999.140000000000.tmp")               # above pid_max: never a running process
stale_sig = mk(md / "grid.gpkg.lmap-sig"); stale_sig.write_text("STALE"); os.utime(stale_sig, (old_t, old_t))
users = [mk(md / n) for n in ("x.partial.tif", ".notes.tmpl", "trail.parts.gpkg")]     # not lmap's: never touched
for p_ in ("map-", "area-image-", "area-export-"):
    lkd = root / "maps" / "au" / "qld" / f"lk{p_}"; lkd.mkdir(); (lkd / "area.json").write_text("{}"); mk(lkd / "a.gpkg.lmap-sig")
part_old = mk(md / "print-topo-25k.pdf.part"); part_new = mk(md / "x.gpkg.part", t=time.time())
ad = root / "areas" / "au" / "qld" / "a1"; ad.mkdir(parents=True); (ad / "area.json").write_text("{}")
busy_sig = mk(ad / "dem.tif.lmap-sig"); busy_sig.write_text("{}")
bfd = os.open(mk(L.STATE / "locks" / "area-a1.lock", t=time.time()), os.O_RDWR); fcntl.flock(bfd, fcntl.LOCK_EX)
plan = {x: a_ for x, _, a_, _ in L.clean_plan(ctx)}
lfds = []
for p_ in ("map-", "area-image-", "area-export-"):
    fd_ = os.open(mk(L.STATE / "locks" / f"{p_}lk{p_}.lock", t=time.time()), os.O_RDWR); fcntl.flock(fd_, fcntl.LOCK_EX)
    lfds.append(fd_)
plan = {x: a_ for x, _, a_, _ in L.clean_plan(ctx)}
check(not any(x.parent.name.startswith("lk") for x in plan), "folders under a map / image / export lock: skipped")
check(plan.get(stale_sig) == "delete" and not set(users) & set(plan), "STALE orphan sig deleted; user files ignored")
check(plan.get(orphan) == "delete" and plan.get(dead) == "delete" and plan.get(part_old) == "delete",
      "orphan signature and dead runs' .tmp/.part in a map folder: deleted")
check(plan.get(part_new) == "keep" and not {empty_sig, paired, qgz_sig, busy_sig} & set(plan),
      "EMPTY / paired / project signatures, fresh .part and a folder being built: left alone")
back = mk(md / "contours-lidar.gpkg")              # its file came back after the plan: the sig must stay
check(not L._root_still(orphan) and not L._root_still(busy_sig), "delete-time re-check: file back / folder busy -> keep")
back.unlink(); check(L._root_still(orphan), "delete-time re-check: still an orphan -> delete")
r = L.cmd_clean(ctx); os.close(bfd)
[os.close(x) for x in lfds]
check(r["ok"] and not orphan.exists() and not dead.exists() and empty_sig.exists() and paired.exists()
      and busy_sig.exists(), "clean --yes on the root rows")

sys.exit(1 if fails else 0)
