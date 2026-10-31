#!/usr/bin/env python3
"""LiDAR 3D terrain from the QldDem elevation cache (no network, no network share; the tile server is mocked by a
local HTTP server serving real LERC tiles written by GDAL):
1. lidar_zoom: 5 m -> z15, 2 m -> z17, 1 m -> z18, never past max_zoom.
2. lidar_units: only units overlapping the coverage (edge contact skipped).
3. au_lidar_res: default 5, remembered, 0 = off, bad value refused, bad stored value -> 5 + warning.
4. lidar_fetch through the shared TileFetcher: right tiles requested, unit assembled with heights > 255 m
   (LERC read as Float32, not Byte), georeferenced on the cache grid, 404 -> nodata; resume; a stop ->
   E_DRAINED with done units kept; a tile that always 500s -> E_UPSTREAM; per-source max_conns caps.
5. lidar_stale: missing unit / unit newer / out older than the 10 m patch -> rebuild; else not.
6. au project's LiDAR step: a stop / abort is raised, not swallowed by the fallback; cut sized to the box.
7. coverage: kept however old; re-asked only with --refresh-live / --refresh, offline keeps the old copy."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import argparse, http.server, importlib.machinery, importlib.util, json, os, shutil, sys, tempfile, threading, time, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
import numpy as np, tomllib
from osgeo import gdal, ogr, osr
gdal.UseExceptions()
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

tmp = Path(tempfile.mkdtemp())
L.STATE = tmp / "state"
logs = []
src = dict(tomllib.loads((HERE / "sources.toml").read_text())["source"]["lidar-qld"])
ctx = types.SimpleNamespace(a=argparse.Namespace(dry_run=False), log=logs.append, vlog=logs.append,
                            scratch=lambda: tmp, prefix="", verbose=False, shrink=2, sources={"lidar-qld": src})
sr = osr.SpatialReference(); sr.ImportFromEPSG(3857)
def gpkg(p, wkt):
    dr = ogr.GetDriverByName("GPKG").CreateDataSource(str(p)); lay = dr.CreateLayer("cov", sr, ogr.wkbPolygon)
    f = ogr.Feature(lay.GetLayerDefn()); f.SetGeometry(ogr.CreateGeometryFromWkt(wkt)); lay.CreateFeature(f); dr = None

# 1. zoom
check((L.lidar_zoom(5), L.lidar_zoom(2), L.lidar_zoom(1), L.lidar_zoom(0.1, 18)) == (15, 17, 18, 18),
      f"zoom: 5->15 2->17 1->18 0.1->18 ({L.lidar_zoom(5)},{L.lidar_zoom(2)},{L.lidar_zoom(1)},{L.lidar_zoom(0.1)})")

# 2. units: small test unit (2 tiles a side) at z17; coverage = exactly unit (ux0, uy0)'s square
z, n = 17, 2
src["unit"] = n
tm = 2 * L.MERC / 2 ** z; um = tm * n
ux0, uy0 = int((16335745 + L.MERC) // um), int((L.MERC + 2194417) // um)
x0, y1 = -L.MERC + ux0 * um, L.MERC - uy0 * um
cov = tmp / "cov.gpkg"
gpkg(cov, f"POLYGON(({x0 + 1} {y1 - um + 1},{x0 + um - 1} {y1 - um + 1},{x0 + um - 1} {y1 - 1},{x0 + 1} {y1 - 1},{x0 + 1} {y1 - um + 1}))")
box = (x0 - 3 * um, y1 - 4 * um, x0 + 4 * um, y1 + 3 * um)
u = L.lidar_units(box, z, n, cov)
check(u == [(ux0, uy0)], f"units: only the unit under the coverage ({u} vs {(ux0, uy0)})")
cov2 = tmp / "cov2.gpkg"
gpkg(cov2, f"POLYGON(({x0 + 1} {y1 - um + 1},{x0 + 3 * um - 1} {y1 - um + 1},{x0 + 3 * um - 1} {y1 - 1},{x0 + 1} {y1 - 1},{x0 + 1} {y1 - um + 1}))")
units3 = sorted(L.lidar_units(box, z, n, cov2))
check(units3 == [(ux0, uy0), (ux0 + 1, uy0), (ux0 + 2, uy0)], f"units: 3 across ({units3})")

# 3. resolution
A = tmp / "au"; A.mkdir()
check(L.au_lidar_res(ctx, A) == 5 and (A / "lidar" / "res").read_text() == "5", "res: default 5, remembered")
ctx.a.lidar_res = 2.0
check(L.au_lidar_res(ctx, A) == 2, "res: --lidar-res 2")
ctx.a.lidar_res = None
check(L.au_lidar_res(ctx, A) == 2, "res: plain re-run keeps 2")
ctx.a.lidar_res = 0.0
check(L.au_lidar_res(ctx, A) == 0, "res: 0 = off")
ctx.a.lidar_res = 20.0
try:
    L.au_lidar_res(ctx, A); check(False, "res: 20 refused")
except L.CliError as e:
    check(e.code == L.E_USAGE, "res: 20 refused")
ctx.a.lidar_res, ctx.a.dry_run = 3.0, True
L.au_lidar_res(ctx, A)
check((A / "lidar" / "res").read_text() == "0", "res: a dry run doesn't remember")
ctx.a.lidar_res, ctx.a.dry_run = None, False
(A / "lidar" / "res").write_text("99")
check(L.au_lidar_res(ctx, A) == 5 and any("using 5 m" in m for m in logs), "res: bad stored value -> 5 m + warning")

# 4. fetch from a local tile server: LERC tiles, height = 300 + row (so > 255), one 404 tile, one 500 tile on demand
def lerc(val):
    p = f"/vsimem/t{val}.tif"
    d = gdal.GetDriverByName("MRF").Create(p, 257, 257, 1, gdal.GDT_Float32, ["COMPRESS=LERC", "OPTIONS=V1:1"])
    a = np.full((257, 257), val, "f4"); a[:, 256] = -1; a[256, :] = -1          # the overlap px must not be used
    d.GetRasterBand(1).WriteArray(a); d = None
    for f in gdal.ReadDir("/vsimem/") or []:
        if f.startswith(f"t{val}") and f.endswith(".lrc"):
            b = gdal.VSIFOpenL(f"/vsimem/{f}", "rb"); gdal.VSIFSeekL(b, 0, 2); sz = gdal.VSIFTellL(b); gdal.VSIFSeekL(b, 0, 0)
            data = gdal.VSIFReadL(1, sz, b); gdal.VSIFCloseL(b); return data
    raise SystemExit("no .lrc written")
reqs, missing, broken, conc = [], set(), set(), [0, 0]
lk = threading.Lock()
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        with lk:
            conc[0] += 1; conc[1] = max(conc[1], conc[0])
        try:
            p = self.path.split("/")
            zz, y, x = int(p[-3]), int(p[-2]), int(p[-1])
            reqs.append((zz, y, x))
            time.sleep(0.01)
            if (y, x) in missing:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            if (y, x) in broken:
                self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
            b = lerc(300.0 + y % 7 + (x % 5) / 10)
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        finally:
            with lk:
                conc[0] -= 1
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
src["url"] = f"http://127.0.0.1:{srv.server_port}/tile/${{z}}/${{y}}/${{x}}"
L.SERVERS["127.0.0.1"] = {"start": 4, "max": 48, "slots": 8}
src["conns"], src["max_conns"] = 2, 3
missing.add((uy0 * n + 1, ux0 * n + 1))
d = tmp / "lid"
got = L.lidar_fetch(ctx, box, z, d, cov2)
check(len(got) == 3 and all(p.is_file() for p in got) and len(reqs) == 3 * n * n + 2,
      f"fetch: 3 units, {3 * n * n} tiles requested (+2: 404 asked 3x) ({len(reqs)})")
check(conc[1] <= 3, f"fetch: per-source max_conns=3 respected (peak {conc[1]})")
check({r[0] for r in reqs} == {z}, "fetch: asked at the chosen zoom")
g = gdal.Open(str(L.lidar_unit_path(d, z, (ux0, uy0)))); a = g.GetRasterBand(1).ReadAsArray(); gt = g.GetGeoTransform()
check(a.shape == (n * 256, n * 256), f"unit: {n}x256 px a side (overlap px dropped) {a.shape}")
r_ = tm / 256
check(abs(gt[0] - (x0 - r_ / 2)) < 1e-6 and abs(gt[3] - (y1 + r_ / 2)) < 1e-6 and abs(gt[1] - r_) < 1e-9,
      f"unit: pixel 0 centred on the tile corner (grid-point values) ({gt[0]:.3f},{gt[3]:.3f})")
ok = a > -1000
check(ok.sum() == 3 * 256 * 256 and a[ok].min() > 255 and (a[ok] > 299).all(),
      f"unit: heights > 255 m kept (LERC read as Float32), 404 tile = nodata ({a[ok].min():.2f}..{a.max():.2f})")
check(abs(a[0, 0] - (300 + (uy0 * n) % 7 + ((ux0 * n) % 5) / 10)) < 0.06 and abs(a[0, 256] - (300 + (uy0 * n) % 7 + ((ux0 * n + 1) % 5) / 10)) < 0.06,
      "unit: tiles in the right place")
check(not list(tmp.glob("lidar-z*.tiles")), "fetch: raw tile dirs cleaned after assembly")
pj = json.loads((L.STATE / f"au-lidar-z{z}.json").read_text())
check(pj["left"] == 0 and pj["final"] == "done" and pj["total"] == 3, f"progress sane ({pj['left']} left, {pj['final']})")
reqs.clear(); got[0].unlink()
L.lidar_fetch(ctx, box, z, d, cov2)
check(len(reqs) == n * n + (2 if got[0].stem == f"{ux0}_{uy0}" else 0), f"resume: only the missing unit's tiles ({len(reqs)})")
got[0].unlink(); got[1].unlink(); L.DRAIN.set()
try:
    L.lidar_fetch(ctx, box, z, d, cov2); check(False, "stop raises E_DRAINED")
except L.CliError as e:
    check(e.code == L.E_DRAINED and got[2].is_file(), f"stop raises E_DRAINED, done units kept ({e.msg})")
L.DRAIN.clear()
L.lidar_fetch(ctx, box, z, d, cov2)
got[1].unlink()
uy, ux = int(got[1].stem.split("_")[1]), int(got[1].stem.split("_")[0])
broken.add((uy * n, ux * n))
L.ABORT.wait = lambda t=None: False                     # no real backoff sleeps in the test
try:
    L.lidar_fetch(ctx, box, z, d, cov2); check(False, "a tile that always fails -> E_UPSTREAM")
except L.CliError as e:
    check(e.code == L.E_UPSTREAM and got[0].is_file() and not got[1].is_file(), f"a tile that always fails -> E_UPSTREAM ({e.msg})")
broken.clear()
L.lidar_fetch(ctx, box, z, d, cov2)
check(not list((d / f"z{z}").glob("*.part.tif")), "no part files left in the units folder")
check(not list(tmp.glob(f"lidar-z{z}-*.tiles")), "scratch tile dirs cleaned after a full success")
# a 0-byte tile (power cut) is NOT taken as "no data": it is refetched
got[0].unlink(); td = tmp / f"lidar-z{z}-{got[0].stem}.tiles"; td.mkdir()
ux_, uy_ = map(int, got[0].stem.split("_")); (td / f"{uy_ * n}_{ux_ * n}").write_bytes(b"")
try:
    L.lidar_fetch(ctx, box, z, d, cov2)
except L.CliError:
    pass
L.lidar_fetch(ctx, box, z, d, cov2)
g0 = gdal.Open(str(got[0])); a0 = g0.GetRasterBand(1).ReadAsArray(); g0 = None
check((a0[:256, :256] > 299).all(), "a 0-byte tile is refetched, not read as no-data")

# 5. staleness (box/zoom/unit as above)
cd_ = tmp / "cd"; (cd_ / f"z{z}").mkdir(parents=True)
shutil.copy(cov2, cd_ / "lidar-coverage.gpkg")
L.au_3d_box = lambda: box
L.lidar_zoom = lambda res, mz=18: z
for p in got:
    shutil.copy(p, cd_ / f"z{z}" / p.name)
fine, out = tmp / "fine.tif", tmp / "out.tif"
fine.write_text("x"); out.write_text("x")
old = time.time() - 100
for p in (cd_ / f"z{z}").iterdir():
    os.utime(p, (old, old))
os.utime(fine, (old, old))
check(not L.lidar_stale(ctx, fine, out, 2, cd_), "stale: complete + newer out -> no rebuild")
(cd_ / f"z{z}" / got[1].name).unlink()
check(L.lidar_stale(ctx, fine, out, 2, cd_), "stale: missing unit -> rebuild")
shutil.copy(got[1], cd_ / f"z{z}" / got[1].name)
check(L.lidar_stale(ctx, fine, out, 2, cd_), "stale: unit newer than out -> rebuild")
os.utime(cd_ / f"z{z}" / got[1].name, (old, old)); os.utime(fine, None); os.utime(out, (old + 50, old + 50))
check(L.lidar_stale(ctx, fine, out, 2, cd_), "stale: out older than the 10 m patch -> rebuild")
check(L.lidar_stale(ctx, fine, tmp / "nope.tif", 2, cd_), "stale: missing out -> rebuild")

# 6. the caller re-raises a stop / abort, falls back on anything else (source check)
code = lmap_source()
i = code.index('au_dem3d_lidar(ctx, d3p, top, lr, A / "lidar")'); blk = code[i:i + 900]
check("except Aborted:\n            raise" in blk and "E_DRAINED" in blk and "except Exception" in blk,
      "au project: a LiDAR stop/abort is raised; other errors fall back")
check('f"NUM_THREADS={nt}", "-te", *map(str, box)' in code and '"-cutline", cov' not in code
      and '"gdal_rasterize", "-q", "-i", "-burn", "-32768", cov, cut' in code,
      "cut: sized to the 3D box; outside the coverage burned to nodata (no slow -cutline)")

# 7. coverage refresh
cf = tmp / "cv" / "lidar-coverage.gpkg"; cf.parent.mkdir(); shutil.copy(cov2, cf)
asked = []
def fake_cov(c, b, o):
    asked.append(1); raise L.CliError(L.E_UPSTREAM, "offline")
L._lidar_coverage = fake_cov
check(L.lidar_coverage(ctx, box, cf) == cf and not asked, "coverage: fresh copy used, no query")
os.utime(cf, (time.time() - 40 * 86400,) * 2)
check(L.lidar_coverage(ctx, box, cf) == cf and not asked, "coverage: 40 days old -> kept, no query")
_rl = getattr(ctx.a, "refresh_live", None); ctx.a.refresh_live = True
try:
    check(L.lidar_coverage(ctx, box, cf) == cf and asked, "coverage: --refresh-live -> re-asked; offline keeps the old copy")
finally:
    ctx.a.refresh_live = _rl

srv.shutdown()
print("ALL PASS" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
