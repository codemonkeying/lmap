# lmap: terrain_slope. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: slope
# One integer-degree slope file per DEM (255 = no data), styled three ways in QGIS (walk/4WD, bushfire AS 3959,
# plain ramp). LiDAR (2 m) is averaged to 10 m first: at 2 m creek banks, road cuts and rocks read as cliffs.
# DEMs are Web Mercator, so each row's gradient is divided by its true metres per pixel (px * cos(lat)).
SLOPE_SRC = (("dem-3d-30m.cog.tif", 1, "slope-30m.cog.tif"),          # (DEM, average NxN first, output)
             ("dem-3d-2m-3dview.cog.tif", 5, "slope-10m-lidar.cog.tif"))
SLOPE_PX = int(os.environ.get("LMAP_SLOPE_PX", 16_000_000))  # (env: tests force small strips)
#      # pixels per strip: ~0.5 GB of float32 temporaries per process (1024 rows of the
                            # 155,849 px wide 30 m DEM was ~5 GB each and got OOM-killed with 4 processes)


def _pool_quiet():
    signal.signal(signal.SIGINT, signal.SIG_IGN)          # Ctrl-C goes to the parent, which cancels the pool


def _grad(a, axis):
    """d/dpx along axis: central difference, one-sided where a neighbour is NaN or off the edge (np.gradient
    would make every cell next to a void, the coast or the LiDAR edge NaN: a lost 1 px ring)."""
    import numpy as np
    p = np.pad(a, [(1, 1) if k == axis else (0, 0) for k in range(2)], constant_values=np.nan)
    sl = lambda o: tuple(slice(1 + o, p.shape[k] - 1 + o) if k == axis else slice(None) for k in range(2))
    lo, hi = p[sl(-1)], p[sl(1)]
    g = (hi - lo) / 2
    g = np.where(np.isnan(g), hi - a, g)
    return np.where(np.isnan(g), a - lo, g)


def _slope_strip(args):
    src, agg, y0, rows, W, H, px, gt3, nd = args
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(src); b = ds.GetRasterBand(1)          # keep ds alive: a band outlives a freed dataset badly
    h = min(rows, H - y0); ry0, ry1 = max(0, y0 - 1), min(H, y0 + h + 1)
    kw = dict(buf_xsize=W, buf_ysize=ry1 - ry0, resample_alg=gdal.GRIORA_Average) if agg > 1 else {}
    a = b.ReadAsArray(0, ry0 * agg, W * agg, (ry1 - ry0) * agg, **kw).astype("float32")
    bad = ~np.isfinite(a)
    if nd is not None:
        bad |= a == nd
    if bad.all():
        return y0, None
    a[bad] = np.nan
    ys = gt3 - (np.arange(ry0, ry1) + 0.5) * px
    k = (px * np.cos(np.arctan(np.sinh(ys / 6378137.0)))).astype("float32")[:, None]   # true m per px, per row
    if a.shape[0] < 2 or a.shape[1] < 2:
        return y0, None
    keep = bad[(y0 - ry0):(y0 - ry0) + h]; del bad
    gy, gx = _grad(a, 0), _grad(a, 1)                                 # float32 in, float32 out
    del a
    g = np.hypot(gx, gy); del gx, gy
    g /= k; np.arctan(g, out=g); np.degrees(g, out=g)
    g = g[(y0 - ry0):(y0 - ry0) + h]
    out = np.full(g.shape, 255, "uint8"); ok = np.isfinite(g) & ~keep
    out[ok] = np.clip(np.rint(g[ok]), 0, 90).astype("uint8")
    return y0, out


