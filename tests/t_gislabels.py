#!/usr/bin/env python3
"""Map-reading safety labels: the magnetic model refuses dates it doesn't cover (a stale declination is silently
wrong), LOS says the terrain may include trees, the sheet footer's approx-contour line names the terrain resolution
used, footer data dates come from the library files, and a restricted source prints its licence on fetch.
Usage: t_gislabels.py <scratch>"""
import argparse, importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


# 1. magnetic model span
name, epoch, until = L.wmm_model()
check(until == epoch + 5, f"model {name} valid {epoch:.0f}-{until:.0f}")
d = L.wmm_declination(-19.26, 146.82, epoch + 1.8, L.WMM_COF)
check(6.5 < d < 8.0, f"test-box declination {d:.2f} deg east (expected ~7)")
for y in (until, until + 3, epoch - 0.1):
    try:
        L.wmm_declination(-19.26, 146.82, y, L.WMM_COF); check(False, f"year {y} refused")
    except L.ModelExpired as e:
        check(True, f"year {y} refused ({e})")

# 2. LOS / viewshed note no longer claims bare earth for a surface model
check("bare-earth DEM" not in L.TERRAIN_NOTE and "trees" in L.TERRAIN_NOTE, "terrain note: may include trees, not 'bare-earth DEM'")

# 3. data_dates from library file times
root = tmp / "root"; (root / "data" / "osm").mkdir(parents=True)
f = root / "data" / "osm" / "x.gpkg"; f.write_bytes(b"x"); os.utime(f, (1767225600, 1767225600))   # 2026-01-01 UTC
ctx = L.Ctx(argparse.Namespace(root=str(root), catalog=None, json=True, dry_run=True, yes=False, verbose=False, quiet=True))
ctx.sources = {"osm-x": {"dest": "data/osm"}}
want = time.strftime("%Y-%m-%d", time.localtime(1767225600))
got = L.data_dates(ctx, {"items": [{"key": "osm", "from": "osm-x"}, {"key": "dem", "from": "dem-au-30m"}]})
check(got == f".  OSM {want}", f"footer data date from the file ({got!r})")
check(L.data_dates(ctx, {"items": [{"key": "osm", "from": "missing"}]}) == "", "unknown source: no date, no crash")

# 4. restricted source: licence printed on fetch (dry-run), open source: nothing
cat = tmp / "s.toml"
cat.write_text('[source.r1]\ntitle="r"\nkind="manual"\ndest="."\nlicence="CC BY-NC 4.0 test"\nrestricted=true\n'
               '[source.o1]\ntitle="o"\nkind="manual"\ndest="."\nlicence="CC BY 4.0"\n')
run = lambda i: subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "--root", str(root), "--catalog", str(cat),
                                "data", "fetch", i, "--dry-run"], capture_output=True, text=True, timeout=60)
r1, o1 = run("r1"), run("o1")
check("CC BY-NC 4.0 test" in r1.stderr, "restricted: licence shown on fetch")
check("licence limits sharing" not in o1.stderr, "open: no licence note")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
