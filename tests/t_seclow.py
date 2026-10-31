#!/usr/bin/env python3
"""Security hardening: https -> http redirects refused, proxy credentials masked, catalog/server names that go into
SQL text must be plain identifiers, a scratch dir that is a link (or not ours) is refused, the qld fetch shell line
quotes source ids, and an oversized "VRT" is not parsed.
Usage: t_seclow.py <scratch>"""
import argparse, importlib.machinery, importlib.util, os, shlex, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


# 1. redirects
h = L._NoDowngrade()
try:
    h.redirect_request(urllib.request.Request("https://a.example/x"), None, 302, "", {}, "http://b.example/y")
    check(False, "https -> http redirect refused")
except urllib.error.HTTPError:
    check(True, "https -> http redirect refused")
r = h.redirect_request(urllib.request.Request("https://a.example/x"), None, 302, "", {}, "https://b.example/y")
check(r.full_url == "https://b.example/y", "https -> https redirect followed")
r = h.redirect_request(urllib.request.Request("http://a.example/x"), None, 302, "", {}, "http://a.example/y")
check(r.full_url == "http://a.example/y", "http -> http redirect followed")
check(any(isinstance(x, L._NoDowngrade) for x in L.OPENER.handlers), "default opener refuses downgrades")

# 2. proxy credentials
check(L.mask_url("http://user:secret@proxy.example:3128") == "http://***@proxy.example:3128", "proxy password masked")
check(L.mask_url("http://proxy.example:3128") == "http://proxy.example:3128", "plain proxy URL unchanged")
check(L.mask_url("user:secret@proxy.example:3128") == "***@proxy.example:3128", "proxy without a scheme masked too")
c0 = L.Ctx(argparse.Namespace(root=str(tmp / "r0"), catalog=None, json=True, dry_run=True, yes=False, verbose=False,
                              quiet=True))
t0 = tmp / "t0"; t0.mkdir(); (tmp / "else0").mkdir(); os.symlink(tmp / "else0", t0 / f"lmap-{os.getuid()}")
c0.tmp = str(t0); c0.tiles_dir = None
try:
    c0.path(str(t0 / f"lmap-{os.getuid()}" / "tiles" / "x")); check(False, "tile cache under a linked scratch refused")
except L.CliError:
    check(True, "tile cache under a linked scratch refused")

# 3. SQL identifiers
for good in ("roads", "OBJECTID", "qld_roads_2"):
    check(L.sql_ident(good, "t") == good, f"plain name {good} accepted")
for bad in ("x; DROP TABLE y", "1abc", "a b", "", None, "a\"b"):
    try:
        L.sql_ident(bad, "t"); check(False, f"name {bad!r} refused")
    except L.CliError:
        check(True, f"name {bad!r} refused")

# 4. scratch dir
ns = argparse.Namespace(root=str(tmp / "root"), catalog=None, json=True, dry_run=True, yes=False, verbose=False, quiet=True)
c = L.Ctx(ns)
t1 = tmp / "t1"; t1.mkdir(); (tmp / "elsewhere").mkdir()
os.symlink(tmp / "elsewhere", t1 / f"lmap-{os.getuid()}")
c.tmp = str(t1)
try:
    c.scratch(); check(False, "scratch that is a symlink refused")
except L.CliError:
    check(True, "scratch that is a symlink refused")
t2 = tmp / "t2"; t2.mkdir(); c.tmp = str(t2); d = c.scratch()
check(d.is_dir() and (d.stat().st_mode & 0o777) == 0o700, "fresh scratch made private (0700)")

# 5. qld fetch shell line: ids quoted (the line is built with shlex_q; a hostile id stays one word)
sid = "x; touch pwned"
check(shlex.split(f"lmap data fetch {L.shlex_q(sid)} --yes")[3] == sid, "source id stays one shell word")
src = (HERE / "lmaplib" / "05_area.py").read_text()
check("data fetch {shlex_q(sid)} --yes" in src, "qld fetch quotes the source id")

# 6. oversized VRT not parsed
big = tmp / "big.vrt"
with open(big, "wb") as f:
    f.truncate((64 << 20) + 1)
check(L._vrt_members(big) is None, "oversized VRT ignored")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
# read-only gpkg opens skip SQLite locking, except while a write is (or was) in progress
import tempfile as _tf
_d = Path(_tf.mkdtemp()); _g = _d / "a.gpkg"; _g.write_bytes(b"")
check(L.ro_gpkg_opts(_g) == ["IMMUTABLE=YES"] and L.ro_gpkg_opts(_d / "a.tif") == [], "IMMUTABLE for a plain gpkg only")
for _x in ("-journal", "-wal", "-shm"):
    _j = Path(f"{_g}{_x}"); _j.write_bytes(b""); check(L.ro_gpkg_opts(_g) == [], f"{_x} present: normal locking"); _j.unlink()
check(L.ro_gpkg_opts("/vsizip/x.zip/a.gpkg") == [], "/vsi paths: no option")

from osgeo import gdal as _g, ogr as _o
_bad = _d / "bad.gpx"; _bad.write_text("not xml")
_o.UseExceptions()                     # GDAL >= 3.? shares one switch for gdal + ogr; raise either way
try:
    L.ro_ogr(_bad); check(False, "ro_ogr raises with ogr exceptions on")
except RuntimeError:
    check(True, "ro_ogr raises with ogr exceptions on (as ogr.Open)")
_o.DontUseExceptions()
check(L.ro_ogr(_bad) is None, "ro_ogr returns None with exceptions off")

sys.exit(1 if fails else 0)
