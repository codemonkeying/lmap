#!/usr/bin/env python3
"""Photo builds are WEBP by default, JPEG only with webp=no.
1. source scan: no hard-coded COMPRESS=JPEG outside photo_co/photo_ovr_env/cog_co (final encodes).
2. real encodes: photo_co + photo_ovr_env + cog_co -> gdalinfo shows WEBP (default) / JPEG (webp=no)."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import importlib.machinery, importlib.util, re, subprocess, sys, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

# 1. no stray JPEG encodes
src = lmap_source().splitlines()
allowed = ("def photo_co", "def photo_ovr_env", "def cog_co", "def to_mbtiles", "def mbtiles_to_jpeg")
fn = ""
for i, line in enumerate(src, 1):
    m = re.match(r"\s*def (\w+)", line)
    if m and not line.startswith(" " * 8): fn = "def " + m.group(1)
    if re.search(r'COMPRESS=JPEG|"COMPRESS_OVERVIEW": "JPEG"', line) and fn not in allowed:
        check(False, f"hard-coded JPEG at line {i} in {fn}")
check(True, "source scan done")
# catalog photo entries: fileclip must let webp= decide over the catalog's compress
fc = "\n".join(src)
check('comp = "WEBP" if ctx.webp else "JPEG"' in fc, "fileclip: webp= overrides catalog compress for photos")
cat = (HERE / "sources.toml").read_text()
check(not re.search(r'^compress\s*=\s*"JPEG"', cat, re.M), "sources.toml: no source pinned to JPEG")

# 2. real encodes
d = Path(tempfile.mkdtemp())
srcimg = d / "in.tif"
subprocess.run(["gdal_create", "-of", "GTiff", "-outsize", "600", "600", "-bands", "3", "-ot", "Byte", "-burn", "90",
                "-a_srs", "EPSG:3857", "-a_ullr", "0", "6000", "6000", "0", str(srcimg)], check=True)
comp = lambda p: (re.search(r"COMPRESSION=(.+)", subprocess.run(["gdalinfo", str(p)], capture_output=True,
                                                                  text=True).stdout) or [None, "?"])[1]
for webp, want in ((True, "WEBP"), (False, "JPEG")):
    ctx = types.SimpleNamespace(webp=webp)
    o = d / f"co-{want}.tif"
    subprocess.run(["gdal_translate", "-q", "-of", "COG", *L.photo_co(ctx), str(srcimg), str(o)], check=True)
    check(want in comp(o).upper(), f"photo_co webp={webp} -> {comp(o)}")
    args, label = L.cog_co(ctx, srcimg)
    o = d / f"cog-{want}.tif"
    subprocess.run(["gdal_translate", "-q", "-of", "COG", *args, str(srcimg), str(o)], check=True)
    check(want in comp(o).upper(), f"cog_co webp={webp} -> {comp(o)} ({label})")
    t = d / f"ovr-{want}.tif"; subprocess.run(["cp", str(srcimg), str(t)], check=True)
    env = [f"--config {k} {v}" for k, v in L.photo_ovr_env(ctx).items()]
    subprocess.run(["gdaladdo", "-q", "-ro", *" ".join(env).split(), str(t), "2", "4"], check=True)
    oc = comp(str(t) + ".ovr")
    check(want in oc.upper(), f"photo_ovr_env webp={webp} -> .ovr {oc}")


# 3. grey photo -> WEBP via grey3 (3 equal bands); GDAL write errors (exit 0) must fail the run
grey = d / "grey.tif"
subprocess.run(["gdal_create", "-q", "-of", "GTiff", "-outsize", "600", "600", "-bands", "1", "-ot", "Byte", "-burn", "120",
                "-a_srs", "EPSG:3857", "-a_ullr", "0", "6000", "6000", "0", str(grey)], check=True)
ctx = types.SimpleNamespace(webp=True, scratch=lambda: d, vlog=lambda *a: None, log=lambda *a: None)
o = d / "grey-webp.tif"
L.run(["gdal_translate", "-q", "-of", "COG", *L.photo_co(ctx), L.photo_src(ctx, grey, o), o], ctx)
check(L.photo_src(ctx, srcimg, o) == str(srcimg), "photo_src: 3-band source used as-is")
check(L.photo_src(types.SimpleNamespace(webp=False), grey, o) == str(grey), "photo_src: webp=no leaves grey as-is (JPEG ok)")
v = list(d.glob("grey-webp.tif.grey3-*")); check(len(v) == 1, f"grey VRT named after the part file: {v}")
a1, a2 = L.grey3(ctx, grey, o, 0), L.grey3(ctx, grey, o, 1)
check(a1 != a2, "two sources -> two VRTs (index, not file name)")
st = subprocess.run(["gdalinfo", "-stats", str(o)], capture_output=True, text=True).stdout
check("WEBP" in comp(o).upper() and "STATISTICS_MEAN=120" in st and "VALID_PERCENT=100" in st,
      f"grey3 -> WEBP keeps grey values ({comp(o)})")
for cmd in (["gdal_translate", "-q", "-of", "COG"], ["gdalwarp", "-q", "-overwrite", "-of", "COG"]):
    try:
        L.run(cmd + L.photo_co(ctx) + [grey, d / "bad.tif"], ctx)
        check(False, f"{cmd[0]}: 1-band WEBP write errors raise")
    except L.CliError as e:
        check("fail" in str(e), f"{cmd[0]}: 1-band WEBP write errors raise: {str(e)[:70]}")
try:                                    # the moon case: GDAL printed tile errors but exited 0
    L.run(["sh", "-c", 'echo "ERROR 1: Error when compressing strip/tile 0" >&2; exit 0'], ctx)
    check(False, "exit 0 + write errors raise")
except L.CliError as e:
    check("failed to write" in str(e), f"exit 0 + write errors raise: {str(e)[:60]}")
check("grey3(ctx, f, part, i)" in fc, "fileclip: grey photo goes through grey3")
check(fc.count("photo_src(ctx,") >= 2, "tcc shrink + area build read photos through photo_src")
for msg in ("ERROR 1: TIFFWriteEncodedTile:Write error", "ERROR 3: Disk full", "ERROR 1: i/o error writing block",
            "ERROR 1: WebPEncode() failed"):
    check(bool(L.WRITE_ERR.search(msg)), f"WRITE_ERR catches {msg!r}")
for msg in ("Warning 1: TIFFReadDirectory: Sum of Photometric", "ERROR 6: no such option FOO"):
    check(not L.WRITE_ERR.search(msg), f"WRITE_ERR ignores {msg!r}")

# 0.75 quality = "original": every photo encode lossless (pixel-identical), never mixed with a lossy tile cache
import json as _j, numpy as _np
from osgeo import gdal as _g
_g.UseExceptions()
T = Path(tempfile.mkdtemp())
std, org = types.SimpleNamespace(webp=True, quality="standard"), types.SimpleNamespace(webp=True, quality="original")
check(L.photo_q(std, 85) == 85 and L.photo_q(org, 85) == 100 and "QUALITY=100" in L.photo_co(org), "original -> QUALITY=100")
check(L.photo_ovr_env(org) == {"COMPRESS_OVERVIEW": "WEBP", "WEBP_LOSSLESS_OVERVIEW": "YES"}, "original overviews lossless")
a = _np.random.default_rng(7).integers(0, 255, (3, 600, 600)).astype("uint8")
d = _g.GetDriverByName("GTiff").Create(str(T / "src.tif"), 600, 600, 3, _g.GDT_Byte)
for i in range(3):
    d.GetRasterBand(i + 1).WriteArray(a[i])
d.SetGeoTransform((0, 1, 0, 0, 0, -1)); d = None
def same(f):
    return bool((_g.Open(str(f)).ReadAsArray() == a).all())
subprocess.run(["gdal_translate", "-q"] + L.tile_co("webp-ll") + [str(T / "src.tif"), str(T / "t.tif")], check=True)
check(same(T / "t.tif"), "lossless tile cache format is pixel-identical")
subprocess.run(["gdal_translate", "-q", "-of", "COG"] + L.photo_co(org) + [str(T / "src.tif"), str(T / "c.tif")], check=True)
check(same(T / "c.tif"), "original COG is pixel-identical")
subprocess.run(["gdal_translate", "-q", "-of", "COG"] + L.photo_co(std) + [str(T / "src.tif"), str(T / "c2.tif")], check=True)
check(not same(T / "c2.tif"), "standard COG is lossy (sanity: the test can tell)")
s_ = {"id": "x", "res_m": 2.4, "xml": "x.xml", "step": 1}
cd = T / "cache"; check(L.check_manifest(s_, cd, True) == "webp-ll", "new cache under original: lossless format")
cd2 = T / "cache2"; L.check_manifest(s_, cd2, False)
try:
    L.check_manifest(s_, cd2, True); check(False, "lossy cache refused under original")
except L.CliError as e:
    check("lossy" in e.msg, "a lossy tile cache is refused under quality = original")
check(L.check_manifest(s_, cd, False) == "webp-ll", "a lossless cache is fine under standard")
cd3 = T / "cache3"; cd3.mkdir(); (cd3 / "old.tif").write_bytes(b"x")
try:
    L.check_manifest(s_, cd3, False); check(False, "tiles with no manifest refused")
except L.CliError as e:
    check("no manifest" in e.msg and not (cd3 / "_manifest.json").exists(), "tiles with no manifest are refused")
cd4 = T / "cache4"; cd4.mkdir(); (cd4 / "_manifest.json").write_text(_j.dumps(L.wmst_manifest(s_)))
try:
    L.check_manifest(s_, cd4, False); check(False, "manifest without a format refused")
except L.CliError as e:
    check("format" in e.msg and "catalog" not in e.msg, f"manifest without a format: says so ({e.msg})")

print("ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
