#!/usr/bin/env python3
"""bin/lmap loads lmaplib/NN_*.py into ONE namespace, so a top-level name defined in two modules silently
replaces the earlier one (0.83 hit this: a new fs_type() shadowed 14_export's). Fails on any top-level
def/class defined twice, or a def/class that a module-level assignment elsewhere overwrites. Plain
re-assignment of a global (counters, caches set in two places) is allowed. Static (ast), no imports."""
import ast, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def scan(sources):
    """sources: [(name, text)] -> (defined twice, def overwritten by an assignment), name -> [where]."""
    defs, assigns = {}, {}
    for name, text in sources:
        for n in ast.parse(text, name).body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs.setdefault(n.name, []).append(f"{name}:{n.lineno}")
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            assigns.setdefault(x.id, []).append(f"{name}:{n.lineno}")
    return ({k: v for k, v in defs.items() if len(v) > 1},
            {k: defs[k] + assigns[k] for k in defs if k in assigns}, len(defs))


fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

files = [HERE / "bin" / "lmap"] + sorted((HERE / "lmaplib").glob("[0-9][0-9]_*.py"))
twice, clash, n = scan([(f.name, f.read_text()) for f in files])
check(len(files) > 10 and n > 300, f"scanned {len(files)} files, {n} top-level defs")
check(not twice, f"no def/class defined in two places ({twice})")
check(not clash, f"no def/class overwritten by a module-level assignment ({clash})")

t, c, _ = scan([("a.py", "def fs_type(p):\n    return 1\nX = 1\nX = 2\n"),
                ("b.py", "class K:\n    def get(self): pass\ndef fs_type(p):\n    return 2\nK = None\n")])
check(set(t) == {"fs_type"} and set(c) == {"K"}, f"catches a shadowed def and a def overwritten by assignment; "
      f"ignores methods and re-assigned globals ({sorted(t)}, {sorted(c)})")
print("ALL OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
