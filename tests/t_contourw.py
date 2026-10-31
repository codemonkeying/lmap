#!/usr/bin/env python3
"""Contour widths: one global default (0.12 mm, index 2x), config [build] contour_mm / index_ratio, env, and a
per-map --contour-mm / --index-ratio override, in that order; bad values are a usage error. Usage: t_contourw.py <tmp>"""
import importlib.machinery, importlib.util, os, sys, tempfile, types
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
T = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
os.environ["XDG_CONFIG_HOME"] = str(T); os.environ.pop("LMAP_CONTOUR_MM", None); os.environ.pop("LMAP_INDEX_RATIO", None)
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)
A = lambda **k: types.SimpleNamespace(contour_mm=k.get("mm"), index_ratio=k.get("r"))
check(L.contour_widths(A()) == (0.12, 0.24), f"default 0.12 / 0.24 ({L.contour_widths(A())})")
(T / "lmap").mkdir(exist_ok=True); L.CONFIG.write_text("[build]\ncontour_mm = 0.1\nindex_ratio = 2.5\n")
check(L.contour_widths(A()) == (0.1, 0.25), "config [build] sets the global default")
os.environ["LMAP_INDEX_RATIO"] = "3"
check(L.contour_widths(A()) == (0.1, 0.3), "env beats config")
check(L.contour_widths(A(mm=0.15, r=1.5)) == (0.15, 0.225), "per-map flags beat both")
for bad in (A(r=9), A(mm=0), A(mm=-1)):
    try:
        L.contour_widths(bad); check(False, f"bad {bad} refused")
    except L.CliError as e:
        check(e.code == L.E_USAGE, f"bad {vars(bad)} is a usage error")
check("a['cw'][1] if ix else a['cw'][0]" in L.TOPO_PY and "_CW[1]" in L.AREA_QGIS, "print + area project read the setting")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
