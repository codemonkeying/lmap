#!/usr/bin/env python3
"""lmap GPU warp (0.12): stands in for
  gdalwarp -te_srs EPSG:4326 -te W S E N -t_srs EPSG:3857 -tr R R -tap -r average -srcnodata None -dstalpha
for a local z/y/x tile pyramid that is itself EPSG:3857 (pure scale + shift, no reprojection).
Tiles are decoded on the CPU with Pillow, GDAL's 'average' runs on the GPU (gpuwarp.cu), GDAL writes the GTiff
with the creation options given. Matches gdalwarp except for gdalwarp's own approximate-transformer noise.

usage: gpuwarp.py LIB SRC.xml OUT.tif W S E N RES WORKERS [CO ...]
Exit 0 = OUT complete (written as OUT.gpu.tmp, renamed at the end). Anything else: no OUT, no tmp; lmap runs
gdalwarp instead. Exit 3 = not a case this handles."""
import ctypes, html, os, re, signal, sys
import numpy as np
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait
from osgeo import gdal

gdal.UseExceptions()
STRIP, EPS = 512, 1e-10
G = {}
OK_MODES = ("RGB", "RGBA", "L", "LA", "P", "PA")           # anything else (CMYK, 16-bit, float): gdalwarp


def skip(why):
    """Not a case this handles (not a GPU fault): lmap uses gdalwarp without counting it as a failure."""
    print(f"LMAP_GPU_SKIP: {why}", file=sys.stderr); sys.exit(3)


def span(n, g0, gr, i0, i1):
    """Per output pixel along one axis: GDAL's source span [lo,hi] and index range [s0,s1) (exact transform)."""
    d = np.arange(n, dtype=np.float64)
    a = i0 + (g0 + d * gr) * i1; b = i0 + (g0 + (d + 1) * gr) * i1
    lo = np.minimum(a, b); hi = np.maximum(a, b)
    s0 = np.floor(lo + EPS).astype(np.int64); s1 = np.ceil(hi - EPS).astype(np.int64)
    return lo, hi, s0, np.where(s0 == s1, s1 + 1, s1)


def init(g):
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    G.update(g)
    lib = ctypes.CDLL(g["libp"])
    P, I = ctypes.c_void_p, ctypes.c_int
    lib.run.argtypes = [P, I, I] + [P] * 8 + [P, I, I]; lib.run.restype = I
    G["lib"] = lib


