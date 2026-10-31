#!/usr/bin/env python3
"""job_stage: the last build-stage line of THIS run from a job log (status during the final join). Offline."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import importlib.machinery, importlib.util, sys, tempfile
from pathlib import Path

ld = importlib.machinery.SourceFileLoader("lmap", str(Path(__file__).resolve().parent.parent / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


d = Path(tempfile.mkdtemp()); log = d / "x.log"
log.write_text("=== 2026-10-16 15:00:00 start jobs=3 ===\nstep 2/2: COG (full detail + mask overviews) 15:30\n"
               "=== 2026-10-16 17:12:47 start jobs=3 ===\n[1/3] ok s19e147  (0 tiles)\n"
               "+ gdalbuildvrt -q x\nbuilding /m/qld/sat-2m4.cog.tif from 9 tiles\n"
               "building sat-2m4.cog.tif (webp) in local scratch, 8 threads\n"
               "step 1/2: overviews (9 levels, WEBP 95) 17:19\n")
st = L.job_stage(log, None)
check(st == {"stage": "step 1/2: overviews (9 levels, WEBP 95)", "stage_since": "17:19"}, f"latest stage + time ({st})")
with open(log, "a") as f:
    f.write("step 2/2: COG (full detail + mask overviews) 17:41\n+ gdal_translate -q ...\n")
st = L.job_stage(log, None)
check(st == {"stage": "step 2/2: COG (full detail + mask overviews)", "stage_since": "17:41"}, "moves to the next step")
log.write_text("=== 2026-10-16 15:00:00 start ===\nstep 2/2: COG (full) 15:30\n=== 2026-10-16 17:12:47 start ===\n[1/3] ok\n")
check(L.job_stage(log, None) is None, "a previous run's stage is not reported")
log.write_text("=== 2026-10-16 17:00:00 start ===\nbuilding /m/a.cog.tif from 9 tiles\n")
check(L.job_stage(log, None) == {"stage": "building /m/a.cog.tif from 9 tiles", "stage_since": None},
      "a stage with no time stamp")
check(L.job_stage(d / "missing.log", None) is None, "no log -> None")
code = lmap_source()
check('if pid and st.get("left") == 0:' in code and 'r["eta_s"] = r["eta_at"] = None' in code,
      "units all done + running: stage shown, stale per-unit ETA dropped")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