def make_slope(ctx, src, agg, out):
    """Slope (degrees, Byte, 255 nodata) of src as a COG at out. Strips in parallel into a scratch GeoTIFF,
    then a COG with NEAREST overviews (band edges stay crisp), verified, copied to the network share."""
    import concurrent.futures as cf
    from osgeo import gdal
    gdal.UseExceptions()
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        ds = gdal_open(src); W, H = ds.RasterXSize // agg, ds.RasterYSize // agg   # drops < agg edge px
        gt = ds.GetGeoTransform(); px = gt[1] * agg; nd = ds.GetRasterBand(1).GetNoDataValue()
        sc = ctx.scratch(); raw = sc / f"{out.stem}.{os.getpid()}.raw.tif"; part = sc / f"{out.stem}.{os.getpid()}.part.tif"
        nt = slot.threads()
        try:
            o = gdal.GetDriverByName("GTiff").Create(str(raw), W, H, 1, gdal.GDT_Byte, [
                "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=ZSTD", "BIGTIFF=YES", "SPARSE_OK=TRUE"])
            o.SetGeoTransform((gt[0], px, 0, gt[3], 0, -px)); o.SetProjection(ds.GetProjection())
            ob = o.GetRasterBand(1); ob.SetNoDataValue(255)        # unwritten sparse blocks read as 255
            rows = max(16, min(2048, SLOPE_PX // max(1, W)))
            jobs = [(str(src), agg, y0, rows, W, H, px, gt[3], nd) for y0 in range(0, H, rows)]
            ctx.log(f"building {out.name}: {W}x{H} px at {px:g} m, {len(jobs)} strips, {nt} processes")
            t0 = time.time(); last = t0
            ex = cf.ProcessPoolExecutor(nt, initializer=_pool_quiet); clean = False
            try:
                for i, (y0, r) in enumerate(ex.map(_slope_strip, jobs)):
                    if ABORT.is_set():
                        raise Aborted()
                    if r is not None:
                        ob.WriteArray(r, 0, y0)
                    if time.time() - last > 60:
                        last = time.time(); ctx.log(f"  slope {i + 1}/{len(jobs)} strips, {time.time() - t0:.0f} s")
                clean = True
            except cf.process.BrokenProcessPool:
                raise CliError(E_FAIL, f"{out.name}: a slope worker died (out of memory?)",
                               "check `dmesg | grep -i oom`; LMAP_SLOPE_PX=4000000 uses smaller strips")
            finally:
                ex.shutdown(wait=clean, cancel_futures=True)     # on abort/error: don't wait for queued strips
            st = Path(src).stat(); o.SetMetadataItem("LMAP_SLOPE_FROM", f"{Path(src).name} {st.st_size} {int(st.st_mtime)}")
            ob = None; o = None
            ctx.log(f"slope done in {time.time() - t0:.0f} s; COG + overviews")
            _run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
                  "-co", "BIGTIFF=YES", "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=NEAREST",
                  "-co", f"NUM_THREADS={nt}", "--config", "GDAL_CACHEMAX", str(BUILD_CACHE[0]), raw, part], ctx)
            raw.unlink(missing_ok=True)
            verify_cog(ctx, part, part)
            install_file(part, out)
        finally:
            raw.unlink(missing_ok=True); part.unlink(missing_ok=True)


def slope_from(p):
    try:                                               # unreadable: unknown source -> rebuilt
        return gdal_open(p).GetMetadataItem("LMAP_SLOPE_FROM")
    except CliError:
        return None


def slope_plan(ctx):
    A = ctx.path("data/australia"); plan = []
    for src, agg, outn in SLOPE_SRC:
        sp, op = A / src, A / outn
        if not sp.is_file():
            continue
        stamp = slope_from(op) if op.is_file() else None
        stale = not op.is_file() or stamp != f"{sp.name} {sp.stat().st_size} {int(sp.stat().st_mtime)}"   # no stamp: rebuild
        plan.append({"from": src, "out": outn, "res_m": None, "build": stale or bool(ctx.a.refresh),
                     "exists": op.is_file(), "src": sp, "agg": agg, "path": op})
    return plan


def cmd_terrain_slope(ctx):
    """Slope layers for australia.qgz: 30 m everywhere + 10 m (from the 2 m LiDAR) where we have it."""
    need_root(ctx)
    plan = slope_plan(ctx)
    if not plan:
        raise CliError(E_NOTFOUND, "no terrain to make slope from", "lmap au project --yes builds data/australia/dem-3d-*.cog.tif")
    from osgeo import gdal
    gdal.UseExceptions()
    for p in plan:
        g = gdal_open(p["src"]).GetGeoTransform(); p["res_m"] = round(g[1] * p["agg"], 2)   # mercator px
    show = [{k: v for k, v in p.items() if k in ("from", "out", "res_m", "build", "exists")} for p in plan]
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "terrain.slope", "layers": show,
                "note": "res_m = Web Mercator pixel (ground is res x cos(lat): 28 m at 19S). Measured once on "
                        "8 cores: 30 m all Australia 11 min + COG/verify 9 min, 3.2 GB; LiDAR 10 m 2 min, 0.1 GB"}
    if not any(p["build"] for p in plan):
        return {"ok": True, "action": "terrain.slope", "layers": show, "unchanged": True,
                "hint": "already built; --refresh rebuilds; lmap au project --yes adds them to the map"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to build slope layers without --yes", "preview: lmap terrain slope --dry-run")
    install_signals()
    t0 = time.time()
    bad = []
    for p, s in zip(plan, show):
        if p["build"]:
            try:
                make_slope(ctx, p["src"], p["agg"], p["path"])
            except CliError as e:                      # the other layer still gets built
                s["error"] = e.msg; bad.append(p["out"])
    for p, s in zip(plan, show):
        s["built"] = s.pop("build") and "error" not in s
        if p["path"].is_file():
            s["gb"] = round(p["path"].stat().st_size / 1e9, 2)
    if bad:
        raise CliError(E_FAIL, f"slope failed for {', '.join(bad)}", json.dumps(show))
    return {"ok": True, "action": "terrain.slope", "layers": show, "seconds": round(time.time() - t0),
            "next": "lmap au project --yes  (adds 'Slope' to australia.qgz, off by default)"}
