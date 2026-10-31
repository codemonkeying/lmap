#!/usr/bin/env python3
"""A tile-server .xml input is signed by its CONTENT: the same .xml in a 2nd checkout / reinstall (other mtime)
gives the same build signature, so an area's basemap is not re-downloaded; a changed .xml does rebuild."""
import importlib.machinery, importlib.util, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lm", str(HERE / "bin" / "lmap"))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("lm", ld)); ld.exec_module(m)
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


with tempfile.TemporaryDirectory() as t:
    a, b = Path(t, "a"), Path(t, "b"); a.mkdir(); b.mkdir()
    xa, xb = a / "qld-topo.xml", b / "qld-topo.xml"
    xa.write_text("<GDAL_WMS>x</GDAL_WMS>"); xb.write_text("<GDAL_WMS>x</GDAL_WMS>")
    os.utime(xa, (1_000_000_000, 1_000_000_000)); os.utime(xb, (1_700_000_000, 1_700_000_000))
    p = {"kind": "topo"}
    check(m.build_sig([xa], p) == m.build_sig([xb], p), "same .xml, other checkout/mtime -> same signature")
    xb.write_text("<GDAL_WMS>y</GDAL_WMS>")
    check(m.build_sig([xa], p) != m.build_sig([xb], p), "changed .xml -> new signature (rebuilds)")
    tif = a / "x.tif"; tif.write_bytes(b"1")
    check(":" in m._sig_input(tif) and "sha1" not in m._sig_input(tif), "data files still size + mtime")
# a live-server item (tile .xml) keeps its signature across months: no automatic re-download
with tempfile.TemporaryDirectory() as t:
    d = Path(t); x = d / "qld-topo.xml"; x.write_text("<GDAL_WMS>x</GDAL_WMS>")
    s1 = m.item_sig(d, "topo", "tiles", str(x), {}, [146, -19.5, 147, -19], "EPSG:3857")[2]
    _t = m.time.time
    m.time.time = lambda: _t() + 400 * 86400              # 400 days later
    try:
        s2 = m.item_sig(d, "topo", "tiles", str(x), {}, [146, -19.5, 147, -19], "EPSG:3857")[2]
    finally:
        m.time.time = _t
    check(s1 == s2, "live item: same signature a year later (only --refresh-live re-downloads)")
    check(m._item_live("tiles", str(x)), "tile .xml source counts as live (--refresh-live picks it)")
# --refresh-live forces live items only; refresh --live passes it to area steps
import types
calls = []
_real = m._area_build_item
m._area_build_item = lambda c, d, key, *r: calls.append(key) or {"file": None, "skipped": "stub"}
try:
    with tempfile.TemporaryDirectory() as t:
        d = Path(t); x = d / "qld-topo.xml"; x.write_text("<GDAL_WMS>x</GDAL_WMS>"); dem = d / "src.tif"; dem.write_bytes(b"1")
        bb = [146, -19.5, 147, -19]
        for key, kind, src in (("topo", "tiles", str(x)), ("dem", "warp", str(dem))):
            out, ins, sig = m.item_sig(d, key, kind, src, {}, bb, "EPSG:3857")
            out.write_bytes(b"x"); m.sig_write(out, sig)
        m.readable = lambda p: Path(p).exists()
        ctx = types.SimpleNamespace(a=types.SimpleNamespace(rebuild=False, refresh_live=True))
        for key, kind, src in (("topo", "tiles", str(x)), ("dem", "warp", str(dem))):
            m.area_build_item(ctx, d, key, kind, src, {}, bb, "EPSG:3857")
    check(calls == ["topo"], f"--refresh-live rebuilds the live item only ({calls})")
finally:
    m._area_build_item = _real
sys.exit(1 if fails else 0)
