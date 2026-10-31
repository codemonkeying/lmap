#!/usr/bin/env python3
"""lmap map: a real A5 1:25k topo sheet rendered end to end into a throwaway root (the library's data/ linked in,
nothing written to the real maps/). Checks the PDF is a GeoPDF whose georeference sits on its own 1 km grid, the
sidecar JSON, a re-run that reuses the built area, and the CLI contract (no --yes = 5, bad args = 2).
Needs a library with dem-au-30m + osm-au (LMAP_ROOT); skipped otherwise. Usage: t_map.py <scratch>"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
lib = Path(os.environ.get("LMAP_ROOT") or "")
if not (lib / "data" / "australia" / "dem-30m.cog.tif").is_file() or not shutil.which("qgis_process") and \
        subprocess.run([sys.executable, "-c", "import qgis.core"], capture_output=True).returncode:
    print("SKIP needs LMAP_ROOT with dem-30m + python3-qgis"); sys.exit(0)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


root = tmp / "root"; root.mkdir()
for x in ("data", "accuracy.json"):                     # read-only use of the library; maps/ and areas/ are ours
    if (lib / x).exists():
        os.symlink(lib / x, root / x)
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_TMP": str(tmp / "scr"), "XDG_STATE_HOME": str(tmp / "state")}


def lmap(*a, timeout=280):
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "map", *a, "--json"], capture_output=True,
                       text=True, env=env, timeout=timeout)
    j = next((json.loads(l) for l in r.stdout.splitlines()[::-1] if l.startswith("{")), {})
    return r.returncode, j, r.stderr


C = "--center=-19.30,146.70"                              # inland of a coastal city: hills, roads, creeks
rc, j, err = lmap(C, "--scale", "25k", "--paper", "A5", "--dry-run")
check(rc == 0 and j.get("dry_run") and j.get("crs") == "EPSG:7855", f"dry run: plan in MGA 55 ({j.get('crs')})")
check(not (root / "maps").exists() or not any((root / "maps").rglob("*.pdf")), "dry run writes no PDF")
check(lmap(C, "--scale", "25k", "--paper", "A5")[0] == 5, "no --yes = 5")
check(lmap(C, "--paper", "A9", "--dry-run")[0] == 2, "bad paper = 2")
check(lmap("--center=1,2,3", "--dry-run")[0] == 2, "bad centre = 2")

t0 = time.time()
rc, j, err = lmap(C, "--scale", "25k", "--paper", "A5", "--yes", timeout=1100)
t1 = time.time() - t0
pdfs = [Path(p) for p in j.get("pdfs") or []]
check(rc == 0 and pdfs and pdfs[0].is_file(), f"sheet rendered in {t1:.0f} s (rc {rc}, missing {j.get('missing_layers')}, {[l for l in err.splitlines() if 'fail' in l.lower() and 'done,' not in l][:12] if rc else ''} {err.strip().splitlines()[-2:] if rc else ''})")
if pdfs and pdfs[0].is_file():
    pdf = pdfs[0]
    check(str(pdf).startswith(str(root / "maps")), "written under the throwaway root's maps/")
    check(pdf.stat().st_size > 50_000 and pdf.read_bytes()[:5] == b"%PDF-", f"a real PDF ({pdf.stat().st_size:,} B)")
    from osgeo import gdal, ogr
    gdal.UseExceptions(); ogr.UseExceptions()
    ds = gdal.Open(str(pdf)); sr = ds.GetSpatialRef()
    check(sr is not None and sr.GetAuthorityCode(None) == "7855", f"GeoPDF in MGA zone 55 ({sr and sr.GetAuthorityCode(None)})")
    ds = None
    v = ogr.Open(str(pdf)); names = [v.GetLayer(i).GetName() for i in range(v.GetLayerCount())]
    check("Grid lines" in names, f"vector grid layer present ({len(names)} layers)")
    g = v.GetLayerByName("Grid lines"); E = []
    for f in g:
        geo = f.GetGeometryRef()                       # labels etc. can carry no geometry
        pts = (geo.GetPoints() if geo is not None else None) or []
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        if xs and max(xs) - min(xs) < 1 and max(ys) - min(ys) > 500:
            E.append(sum(xs) / len(xs))
    v = None
    off = max((abs(round(e, -3) - e) for e in E), default=99)
    check(len(E) >= 2 and off < 0.5, f"georeference on its own 1 km grid ({len(E)} eastings, max {off:.2f} m off)")
    side = pdf.with_suffix(".json")
    sj = json.loads(side.read_text()) if side.is_file() else {}
    check(sj.get("lmap") and sj.get("scale") in (25000, "25k", "25000"), f"sidecar JSON ({sj})")
    check("georeference not checked" not in err, "georeference checked + corrected (2+ grid lines each way on A5)")
    import re, zipfile
    qz = next(pdf.parent.glob("*.qgz"), None)
    qs = zipfile.ZipFile(qz).read(next(n for n in zipfile.ZipFile(qz).namelist() if n.endswith(".qgs"))).decode() if qz else ""
    tree = re.findall(r'<layer-tree-(?:group|layer)[^>]*name="([^"]*)"', qs)
    ci = [i for i, n in enumerate(tree) if n.startswith("Contours")]; ri = [i for i, n in enumerate(tree) if n.startswith("Roads")]
    check(ci and ri and min(ci) > max(ri), f"project: contours listed under roads (roads {ri}, contours {ci})")
    rdx = re.search(r'<maplayer[^>]*>(?:(?!</maplayer>).)*?<layername>Roads[^<]*</layername>(?:(?!</maplayer>).)*?<renderer-v2[^>]*symbollevels="(\d)"', qs, re.S)
    check(rdx and rdx.group(1) == "1", "roads use symbol levels (outlines under all fills: no blocks at joins)")

    t0 = time.time(); m0 = pdf.stat().st_mtime_ns
    rc, j, err = lmap(C, "--scale", "25k", "--paper", "A5", "--yes", timeout=600)
    t2 = time.time() - t0
    check(rc == 0 and pdf.is_file(), f"re-run ok in {t2:.0f} s (first {t1:.0f} s)")
    check(t2 < t1 * 0.8 or t2 < 60, "re-run reuses the built area (faster)")
check(not list((tmp / "scr").rglob("*.part*")), "no .part files left in scratch")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
