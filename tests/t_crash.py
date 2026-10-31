#!/usr/bin/env python3
"""A child killed by a signal (ogr2ogr segfault in GDAL 3.10's Arrow path) is reported as "crashed (SIGSEGV)",
never as an empty "failed:"; and lmap turns the ogr2ogr Arrow path off for every child."""
import importlib.machinery, importlib.util, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)


class Ctx:
    def vlog(self, *a): pass
    def log(self, *a): pass


def err(cmd):
    try:
        L.run(cmd, Ctx()); return None
    except L.CliError as e:
        return str(e)


m = err(["sh", "-c", "kill -SEGV $$"])
check(m is not None and "sh crashed (SIGSEGV)" in m, f"segfault named: {m!r}")
m = err(["sh", "-c", "echo boom >&2; kill -KILL $$"])
check(m is not None and "crashed (SIGKILL): boom" in m, f"signal + stderr tail: {m!r}")
m = err(["sh", "-c", "echo bad >&2; exit 3"])
check(m is not None and "sh failed: bad" in m, f"plain non-zero exit unchanged: {m!r}")
import os
os.environ.pop("OGR2OGR_USE_ARROW_API", None)
L.Ctx(L.build_parser().parse_args(["status"]))          # the real start-up path, not a source grep
check(os.environ.get("OGR2OGR_USE_ARROW_API") == "NO", "Ctx turns the ogr2ogr Arrow path off for children")
m = err(["sh", "-c", 'test "$OGR2OGR_USE_ARROW_API" = NO'])
check(m is None, f"a child run() sees it: {m!r}")
print("FAILED: " + "; ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
