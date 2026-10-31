#!/usr/bin/env python3
"""wms-tiles from a plain TMS server (lmap's own downloader + warp), against a local tile server: two 0.05-degree
units arrive as tiles, are warped and installed, the finished COG covers the bbox with the server's pixels (a tile
colour code survives), a 404 is a blank tile (sea) not a failure, every request uses the configured User-Agent
(the shipped xml no longer names the tool), and a re-run with everything present fetches nothing.
Usage: t_tms.py <scratch>   (T_TMS_GDAL=1: GDAL fetches the tiles itself, the fetch="gdal" path)"""
import http.server, io, os, shutil, subprocess, sys, tempfile, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


from PIL import Image
Z = 13
seen = {"n": 0, "ua": set(), "404": 0}


def tile(x, y):
    """A flat tile whose colour encodes its position: R = x % 256, G = y % 256, B = 77."""
    im = Image.new("RGB", (256, 256), (x % 256, y % 256, 77)); b = io.BytesIO(); im.save(b, "PNG")
    return b.getvalue()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        seen["n"] += 1; seen["ua"].add(self.headers.get("User-Agent", ""))
        try:
            y, x = int(parts[-2]), int(parts[-1].split(".")[0])
        except (ValueError, IndexError):
            self.send_error(400); return
        if (x + y) % 2 == 0 or seen.get("outage"):      # some "sea": no tile (outage: none at all)
            seen["404"] += 1; self.send_error(404); return
        b = tile(x, y)
        self.send_response(200); self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
xml = tmp / "t.xml"
xml.write_text(f'''<GDAL_WMS><Service name="TMS"><ServerUrl>http://127.0.0.1:{port}/t/${{z}}/${{y}}/${{x}}.png</ServerUrl></Service>
<DataWindow><UpperLeftX>-20037508.34</UpperLeftX><UpperLeftY>20037508.34</UpperLeftY><LowerRightX>20037508.34</LowerRightX><LowerRightY>-20037508.34</LowerRightY><TileLevel>{Z}</TileLevel><TileCountX>1</TileCountX><TileCountY>1</TileCountY><YOrigin>top</YOrigin></DataWindow>
<Projection>EPSG:3857</Projection><BlockSizeX>256</BlockSizeX><BlockSizeY>256</BlockSizeY><BandsCount>3</BandsCount><MaxConnections>4</MaxConnections><ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes><UserAgent>should-be-ignored</UserAgent></GDAL_WMS>''')
cat = tmp / "sources.toml"
cat.write_text(f'''
[source.ttms]
title = "test tms"
kind = "wms-tiles"
xml = "{xml}"
bbox = [146.70, -19.30, 146.80, -19.25]
step = 0.05
res_m = 19.109
dest = "ttms"
out = "data/t/ttms.cog.tif"
licence = "test"
{'fetch = "gdal"' if os.environ.get("T_TMS_GDAL") else ''}
''')
root = tmp / "root"; root.mkdir()
UA = "TestAgent/1.0"
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_CATALOG": str(cat), "LMAP_TMP": str(tmp / "scr"),
       "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "LMAP_BACKOFF": "0.02",
       "LMAP_SCOPE": "0", "LMAP_UA": UA, "LMAP_GPU": "0"}


def fetch():
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "ttms", "--yes", "--json"],
                       capture_output=True, text=True, env=env, timeout=280)
    if os.environ.get("T_TMS_GDAL") and not seen.get("gdal_checked"):
        seen["gdal_checked"] = 1; check("blocks x" in r.stderr, "fetch=\"gdal\": GDAL fetched the blocks")
    return r.returncode, r.stderr


