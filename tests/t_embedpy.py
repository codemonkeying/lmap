#!/usr/bin/env python3
"""Every embedded QGIS/Python script (module-level *_PY strings) compiles: a typo there only shows at print time."""
import importlib.machinery, importlib.util
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
bad, n = [], 0
for k in dir(L):
    v = getattr(L, k)
    if k.endswith("_PY") and isinstance(v, str):
        n += 1
        try:
            compile(v, k, "exec")
        except SyntaxError as e:
            bad.append(f"{k}: {e}")
print(("PASS " if not bad and n else "FAIL ") + f"{n} embedded scripts compile {bad}")
print("ALL OK" if not bad and n else "SOME FAILED"); raise SystemExit(1 if bad or not n else 0)
