#!/usr/bin/env python3
"""queue add --refresh: the queue re-gets a source whose file already exists, keeps the flag across a
stop/run, clears it once done; a plain add still says 'unchanged'. Real queue_worker, do_fetch's kind
replaced by a counter. Private state dir (XDG_STATE_HOME), the real queue is not touched. No args."""
import argparse, importlib.machinery, importlib.util, os, tempfile, types
from pathlib import Path
d = Path(tempfile.mkdtemp()); os.environ["XDG_STATE_HOME"] = str(d / "state")
HERE = Path(__file__).resolve().parent.parent
_ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap")); L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", _ld)); _ld.exec_module(L)
L.STATE.mkdir(parents=True, exist_ok=True)
assert str(L.QUEUE).startswith(str(d)), L.QUEUE
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg, flush=True)
    ok or fails.append(msg)

out = d / "x.cog.tif"; out.write_text("old")
calls = []
def fake_fetch(ctx, s, plan):
    calls.append(ctx.a.refresh); out.write_text("new"); return {"built": str(out)}
L.KINDS["fake"] = (None, fake_fetch)
S = {"id": "fake-src", "kind": "fake", "url": "http://example.invalid/x", "out": str(out)}
logs = []
ctx = types.SimpleNamespace(sources={"fake-src": S}, src=lambda i: S, path=Path, scratch=lambda: d, prefix="",
                            log=logs.append, vlog=lambda *a: None, webp=True, shrink=1,
                            a=argparse.Namespace(ids=["fake-src"], dry_run=False, refresh=False, yes=True))
L.finish_out = lambda c, s: {}
L.lock_path = lambda c, s: d / "fake.lock"
L.clean_stale = lambda c, s: 0
L.space_wait = lambda c, s: None

def run_queue():
    todo = list(L.queue_load()["ids"]); L.queue_worker(ctx, "h", todo, {"h"})

# 1. plain add: the existing file is kept (same as before)
L.cmd_queue_add(ctx); run_queue()
check(calls == [] and out.read_text() == "old", f"plain add: not re-got (calls {calls})")
check(L.queue_load()["results"]["fake-src"]["state"] == "done", "plain add: marked done (unchanged)")

# 2. add --refresh: re-got, flag stored, then cleared
ctx.a.refresh = True
r = L.cmd_queue_add(ctx)
check(r.get("refresh") == ["fake-src"], f"add --refresh reports it: {r.get('refresh')}")
check(L.queue_load().get("refresh") == ["fake-src"], "refresh flag stored in queue file")
ctx.a.refresh = False                       # `queue run` itself has no --refresh: the flag comes from the file
run_queue()
check(calls == [True] and out.read_text() == "new", f"refresh add: re-got (calls {calls})")
check(L.queue_load().get("refresh") == [], "refresh flag cleared after done")

# 3. flag survives a stop: drained run keeps it, next run uses it
out.write_text("old"); calls.clear(); ctx.a.refresh = True; L.cmd_queue_add(ctx); ctx.a.refresh = False
L.DRAIN.set(); run_queue(); L.DRAIN.clear()
check(calls == [] and L.queue_load().get("refresh") == ["fake-src"], "stopped before start: flag kept")
run_queue()
check(calls == [True] and out.read_text() == "new", "next queue run still re-gets it")

# 4. remove clears it
ctx.a.refresh = True; L.cmd_queue_add(ctx); ctx.a.ids = ["fake-src"]; L.cmd_queue_remove(ctx)
check(L.queue_load().get("refresh") == [] and "fake-src" not in L.queue_load()["ids"], "remove clears the flag")

# 5. race: add --refresh lands while a PLAIN run of the same id is going -> it runs again, re-getting
out.write_text("old"); calls.clear(); ctx.a.refresh = False; L.cmd_queue_add(ctx)
def racing_fetch(c, s, plan):
    calls.append(c.a.refresh)
    if len(calls) == 1:                         # mid-run: someone adds it with --refresh
        ctx.a.refresh = True; L.cmd_queue_add(ctx); ctx.a.refresh = False
    if c.a.refresh:
        out.write_text("new")
    return {"built": str(out)}
L.KINDS["fake"] = (None, racing_fetch)
L.do_fetch = lambda c, s, plan: L.KINDS[s["kind"]][1](c, s, plan)   # bypass the exists-check for the first plain run
run_queue()
check(calls == [False, True] and out.read_text() == "new", f"refresh asked mid plain run: ran again re-getting {calls}")
check(L.queue_load().get("refresh") == [] and "fake-src" not in L.queue_load()["ids"], "then flag + id cleared")

# 6. CLI has the option
h = L.build_parser().parse_args(["queue", "add", "x", "--refresh"])
check(h.refresh is True, "lmap queue add --refresh parses")
print("ALL PASS" if not fails else f"{len(fails)} FAIL")
