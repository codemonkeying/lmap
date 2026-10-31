"""run.sh gives every test its own folder (argv[1]) and points TMPDIR, XDG_STATE_HOME and XDG_CACHE_HOME into it,
so temp files, run records and caches are removed with that folder and never touch the user's real lmap state.
Run alone (no folder argument): skipped."""
import os, sys, tempfile
from pathlib import Path

if len(sys.argv) < 2:
    print("SKIP: only meaningful under tests/run.sh"); sys.exit(0)
D = Path(sys.argv[1]).resolve()
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

for v in ("TMPDIR", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    p = os.environ.get(v, "")
    r = Path(p).resolve() if p else None
    check(r is not None and (r == D or D in r.parents), f"{v} is inside the test's folder ({p})")
check(D in Path(tempfile.mkdtemp()).resolve().parents, "tempfile.mkdtemp() lands in the test's folder")
print("FAILED: " + "; ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