rc, err = fetch()
out = root / "data/t/ttms.cog.tif"
check(rc == 0 and out.is_file(), f"fetch + build ({err.strip().splitlines()[-2:] if rc else ''})")
check(seen["n"] > 4 and seen["404"] > 0, f"tiles came from the server ({seen['n']} requests, {seen['404']} were 404 = sea)")
check(seen["ua"] == {UA}, f"every request used the configured User-Agent ({seen['ua']})")
if out.is_file():
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(str(out)); gt = ds.GetGeoTransform(); W, Hh = ds.RasterXSize, ds.RasterYSize
    check(ds.GetSpatialRef().GetAuthorityCode(None) == "3857" and abs(gt[1] - 19.109) < 0.01, "EPSG:3857 at the asked resolution")
    import math
    R = 6378137.0; mx = lambda lo: math.radians(lo) * R
    my = lambda la: math.log(math.tan(math.pi / 4 + math.radians(la) / 2)) * R
    cov = gt[0] <= mx(146.70) + 40 and gt[0] + W * gt[1] >= mx(146.80) - 40 and \
        gt[3] >= my(-19.25) - 40 and gt[3] + Hh * gt[5] <= my(-19.30) + 40
    check(cov, "covers the bbox")
    # a pixel's colour = the server tile it came from (B = 77 marks real tile data)
    tm = 2 * 20037508.342789244 / 2 ** Z
    lon, lat = 146.753, -19.271
    x, y = int((mx(lon) + 20037508.342789244) // tm), int((20037508.342789244 - my(lat)) // tm)
    px, py = int((mx(lon) - gt[0]) / gt[1]), int((my(lat) - gt[3]) / gt[5])
    v = [int(ds.GetRasterBand(b).ReadAsArray(px, py, 1, 1)[0, 0]) for b in (1, 2, 3)]
    want = (0, 0, 0) if (x + y) % 2 == 0 else (x % 256, y % 256, 77)
    check(all(abs(a - b) <= 12 for a, b in zip(v, want)), f"pixel at {lon},{lat} is tile {x},{y}'s colour ({v} vs {want})")
    # every pixel vs the tile it falls in (away from tile edges, where averaging blends two tiles)
    import numpy as np
    arr = np.stack([ds.GetRasterBand(b).ReadAsArray() for b in (1, 2, 3)]).astype(int)
    cx = gt[0] + (np.arange(W) + 0.5) * gt[1] + 20037508.342789244
    cy = 20037508.342789244 - (gt[3] + (np.arange(Hh) + 0.5) * gt[5])
    tx, ty = (cx // tm).astype(int), (cy // tm).astype(int)
    fx, fy = cx % tm, cy % tm
    e_ = 16 * gt[1]                                  # lossy WEBP blurs ~8 px across a black/colour edge
    okx = (fx > e_) & (fx < tm - e_); oky = (fy > e_) & (fy < tm - e_)
    TX, TY = np.meshgrid(tx, ty); M = np.outer(oky, okx)
    sea = ((TX + TY) % 2 == 0) & M; land = ((TX + TY) % 2 != 0) & M
    # checkerboard: a one-tile shift would swap sea and land everywhere, so these two checks pin placement
    check(sea.sum() > 100 and arr[:, sea].max() <= 12, f"404 (sea) tiles are blank, not invented ({sea.sum()} px, max {arr[:, sea].max()})")
    # a hole must not look like black imagery: 404 (sea) is transparent, real tiles are opaque
    check(ds.RasterCount == 3 and ds.GetRasterBand(1).GetMaskFlags() & gdal.GMF_PER_DATASET or
          ds.RasterCount == 4 and ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand,
          f"transparency is a dataset mask or an Alpha band ({ds.RasterCount} bands)")
    b1 = ds.GetRasterBand(1)                         # the build keeps transparency as the internal mask (or alpha)
    al = ds.GetRasterBand(4) if ds.RasterCount == 4 else b1.GetMaskBand()
    a = al.ReadAsArray().astype(int)
    check(a[sea].max() == 0 and a[land].min() == 255, f"404 tiles transparent, land opaque (sea mask max {a[sea].max()}, land min {a[land].min()})")
    # per tile: median colour (lossy WEBP smears chroma at the black/colour edges; the median is the tile's own)
    bad = []
    for kx, ky in {(int(a_), int(b_)) for a_, b_ in zip(TX[land], TY[land])}:
        sel = land & (TX == kx) & (TY == ky)
        if sel.sum() < 50:
            continue
        med = [int(np.median(arr[c][sel])) for c in (0, 1)]
        if abs(med[0] - kx % 256) > 4 or abs(med[1] - ky % 256) > 4:
            bad.append((kx, ky, med))
    check(land.sum() > 100 and not bad, f"each land tile sits where it belongs ({bad[:3]})")
    ds = None
tdir_ = tmp / "scr" / f"lmap-{os.getuid()}" / "tiles" / "ttms"
check(not tdir_.exists() or not any(f.is_file() for f in tdir_.rglob("*")), "downloaded tiles cleared after install")
n0 = seen["n"]
rc, err = fetch()
check(rc == 0 and seen["n"] == n0, f"re-run: unchanged, no requests ({seen['n'] - n0})")

# 0.81 raw cache (lmap's own downloader): the server's tiles are kept as they came and the finished file is built
# straight from them, on the server's own pixel grid. quality=original makes that build lossless, so every pixel
# must equal the tile it came from exactly (the oracle), and a 404 tile is transparent.
if not os.environ.get("T_TMS_GDAL"):
    import math, numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    WM = 20037508.342789244; px = 2 * WM / (2 ** Z * 256)
    t = cat.read_text()
    cat.write_text(t + t.split("[source.ttms]")[1].replace('dest = "ttms"', 'dest = "ttms2"')
                   .replace('out = "data/t/ttms.cog.tif"', 'out = "data/t/ttms2.cog.tif"').join(["\n[source.ttms2]", ""]))
    cat.write_text(cat.read_text() + t.split("[source.ttms]")[1].replace('dest = "ttms"', 'dest = "ttms30"')
                   .replace('out = "data/t/ttms.cog.tif"', 'out = "data/t/ttms30.cog.tif"')
                   .replace("res_m = 19.109", "res_m = 30").join(["\n[source.ttms30]", ""]))
    r_ = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "ttms2", "--yes", "--json"],
                        capture_output=True, text=True, env={**env, "LMAP_QUALITY": "original"}, timeout=280)
    o2 = root / "data/t/ttms2.cog.tif"
    check(r_.returncode == 0 and o2.is_file(), f"raw cache, quality=original: built ({r_.stderr.strip().splitlines()[-2:]})")
    if o2.is_file():
        ds = gdal.Open(str(o2)); gt = ds.GetGeoTransform()
        check(abs(gt[1] - px) < 1e-9 and abs((gt[0] + WM) / (256 * px) - round((gt[0] + WM) / (256 * px))) < 1e-9
              and abs((WM - gt[3]) / (256 * px) - round((WM - gt[3]) / (256 * px))) < 1e-9,
              f"on the server's own tile grid exactly ({gt[0]}, {gt[3]}, {gt[1]} vs {px})")
        a = ds.ReadAsArray().astype(int); m = ds.GetRasterBand(1).GetMaskBand().ReadAsArray()
        X0 = round((gt[0] + WM) / (256 * px)); Y0 = round((WM - gt[3]) / (256 * px))
        H_, W_ = a.shape[1:]; bad = 0; holes = 0
        for ty in range(H_ // 256):
            for tx in range(W_ // 256):
                x, y = X0 + tx, Y0 + ty; sl = (slice(ty * 256, ty * 256 + 256), slice(tx * 256, tx * 256 + 256))
                if (x + y) % 2 == 0:
                    holes += 1; bad += int(m[sl].max() != 0)
                else:
                    bad += int(m[sl].min() != 255 or (a[0][sl] != x % 256).any() or (a[1][sl] != y % 256).any()
                               or (a[2][sl] != 77).any())
        check(W_ % 256 == 0 and H_ % 256 == 0 and holes > 0 and bad == 0,
              f"every pixel is its server tile's, exactly; 404 tiles transparent ({bad} bad of {(W_ // 256) * (H_ // 256)} tiles)")
        ds = None
    # status / verify on a raw cache that isn't finished yet (no out=: tiles stay; a broken tile is found)
    cat.write_text(cat.read_text() + t.split("[source.ttms]")[1].replace('dest = "ttms"', 'dest = "ttmsv"')
                   .replace('out = "data/t/ttms.cog.tif"\n', '').join(["\n[source.ttmsv]", ""]))
    lm = lambda *a: subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), *a, "--json"], capture_output=True,
                                   text=True, env=env, timeout=280)
    r_ = lm("data", "fetch", "ttmsv", "--yes"); dv = root / "ttmsv"
    import json as _j
    check(r_.returncode == 0 and _j.loads((dv / "_manifest.json").read_text()).get("fmt") == "raw"
          and len(list(dv.glob("*.done"))) == 2, f"no out=: raw cache, 2 unit markers ({r_.stderr.strip().splitlines()[-1:]})")
    tl = sorted((f for f in (dv / "tiles").rglob("*") if f.is_file()), key=lambda f: -f.stat().st_size)
    check(bool(tl), "raw tiles kept in the cache")
    if tl:
        tl[0].write_bytes(tl[0].read_bytes()[:200])                 # a broken tile
        v1 = _j.loads(lm("data", "verify", "ttmsv").stdout or "{}"); kept = tl[0].is_file()
        v2 = _j.loads(lm("data", "verify", "ttmsv", "--yes").stdout or "{}")
        check(v1.get("bad") and kept and v2.get("bad") and not tl[0].is_file() and not list(dv.glob("*.done")),
              f"verify finds the broken tile; --yes deletes it and reopens the units ({v1.get('bad')}, {v2.get('bad')})")
        n1 = seen["n"]; r_ = lm("data", "fetch", "ttmsv", "--yes")
        check(r_.returncode == 0 and seen["n"] - n1 == 1 and tl[0].is_file(), f"re-fetch asks only for that tile ({seen['n'] - n1} requests)")
        mk = _j.loads(sorted(dv.glob("*.done"))[0].read_text())
        check(mk.get("z") == Z and len(mk.get("box") or []) == 4, f"unit marker records its tile box ({mk})")
        tl[1].unlink()                                               # a tile vanished from a done unit
        v3 = _j.loads(lm("data", "verify", "ttmsv", "--quick").stdout or "{}")
        check(any("missing" in b for b in v3.get("bad") or []), f"verify --quick finds a tile missing from a done unit ({v3.get('bad')})")
        lm("data", "verify", "ttmsv", "--yes"); n1 = seen["n"]; r_ = lm("data", "fetch", "ttmsv", "--yes")
        check(r_.returncode == 0 and seen["n"] - n1 == 1 and tl[1].is_file(), f"... and the next fetch gets just that one ({seen['n'] - n1})")
        src_ = (HERE / "lmaplib" / "02_kinds.py").read_text()
        i_ = src_.index("def _finish_raw(")
        check(src_.index("raw_missing(d, have)", i_) < src_.index("build_cog(", i_),
              "finish checks for missing tiles before starting the build")
    r_ = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "ttms30", "--yes", "--json"],
                        capture_output=True, text=True, env=env, timeout=280)
    o3 = root / "data/t/ttms30.cog.tif"
    check(r_.returncode == 0 and o3.is_file(), f"raw cache, res_m 30 (coarser than the zoom-13 grid): built ({r_.stderr.strip().splitlines()[-3:]})")
    if o3.is_file():
        ds = gdal.Open(str(o3)); gt = ds.GetGeoTransform()
        check(abs(gt[1] - 30) < 1e-9 and abs(gt[0] / 30 - round(gt[0] / 30)) < 1e-6, f"one warp to res_m on the -tap grid ({gt[:2]})")
        R = 6378137.0; lon, lat = 146.753, -19.271
        mx_, my_ = math.radians(lon) * R, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
        x, y = int((mx_ + WM) // (256 * px)), int((WM - my_) // (256 * px))
        cx, cy = -WM + (x + 0.5) * 256 * px, WM - (y + 0.5) * 256 * px     # tile centre: away from edges
        v = [int(ds.GetRasterBand(b).ReadAsArray(int((cx - gt[0]) / gt[1]), int((cy - gt[3]) / gt[5]), 1, 1)[0, 0]) for b in (1, 2, 3)]
        mk = int(ds.GetRasterBand(1).GetMaskBand().ReadAsArray(int((cx - gt[0]) / gt[1]), int((cy - gt[3]) / gt[5]), 1, 1)[0, 0])
        want = (0, 0, 0, 0) if (x + y) % 2 == 0 else (x % 256, y % 256, 77, 255)
        check(all(abs(a_ - b_) <= 6 for a_, b_ in zip(v + [mk], want)), f"tile {x},{y} centre after the warp ({v}, mask {mk} vs {want})")
        ds = None

# an empty server (every tile 404: an outage, seen on QLD imagery) must not build a valid-looking blank file
if not os.environ.get("T_TMS_GDAL"):
    seen["outage"] = 1
    with open(cat, "a") as f_:
        f_.write("\n[source.ttms-out]" + cat.read_text().split("[source.ttms]")[1].split("\n[source.")[0]
                 .replace('dest = "ttms"', 'dest = "ttms-out"').replace("ttms.cog", "ttms-out.cog"))
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "ttms-out", "--yes"],
                       capture_output=True, text=True, env=env, timeout=280)
    o_ = root / "data/t/ttms-out.cog.tif"
    check(r.returncode != 0 and not o_.exists() and "no imagery at all" in r.stderr,
          f"all-empty server: refused, nothing built (rc {r.returncode}, {r.stderr.strip()[-160:]})")
    left = [f for f in (root / "ttms-out").rglob("*") if f.is_file()] if (root / "ttms-out").exists() else []
    check(not left, f"all-empty server: blank tiles + markers dropped, next fetch asks again ({len(left)} left)")
    seen["outage"] = 0

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
