#!/usr/bin/env python3
"""ro_sqlite: read-only SQLite opens are immutable (no lock probes on a CIFS share) unless a write is in
progress, and odd folder names ('?', '#', '%', space) don't cut the path. Source scan: no other mode=ro opens."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import importlib.machinery, importlib.util, re, sqlite3, subprocess, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

src = lmap_source()
import re as _re
lines = src.splitlines()
inside = False; bad = []
for l in lines:
    if _re.match(r"def \w", l): inside = l.startswith("def ro_sqlite(")
    if "sqlite3.connect(" in l and not inside and "part" not in l:   # writers only ever open a .part file
        bad.append(l.strip())
check(not bad, f"every non-.part sqlite open goes through ro_sqlite {bad[:2]}")

T = Path(tempfile.mkdtemp(prefix="lmap-trq-"))
try:
    d = T / "a?b #c%20d"; d.mkdir()
    f = d / "t.mbtiles"
    w = sqlite3.connect(f); w.execute("CREATE TABLE metadata (name, value)")
    w.execute("INSERT INTO metadata VALUES ('lmap_key', 'k1')"); w.commit(); w.close()
    c = L.ro_sqlite(f)
    check(c.execute("SELECT value FROM metadata").fetchone()[0] == "k1", "odd folder name opens the right file")
    c.close()
    u = T / "ünï cødé"; u.mkdir(); import shutil; shutil.copy(f, u / "t.mbtiles")
    cwd = _o.getcwd(); _o.chdir(u)
    try:
        c = L.ro_sqlite(Path("t.mbtiles")); check(c.execute("SELECT value FROM metadata").fetchone()[0] == "k1",
                                                  "relative path in a unicode folder"); c.close()
    finally:
        _o.chdir(cwd)
    check(L.mbtiles_current(f, "k1") and not L.mbtiles_current(f, "k2"), "mbtiles_current via ro_sqlite")
    # another process holds an EXCLUSIVE lock: an immutable read doesn't probe locks -> still reads
    w = sqlite3.connect(f, isolation_level=None); w.execute("BEGIN EXCLUSIVE")
    try:
        c = L.ro_sqlite(f); r = c.execute("SELECT count(*) FROM metadata").fetchone()[0]; c.close()
        check(r == 1, "no lock probe while immutable (CIFS-safe)")
    except sqlite3.OperationalError as e:
        check(False, f"immutable read blocked: {e}")
    w.execute("ROLLBACK"); w.close()
    jp = Path(str(f) + "-journal")      # a -journal beside it = write in progress: normal locking must apply
    jp.write_bytes(b"")
    w = sqlite3.connect(f, isolation_level=None); w.execute("BEGIN EXCLUSIVE")
    try:
        c = L.ro_sqlite(f, timeout=0.2); c.execute("SELECT count(*) FROM metadata").fetchone(); c.close(); locked = False
    except sqlite3.OperationalError:
        locked = True
    w.execute("ROLLBACK"); w.close()
    check(locked, "with a -journal present: normal locking (sees the writer)")
    # a stale side file with no writer: still opens (no rollback needed / immutable fallback)
    c = L.ro_sqlite(f, timeout=0.2); check(c.execute("SELECT value FROM metadata").fetchone()[0] == "k1",
                                          "stale -journal, no writer: opens"); c.close()
    w = sqlite3.connect(f, isolation_level=None); w.execute("BEGIN EXCLUSIVE")
    try:
        ok = L.file_check("t.mbtiles")(f); w.execute("ROLLBACK"); w.close()
        check(ok, "verifier never lock-probes (immutable even with a -journal)")
    except Exception as e:
        w.close(); check(False, f"verifier lock-probed: {e}")
    jp.unlink(missing_ok=True)
finally:
    subprocess.run(["rm", "-rf", str(T)])
print("FAILED: " + "; ".join(fails) if fails else "OK")
raise SystemExit(1 if fails else 0)
