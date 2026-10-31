# lmap: terrain_viewshed. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: viewshed (gdal_viewshed)
# A window of the finest DEM that covers it is warped to the observers' UTM zone (square metres, MGRS grid),
# gdal_viewshed runs once per observer, and the results are added: each cell = how many observers see it.
VS_MAX_PTS, VS_MAX_KM = 20, 60


def vs_pick_dem(ctx, pts, rad_m):
    """Finest DEM with DATA all round every observer's circle; 2 m only up to a ~30 km window. -> (dem, ground m)"""
    import numpy as np
    from osgeo import osr
    osr.UseExceptions()
    lat0 = sum(p["pos"][0] for p in pts) / len(pts); cl = math.cos(math.radians(lat0))
    span = rad_m * 2 + max(_m_between(pts[0]["pos"], p["pos"]) for p in pts)
    lons, lats = [], []                       # each centre + 16 points on its circle + 8 at half radius
    for p in pts:
        lons.append(p["pos"][1]); lats.append(p["pos"][0])
        for r_, n_ in ((rad_m * 0.999, 16), (rad_m / 2, 8)):
            for i in range(n_):
                t = 2 * math.pi * i / n_
                lats.append(p["pos"][0] + r_ * math.cos(t) / 111320)
                lons.append(p["pos"][1] + r_ * math.sin(t) / (111320 * cl))
    for d in terrain_dems(ctx):
        g = d.res_m * (cl if d.srs.IsProjected() and (d.srs.GetAttrValue("PROJECTION") or "").startswith("Mercator") else 1)
        if g < 1.5 or (g <= 2.5 and span > 30000):
            continue
        if not np.isnan(d.sample(lons, lats)).any():      # data there, not just inside the file's bounds
            return d, g
    raise CliError(E_NOTFOUND, "no DEM covers the whole circle", "smaller --radius-km, or a point inside Australia")


def vs_angles(ctx, win, seen, oxy, eye_h, tgt_h, k, grid, prj, tmp, part):
    """Vertical angle (mils, + above / - below level) from the eye to each seen cell's target (ground + target
    height), with the same earth curvature + refraction as gdal_viewshed: drop = d^2 / 2R * (1 - k).
    Unseen = nodata. By row blocks (Float32 window can be ~600 MB)."""
    import numpy as np
    from osgeo import gdal
    x0, y1, res = grid; H, W = seen.shape
    wds = gdal_open(win); wb = wds.GetRasterBand(1)
    oc, orow = int((oxy[0] - x0) / res), int((y1 - oxy[1]) / res)
    zg = float(wb.ReadAsArray(oc, orow, 1, 1)[0, 0])
    if not math.isfinite(zg) or zg == -32768:
        wb = wds = None
        raise CliError(E_NOTFOUND, "no terrain under the observer (sea / data gap)", "move the point onto land")
    zo = zg + eye_h
    o = gdal.GetDriverByName("GTiff").Create(str(tmp), W, H, 1, gdal.GDT_Float32,
                                             ["TILED=YES", "COMPRESS=ZSTD", "BIGTIFF=IF_SAFER"])
    o.SetGeoTransform((x0, res, 0, y1, 0, -res)); o.SetProjection(prj)
    ob = o.GetRasterBand(1); ob.SetNoDataValue(-9999)
    xs = x0 + (np.arange(W) + 0.5) * res - oxy[0]
    lo, hi = 1e9, -1e9
    try:
        for r0 in range(0, H, 1024):
            if ABORT.is_set():
                raise Aborted()
            n = min(1024, H - r0)
            z = wb.ReadAsArray(0, r0, W, n).astype("float64")
            ys = (y1 - (np.arange(r0, r0 + n) + 0.5) * res - oxy[1])[:, None]
            dist = np.hypot(xs[None, :], ys)
            dz = z + tgt_h - dist * dist / (2 * EARTH_R) * (1 - k) - zo
            m = np.degrees(np.arctan2(dz, np.maximum(dist, res / 2))) * 6400 / 360
            vis = seen[r0:r0 + n] > 0
            if vis.any():
                lo, hi = min(lo, float(m[vis].min())), max(hi, float(m[vis].max()))
            ob.WriteArray(np.where(vis, m, -9999).astype("float32"), 0, r0)
    finally:
        ob = o = wb = wds = None
    # LERC to 0.05 mil (well under what a clinometer reads): 18 MB vs 142 MB plain float at 2 m, 10 km
    _run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=LERC_DEFLATE", "-co", "MAX_Z_ERROR=0.05",
          "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "BIGTIFF=IF_SAFER", tmp, part], ctx)
    return {"min": round(lo), "max": round(hi)} if hi > -1e9 else None
