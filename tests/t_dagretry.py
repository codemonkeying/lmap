#!/usr/bin/env python3
"""A step that hits a storage read error (network share reconnect) is retried with growing waits; a real bug is not. Offline."""
import importlib.machinery, importlib.util, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
L.NAS_BLIP_WAITS = (0.01, 0.01, 0.01)
ctx = types.SimpleNamespace(log=lambda m: None)
n = {"io": 0, "bug": 0}
def io():
    n["io"] += 1
    if n["io"] < 3:
        raise L.CliError(L.E_FAIL, "gdalwarp failed: ERROR 1: dem.tif, band 1: IReadBlock failed at X offset 243")
    return {"ok": 1}
def bug():
    n["bug"] += 1; raise L.CliError(L.E_FAIL, "TypeError: unsupported operand")
d = L.Dag(ctx, width=2); d.add("io", io); d.add("bug", bug); d.add("after", lambda: 1, deps=["io"])
r = d.run()
check(r["io"].state == "done" and n["io"] == 3 and r["after"].state == "done", "storage read error retried until it worked; dependants ran")
check(r["bug"].state == "failed" and n["bug"] == 1, "a real bug fails at once, no retry")
check(L.nas_blip(OSError(5, "Input/output error")) and not L.nas_blip(ValueError("x")), "nas_blip: EIO yes, ValueError no")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