def load(gx0, gx1, gy0, gy1):
    from PIL import Image
    T, td, z = G["bs"], G["tdir"], G["z"]
    a = np.zeros((gy1 - gy0, gx1 - gx0, 3), np.uint8)
    for ty in range(gy0 // T, (gy1 - 1) // T + 1):
        for tx in range(gx0 // T, (gx1 - 1) // T + 1):
            with Image.open(f"{td}/{z}/{ty}/{tx}") as im:      # missing / broken tile -> error -> gdalwarp path
                if im.mode not in OK_MODES:
                    raise ValueError(f"tile {z}/{ty}/{tx} is {im.mode}: left to gdalwarp")
                t = np.asarray(im.convert("RGB"))
            if t.shape[:2] != (T, T):
                raise ValueError(f"tile {z}/{ty}/{tx} is {t.shape[1]}x{t.shape[0]}, expected {T}")
            y0, x0 = ty * T, tx * T
            ys, ye = max(gy0, y0), min(gy1, y0 + T); xs, xe = max(gx0, x0), min(gx1, x0 + T)
            a[ys - gy0:ye - gy0, xs - gx0:xe - gx0] = t[ys - y0:ye - y0, xs - x0:xe - x0]
    return a


def strip(r0):
    W, H = G["W"], G["H"]; XL, XH, XI0, XI1 = G["X"]; YL, YH, YI0, YI1 = G["Y"]
    r1 = min(H, r0 + STRIP)
    gy0, gy1 = int(YI0[r0:r1].min()), int(YI1[r0:r1].max()); gx0, gx1 = int(XI0.min()), int(XI1.max())
    src = load(gx0, gx1, gy0, gy1)
    arrs = [np.ascontiguousarray(v, t) for v, t in (
        (XL - gx0, np.float64), (XH - gx0, np.float64), (XI0 - gx0, np.int32), (XI1 - gx0, np.int32),
        (YL[r0:r1] - gy0, np.float64), (YH[r0:r1] - gy0, np.float64), (YI0[r0:r1] - gy0, np.int32),
        (YI1[r0:r1] - gy0, np.int32))]
    out = np.empty((r1 - r0, W, 3), np.uint8)
    rc = G["lib"].run(src.ctypes.data_as(ctypes.c_void_p), src.shape[1], src.shape[0],
                      *[a.ctypes.data_as(ctypes.c_void_p) for a in arrs], out.ctypes.data_as(ctypes.c_void_p), W, r1 - r0)
    if rc:
        raise RuntimeError(f"CUDA error {rc}")
    return r0, out


def main():
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))   # run the cleanup below, not a silent exit
    libp, sx, outp, w, s, e, n, res, nw = sys.argv[1:10]
    co, nw = sys.argv[10:], int(nw)
    if any("=" not in c for c in co):
        skip("bad creation option")
    text = open(sx).read()
    m = re.search(r"file://(.*?)/\$\{z\}/\$\{y\}/\$\{x\}", text)
    if not m:
        skip("not a local z/y/x pyramid")
    tdir = html.unescape(m.group(1))
    z = int(re.search(r"<TileLevel>(\d+)</TileLevel>", text).group(1))
    m = re.search(r"<BlockSizeX>(\d+)</BlockSizeX>", text); bs = int(m.group(1)) if m else 256
    m = re.search(r"<BandsCount>(\d+)</BandsCount>", text)
    if (m and m.group(1) != "3") or re.search(r"<YOrigin>\s*bottom", text):
        skip("not 3-band top-origin")
    src = gdal.Open(sx); sgt = src.GetGeoTransform(); isg = gdal.InvGeoTransform(sgt)
    if sgt[2] or sgt[4] or src.RasterCount != 3:
        skip("rotated or not 3 bands")
    # the output grid exactly as gdalwarp picks it (a VRT costs nothing)
    v = gdal.Warp("", src, options=["-of", "VRT", "-te_srs", "EPSG:4326", "-te", w, s, e, n, "-t_srs", "EPSG:3857",
                                    "-tr", res, res, "-tap", "-r", "average"])
    gt, W, H, prj = v.GetGeoTransform(), v.RasterXSize, v.RasterYSize, v.GetProjection(); v = None
    X = span(W, gt[0], gt[1], isg[0], isg[1]); Y = span(H, gt[3], gt[5], isg[3], isg[5])
    if (X[2].min() < 0 or Y[2].min() < 0 or X[3].max() > src.RasterXSize or Y[3].max() > src.RasterYSize
            or X[3].max() - X[2].min() > 2**30):
        skip("unit runs off the tile grid")
    tmp = outp + ".gpu.tmp"
    ds = ex = None
    try:
        ds = gdal.GetDriverByName("GTiff").Create(tmp, W, H, 4, gdal.GDT_Byte, co)
        ds.SetGeoTransform(gt); ds.SetProjection(prj)
        for b, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand)):
            ds.GetRasterBand(b + 1).SetColorInterpretation(ci)
        alpha = np.full((STRIP, W), 255, np.uint8)
        ex = ProcessPoolExecutor(nw, initializer=init, initargs=(dict(libp=libp, tdir=tdir, z=z, bs=bs, W=W, H=H, X=X, Y=Y),))
        rows, run_, done = iter(range(0, H, STRIP)), set(), {}
        nxt = 0                                              # write strips in order; at most 2 per worker in flight
        while True:
            while len(run_) + len(done) < 2 * nw:
                r0 = next(rows, None)
                if r0 is None:
                    break
                run_.add(ex.submit(strip, r0))
            if not run_ and not done:
                break
            if run_:
                fin, run_ = wait(run_, return_when=FIRST_COMPLETED)
                for f in fin:
                    r0, out = f.result()                     # a worker error raises here
                    done[r0] = out
            while nxt in done:
                out = done.pop(nxt)
                for b in range(3):
                    ds.GetRasterBand(b + 1).WriteArray(out[:, :, b], 0, nxt)
                ds.GetRasterBand(4).WriteArray(alpha[:out.shape[0]], 0, nxt)
                nxt += STRIP
        ex.shutdown(wait=True); ex = None
        ds.FlushCache(); ds.Close(); ds = None                # Close() raises if the last blocks fail to write
        os.replace(tmp, outp)
    except BaseException:
        if ex is not None:
            procs = list((getattr(ex, "_processes", None) or {}).values())
            for p in procs:                                  # stop the decoders now, not after the queued strips
                try:
                    p.kill()
                except Exception:
                    pass
            ex.shutdown(wait=False, cancel_futures=True)
        ds = None
        for p in (tmp, outp):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise


if __name__ == "__main__":
    main()
