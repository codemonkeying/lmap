#!/usr/bin/env python3
"""100a: `refresh --only ''` (a blank script variable) is a usage error, never 'refresh everything'. Dry run, offline."""
import subprocess, sys
from pathlib import Path
L = str(Path(__file__).resolve().parent.parent / "bin" / "lmap")
fails = []
for v in ("", " , "):
    r = subprocess.run([sys.executable, L, "--json", "refresh", "--only", v, "--dry-run"], capture_output=True, text=True, timeout=120)
    ok = r.returncode != 0 and "--only is empty" in r.stdout + r.stderr
    print(("PASS " if ok else "FAIL ") + f"--only {v!r} refused (rc {r.returncode})"); ok or fails.append(v)
print("ALL OK" if not fails else "SOME FAILED"); raise SystemExit(1 if fails else 0)
