#!/usr/bin/env python3
"""The README promises Python 3.11+: every lmap file must parse with the 3.11 grammar (3.12 f-strings that reuse
the outer quote, e.g. f"{r["id"]}", are a SyntaxError on 3.11 and stop every command). Uses a real python3.11
if installed, else parso's 3.11 grammar (pip install parso / python3-parso; PYTHONPATH to a copy also works),
else SKIP."""
import glob, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
files = sorted(glob.glob(str(HERE / "lmaplib" / "*.py"))) + [str(HERE / "bin" / "lmap")]
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


py = shutil.which("python3.11")
if py:
    bad = [f for f in files if subprocess.run([py, "-c", "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')", f],
                                              capture_output=True).returncode]     # compile(): no __pycache__ left behind
    check(not bad, f"python3.11 compiles every file {bad}")
else:
    try:
        import parso
    except ImportError:
        print("SKIP no python3.11 and no parso"); sys.exit(0)
    g = parso.load_grammar(version="3.11")
    check(g.iter_errors(g.parse('x = f"{r["id"]}"\n')), "control: a 3.12-only f-string is flagged")
    errs = [f"{Path(f).name}:{e.start_pos[0]} {e.message}" for f in files for e in g.iter_errors(g.parse(open(f).read()))]
    check(not errs, f"every file parses as Python 3.11 ({len(files)} files) {errs[:5]}")
sys.exit(1 if fails else 0)
