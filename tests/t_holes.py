#!/usr/bin/env python3
"""A tile the server has no imagery for (204/404) is a transparent hole, never opaque black that looks like
real dark imagery. Blank tiles carry alpha; a unit with a hole is read with one more band (alpha) so the hole
stays alpha 0 while JPEG tiles read alpha 255.
(The end-to-end check, 404 -> mask 0 in the finished COG on both fetch paths, is in t_tms.)"""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
import importlib.machinery, importlib.util, io, tempfile, shutil, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
from PIL import Image
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

for b, mode in ((1, "LA"), (3, "RGBA"), (4, "RGBA")):
    im = Image.open(io.BytesIO(L.zero_tile(b, 256)))
    check(im.mode == mode and im.size == (256, 256) and im.getextrema()[-1] == (0, 0),
          f"{b}-band blank is {mode}, fully transparent")

x = '<GDAL_WMS><Service name="TMS"><ServerUrl>https://h/${z}/${y}/${x}</ServerUrl></Service><DataWindow>' \
    '<TileLevel>9</TileLevel><YOrigin>bottom</YOrigin></DataWindow><BandsCount>3</BandsCount>' \
    '<ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes></GDAL_WMS>'
check("<BandsCount>4</BandsCount>" in L.tms_local_xml(x, Path("/t"), 5, True, bands=4), "local xml: bands override")
check("<BandsCount>3</BandsCount>" in L.tms_local_xml(x, Path("/t"), 5, True), "local xml: no override keeps 3")

# GDAL reads a mixed tree (JPEG + new blank) under the +1 band count: data opaque, hole transparent
T = Path(tempfile.mkdtemp(prefix="lmap-tho-"))
try:
    (T / "2" / "1").mkdir(parents=True)
    Image.new("RGB", (256, 256), (200, 100, 50)).save(T / "2" / "1" / "1", format="JPEG")
    (T / "2" / "1" / "2").write_bytes(L.zero_tile(3, 256))
    xml = f'''<GDAL_WMS><Service name="TMS"><ServerUrl>file://{T}/${{z}}/${{y}}/${{x}}</ServerUrl></Service>
<DataWindow><UpperLeftX>-20037508.34</UpperLeftX><UpperLeftY>20037508.34</UpperLeftY><LowerRightX>20037508.34</LowerRightX>
<LowerRightY>-20037508.34</LowerRightY><TileLevel>2</TileLevel><TileCountX>1</TileCountX><TileCountY>1</TileCountY>
<YOrigin>top</YOrigin></DataWindow><Projection>EPSG:3857</Projection><BlockSizeX>256</BlockSizeX>
<BlockSizeY>256</BlockSizeY><BandsCount>3</BandsCount></GDAL_WMS>'''
    (T / "x.xml").write_text(L.tms_local_xml(xml, T, 2, False, bands=4))
    v = {}
    for tx in (1, 2):
        r = subprocess.run(["gdallocationinfo", "-valonly", str(T / "x.xml"), str(tx * 256 + 100), "300"],
                           capture_output=True, text=True)
        v[tx] = [int(a) for a in r.stdout.split()]
    check(len(v[1]) == 4 and v[1][3] == 255 and abs(v[1][0] - 200) < 6, f"JPEG tile: colour, alpha 255 ({v[1]})")
    check(v[2] == [0, 0, 0, 0], f"blank tile: alpha 0 ({v[2]})")
    Image.new("L", (256, 256), 150).save(T / "2" / "1" / "1", format="JPEG")       # 1-band source: LA, 2 bands
    (T / "2" / "1" / "2").write_bytes(L.zero_tile(1, 256))
    (T / "x.xml").write_text(L.tms_local_xml(xml.replace(">3</BandsCount>", ">1</BandsCount>"), T, 2, False, bands=2))
    for tx in (1, 2):
        r = subprocess.run(["gdallocationinfo", "-valonly", str(T / "x.xml"), str(tx * 256 + 100), "300"],
                           capture_output=True, text=True)
        v[tx] = [int(a) for a in r.stdout.split()]
    check(len(v[1]) == 2 and v[1][1] == 255 and abs(v[1][0] - 150) < 6 and v[2] == [0, 0],
          f"1-band: gray tile opaque, blank transparent ({v[1]}, {v[2]})")
finally:
    shutil.rmtree(T, ignore_errors=True)

src = (HERE / "lmaplib" / "02_kinds.py").read_text()
check('["-srcnodata", "None"] + http' in src and "([13])" in src,
      "GDAL fetch path: alpha from the xml, GDAL zero blocks transparent")
print("ALL PASS" if not fails else "SOME FAILED")
_s.exit(1 if fails else 0)
