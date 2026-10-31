#!/usr/bin/env python3
"""lmap au contours: contract (usage / dry-run / --yes), a 2-cell build into a test root, resume, key stamp,
20 m values only, no sea, cells joined at the seam. Usage: t_aucont.py SCRATCHDIR"""
import json, os, subprocess, sys
from pathlib import Path
from osgeo import ogr
ogr.UseExceptions()
L = Path(__file__).resolve().parent.parent / "bin" / "lmap"
S = Path(sys.argv[1]); R = S / "root"; (R / "data" / "australia").mkdir(parents=True, exist_ok=True)
if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(0)
DEM = Path(os.environ["LMAP_ROOT"]) / Path("data/australia/dem-30m.cog.tif")
if not DEM.is_file():
    print("SKIP: no national 30 m DEM at", DEM); sys.exit(0)
(R / "data" / "australia" / DEM.name).unlink(missing_ok=True); (R / "data" / "australia" / DEM.name).symlink_to(DEM)
bad = 0
def ok(c, m):
    global bad
    print(("PASS " if c else "FAIL ") + m); bad += not c
def lm(*a, env=None):
    e = dict(os.environ, LMAP_AUCONT_CELLS="146,-20 147,-20", LMAP_TMP=str(S / "tmp"), **(env or {}))
    r = subprocess.run([str(L), "--json", "--root", str(R), *a], capture_output=True, text=True, env=e)
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        j = {}
    if r.returncode not in (0, 2, 5):                 # a failed build: show why (it only failed under load so far)
        print(f"  lmap {' '.join(a)} -> rc {r.returncode}\n  " + "\n  ".join(r.stderr.strip().splitlines()[-15:]))
    return r.returncode, j

rc, j = lm("au", "contours", "--bogus"); ok(rc == 2, f"bad flag -> exit 2 ({rc})")
rc, j = lm("au", "contours", "--dry-run"); ok(rc == 0 and j.get("cells") == 2 and j.get("build"), f"dry-run plans 2 cells ({j.get('cells')})")
rc, j = lm("au", "contours"); ok(rc == 5, f"no --yes -> exit 5 ({rc})")
out = R / "data" / "australia" / "contours-30m.gpkg"
ok(not out.exists(), "nothing written without --yes")
rc, j = lm("au", "contours", "--yes"); ok(rc == 0 and j.get("ok"), f"build ok ({rc}, {j.get('seconds')} s)")
ds = ogr.Open(str(out)); Lr = ds.GetLayer("contours")
n = Lr.GetFeatureCount(); ok(n > 10000, f"{n} lines")
import importlib.machinery, importlib.util
ld = importlib.machinery.SourceFileLoader("lm", str(L)); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("lm", ld)); ld.exec_module(m)
ok(ds.GetMetadataItem("lmap_key") == m.au_contours_key(DEM), "key stamped (version + DEM)")
sq = ds.ExecuteSQL("SELECT min(elevation_m) a, max(elevation_m) b, sum(elevation_m % 20 <> 0) c FROM contours")
f = sq.GetNextFeature(); ok(f["a"] >= 20 and f["c"] == 0, f"only 20 m values, none <= 0 ({f['a']}..{f['b']})"); f = None; ds.ReleaseResultSet(sq)
e = Lr.GetExtent(); ok(145.99 < e[0] and e[1] < 148.01 and -20.01 < e[2] and e[3] < -18.99, f"inside the 2 cells {[round(v, 3) for v in e]}")
# seam at 147E: line ends from the west cell meet line ends from the east cell (same points, both sides)
Lr.SetSpatialFilterRect(146.99999, -20, 147.00001, -19); W_, E_ = set(), set()
for ft in Lr:
    g = ft.GetGeometryRef()
    e = g.GetEnvelope(); side = W_ if e[0] < 147 - 1e-9 else E_     # which cell the line lies in
    for pt in (g.GetPoint_2D(0), g.GetPoint_2D(g.GetPointCount() - 1)):
        if abs(pt[0] - 147) < 1e-7:
            side.add((pt[1], ft["elevation_m"]))
Lr.SetSpatialFilter(None)
# each west end has an east end of the same height within 30 m (0.0003 deg; <0.5 mm at 1:25k)
both = sum(any(e_[1] == w[1] and abs(e_[0] - w[0]) < 0.0003 for e_ in E_) for w in W_)
ok(len(W_) > 50 and both >= 0.95 * len(W_), f"147E seam: {both} of {len(W_)} line ends meet a neighbour within 30 m")
Lr = None; ds = None
rc, j = lm("au", "contours", "--yes"); ok(rc == 0 and j.get("unchanged"), "second run: unchanged")
m0 = out.stat().st_mtime
rc, j = lm("au", "contours", "--yes", "--refresh"); ok(rc == 0 and j.get("ok") and not j.get("unchanged"), f"--refresh rebuilds ({rc}, {str(j.get('error') or j.get('hint') or '')[:300]})")
ds = ogr.Open(str(out)); ok(out.stat().st_mtime > m0 and ds.GetMetadataItem("lmap_key") == m.au_contours_key(DEM), "refreshed file, key kept"); ds = None
# resume: a finished cell file is counted and not rebuilt
import hashlib
cd = S / "tmp" / f"lmap-{os.getuid()}" / "au-contours" / hashlib.sha1(m.au_contours_key(DEM).encode()).hexdigest()[:10]
cd.mkdir(parents=True, exist_ok=True); (cd / "146_-20.gpkg").write_bytes(b"x")
rc, j = lm("au", "contours", "--dry-run", "--refresh"); ok(j.get("cells_done") == 1, f"dry-run counts a finished cell ({j.get('cells_done')})")
(cd / "146_-20.gpkg").unlink()
# not found: a root with no DEM
E = S / "empty"; (E / "data" / "australia").mkdir(parents=True, exist_ok=True)
r = subprocess.run([str(L), "--json", "--root", str(E), "au", "contours", "--dry-run"], capture_output=True, text=True,
                   env=dict(os.environ, LMAP_TMP=str(S / "tmp")))
ok(r.returncode == 4, f"no DEM -> exit 4 ({r.returncode})")
print("ALL PASS" if not bad else f"{bad} FAILED"); sys.exit(1 if bad else 0)
