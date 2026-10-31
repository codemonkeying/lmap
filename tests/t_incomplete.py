#!/usr/bin/env python3
"""A sheet missing a layer its style needs (photo: sat; both: dem, osm) fails (exit 6) but the PDF is still printed,
stamped INCOMPLETE on the sheet. Offline (source checks; the real print is exercised by t_map)."""
import importlib.machinery, importlib.util, inspect
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

check(L.STYLE_NEEDS["photo"] >= {"sat", "dem", "osm"} and "sat" not in L.STYLE_NEEDS["topo"], "photo needs sat; topo doesn't")
src = inspect.getsource(L.cmd_map)
check('res["ok"] = False' in src and "E_UPSTREAM" in src and '"incomplete"' in src, "missing needed layer: ok false, exit 6")
check("pdfs" in src and src.index("print_sheet(") < src.index('res["incomplete"]'), "PDFs are printed before the verdict (kept)")
check("a.get('incomplete')" in L.PRINT_PY and "INCOMPLETE: " in L.PRINT_PY, "print script stamps INCOMPLETE on the sheet")
check("needs internet)" not in src, "no fixed 'satellite needs internet' note")
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
