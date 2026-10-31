# lmap: sheets. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- sheets: GA topographic map sheets, on demand
# Indexes (public ArcGIS FeatureServers, GA): a sheet's eCAT id -> the eCAT catalogue search gives its download
# link (CloudFront). 250k AUSTopo (2025, GDA2020) are GeoPDFs: kept + cut to the map frame as a map layer.
# 100k / 50k (1961-2008, AGD66 grid) are plain scans with NO georeferencing: kept as print files only.
GA_SVC = "https://services1.arcgis.com/wfNKYeHsOyaFyPw3/arcgis/rest/services/"
SHEET_SERIES = {
    "250k": {"url": GA_SVC + "AUSTopo_MapSeriesIndex250k_FEB26/FeatureServer/2", "est_mb": 35,
             "sql": "SELECT TILENUMBER AS number, TILENAME AS name, CAST(New_Year AS INTEGER) AS year, "
                    "CAST(eCAT AS INTEGER) AS ecat, 1 AS available, geom FROM {t}"},
    "100k": {"url": GA_SVC + "AGOL_MapIndex100k/FeatureServer/0", "est_mb": 315,
             "sql": "SELECT MAPNUMBER AS number, MAPNAME AS name, Year_Published AS year, "
                    "PID_URL AS ecat, "
                    "(STATUS = 'Download Map') AS available, geom FROM {t}"},
    "50k": {"url": GA_SVC + "ArcGIS_Online_Map_Index_50k_new/FeatureServer/0", "est_mb": 315,
            "sql": "SELECT MAP_CODE AS number, trim(NAME) AS name, PUB_YEAR AS year, CATNO AS ecat, "
                   "1 AS available, geom FROM {t}"},
}
SHEETS_VER = "sheets v1"
ECAT_SEARCH = "https://ecat.ga.gov.au/geonetwork/srv/api/search/records/_search"


def sheets_dir(ctx):
    return ctx.path("data/australia") / "sheets"


def cmd_sheets_index(ctx):
    """data/australia/sheets/index.gpkg: where every GA 1:250k / 1:100k / 1:50k sheet is (number, name, year)."""
    need_root(ctx); need_tool("ogr2ogr")
    from osgeo import gdal
    gdal.UseExceptions()
    out = sheets_dir(ctx) / "index.gpkg"
    build = gpkg_meta(out, "lmap_key") != SHEETS_VER or bool(ctx.a.refresh) if out.is_file() else True
    plan = {"action": "sheets.index", "file": str(out), "exists": out.is_file(), "build": build,
            "series": list(SHEET_SERIES)}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not build:
        return {"ok": True, **plan, "unchanged": True, "hint": "already built; --refresh re-downloads"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to download the sheet indexes without --yes", "preview: lmap sheets index --dry-run")
    install_signals(); t0 = time.time()
    with Lock(STATE / "locks" / "out-sheets-index.lock", wait=ctx):
        part = ctx.scratch() / f"sheets-index.{os.getpid()}.part.gpkg"; raw = part.with_suffix(".raw.gpkg")
        counts = {}
        try:
            for i, (sc, sr) in enumerate(SHEET_SERIES.items()):
                if ABORT.is_set():
                    raise Aborted()
                raw.unlink(missing_ok=True)
                cr = http_get(sr["url"] + "/query?where=1%3D1&returnCountOnly=true&f=json", timeout=60, retries=4)
                try:
                    expect = int(json.loads(cr)["count"])
                except (TypeError, ValueError, KeyError):
                    raise CliError(E_UPSTREAM, f"{sc} index: no sheet count from GA", "re-run later")
                # the ESRIJSON driver pages by resultOffset (server max 1000-2000 a page); the exact count check below
                # catches a short or overlapping read (orderByFields= breaks the driver's URL detection)
                _run(["ogr2ogr", "-q", "-f", "GPKG", "-t_srs", "EPSG:4326", "-nlt", "MULTIPOLYGON", "-nln", "raw", raw,
                      sr["url"] + "/query?where=1%3D1&outFields=*&outSR=4326&f=json"], ctx)
                ds = gdal_open(raw, vector=True); Lr = ds.GetLayer(0)
                nd = Lr.GetFeatureCount(); Lr = ds = None
                if nd != expect:
                    raise CliError(E_UPSTREAM, f"{sc} index: got {nd} of {expect} sheets", "re-run later")
                _run(["ogr2ogr", "-q", "-f", "GPKG"] + (["-update"] if i else []) + ["-nln", f"s{sc}", "-nlt", "MULTIPOLYGON",
                      "-dialect", "SQLITE", "-sql", sr["sql"].format(t="raw"), part, raw], ctx)
                ds = gdal_open(part, update=True, vector=True); L = ds.GetLayerByName(f"s{sc}")
                counts[sc] = L.GetFeatureCount()

                if sc == "100k":                       # eCAT id = the trailing number of PID_URL
                    L.StartTransaction()
                    for f in L:
                        m = re.search(r"(\d+)\s*$", str(f["ecat"] or "")); f["ecat"] = m.group(1) if m else None; L.SetFeature(f)
                    L.CommitTransaction()
                L = None; ds = None
            ds = gdal_open(part, update=True, vector=True); ds.SetMetadataItem("lmap_key", SHEETS_VER); ds = None
            install_file(part, out)
        except Aborted:
            raise CliError(E_ABORTED, "stopped; old index (if any) kept")
        finally:
            for x in (part, raw):
                x.unlink(missing_ok=True)
    return {"ok": True, **plan, "sheets": counts, "seconds": round(time.time() - t0, 1),
            "next": "lmap sheets get --at POS --radius-km 50 --dry-run"}


def sheets_find(ctx, scale, lat, lon, rad_km):
    """Sheets of one series touching the circle (box test on degrees, generous)."""
    from osgeo import ogr
    ix = sheets_dir(ctx) / "index.gpkg"
    ds = ro_ogr(ix); L = ds.GetLayerByName(f"s{scale}")
    dl = rad_km / 111.32; dn = rad_km / (111.32 * math.cos(math.radians(lat)))
    L.SetSpatialFilterRect(lon - dn, lat - dl, lon + dn, lat + dl)
    c = ogr.Geometry(ogr.wkbPoint); c.AddPoint_2D(lon, lat)
    cl = math.cos(math.radians(lat))
    out = []
    for f in L:
        g = f.GetGeometryRef().Clone()                 # local equirectangular km around the point
        for gi in range(g.GetGeometryCount()):
            for ri in range(g.GetGeometryRef(gi).GetGeometryCount()):
                r = g.GetGeometryRef(gi).GetGeometryRef(ri)
                for pi in range(r.GetPointCount()):
                    x, y = r.GetPoint_2D(pi); r.SetPoint_2D(pi, (x - lon) * 111.32 * cl, (y - lat) * 111.32)
        o = ogr.Geometry(ogr.wkbPoint); o.AddPoint_2D(0, 0)
        dk = g.Distance(o)
        if dk > rad_km + 1e-6:
            continue
        out.append({"scale": scale, "number": f["number"], "name": (f["name"] or "").strip(), "year": f["year"],
                    "ecat": f["ecat"], "available": bool(f["available"]), "dist_km": round(dk, 1)})
    L = ds = None
    return sorted(out, key=lambda x: (x["dist_km"], x["number"]))


def sheet_url(ctx, ecat):
    """eCAT id -> the download link (cached in sheets/urls.json)."""
    cf = sheets_dir(ctx) / "urls.json"
    cache = json.loads(cf.read_text()) if cf.is_file() else {}
    if str(ecat) in cache:
        return cache[str(ecat)]
    if not str(ecat or "").isdigit():
        return None
    q = json.dumps({"query": {"query_string": {"query": f'"{ecat}"'}}, "size": 5, "_source": ["link", "resourceIdentifier"]})
    raw = None; last = None
    for i in range(4):
        try:
            rq = urllib.request.Request(ECAT_SEARCH, data=q.encode(), headers={"User-Agent": UA, "Content-Type": "application/json",
                                                                              "Accept": "application/json"})
            with host_slot(ECAT_SEARCH), OPENER.open(rq, timeout=60) as r:
                raw = read_capped(r, 16 << 20); break
        except ValueError as e:                    # over the size cap: not worth retrying
            raise CliError(E_UPSTREAM, f"GA catalogue search for {ecat}: {e}")
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if RetryPolicy.permanent(e):           # 401/405/410/451 won't change
                break
            RetryPolicy(4, 2, 60).wait(i, e)       # jittered, honours Retry-After, raises Aborted
    if raw is None:
        raise CliError(E_UPSTREAM, f"GA catalogue search failed for {ecat}: {last}")
    lst = lambda x: x if isinstance(x, list) else [x] if x else []
    try:
        hits = json.loads(raw)["hits"]["hits"]
    except (ValueError, KeyError, TypeError):
        raise CliError(E_UPSTREAM, f"GA catalogue: unexpected answer for {ecat}", "re-run later")
    url = None
    for h in hits:
        src = h.get("_source") or {}
        ids = [x.get("code", "") for x in lst(src.get("resourceIdentifier")) if isinstance(x, dict)]
        if not any(i_.rstrip("/").endswith(f"/{ecat}") for i_ in ids):
            continue
        for l in lst(src.get("link")):
            if not isinstance(l, dict):
                continue
            u = (l.get("urlObject") or {}).get("default", "")
            if "cloudfront.net/" in u and u.lower().endswith((".pdf", ".tif", ".tiff")):
                url = u; break
        if url:
            break
    if url:
        cache[str(ecat)] = url; cf.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cf, json.dumps(cache, indent=0))
    return url


def sheet_layer(ctx, pdf, out):
    """250k GeoPDF -> COG cut to the map frame (NEATLINE), 25 m (254 dpi), photo_co (WEBP/JPEG), transparent outside."""
    from osgeo import gdal
    ds = gdal_open(pdf); nl = ds.GetMetadataItem("NEATLINE"); sr = ds.GetSpatialRef(); ds = None
    if not nl or sr is None:
        raise CliError(E_FAIL, f"{pdf.name}: no map frame / georeferencing in the PDF")
    part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
    try:
        _run(["gdalwarp", "-q", "-overwrite", "-cutline", nl, "-cutline_srs", sr.ExportToWkt(),
              "-crop_to_cutline", "-dstalpha", "-of", "COG", *photo_co(ctx),
              "-co", "OVERVIEW_RESAMPLING=AVERAGE", pdf, part], ctx, env={**os.environ, "GDAL_PDF_DPI": "254"})
        if not gdal_ok(part, deep=True):
            raise CliError(E_FAIL, f"{out.name}: conversion failed its check", "re-run")
        install_file(part, out)
    finally:
        part.unlink(missing_ok=True)


# 100k / 50k scans -> map layers: the map frame (neatline) is a long black line; its 4 corners are the sheet's
# graticule corners (index.gpkg, whole 15'/30'), printed on AGD66. Corners -> GDA94 by the national 7-parameter
# AGD66 transform (EPSG:1278, ~5 m; the NTv2 grid isn't installed and the default PROJ pick for AGD66 -> GDA2020 is
# a NO-OP "ballpark" = ~200 m out) -> GDA2020 -> MGA. The scan is affine to AMG (paper + scanner, checked on the 4 corners); AMG -> MGA2020 goes in as
# a 10x10 GCP grid, gdalwarp -order 2, cut to the frame.
FIT_DARK = 140             # max(R,G,B) below this = black ink (a light frame on some sheets: 80-130)
FIT_MAX_RES_M = 60.0       # corners this far off a plain affine = refuse. Uneven paper shrink (taken up exactly at the
                           # corners) seen 2-46 m on GA scans; a wrong frame is mostly caught by FIT_SIZE_TOL
FIT_SLOPE_MAX = 0.012      # scans rotated more than this (0.7 deg) = not found (GA scans seen up to 0.006)
FIT_SIZE_TOL = 0.008       # the frame's size on the scan within 0.8% of the sheet's (paper shrink); < half the
                           # 1 km grid spacing, so a grid line can't stand in for a frame line


def sheet_scan_dpi(ds):
    """Scan resolution in dots per inch from the TIFF tags (pixels/cm on some sheets); 400 if not set."""
    try:
        v = float(ds.GetMetadataItem("TIFFTAG_XRESOLUTION") or 0)
    except ValueError:
        v = 0
    if (ds.GetMetadataItem("TIFFTAG_RESOLUTIONUNIT") or "").startswith("3"):
        v *= 2.54
    return v if 100 <= v <= 1200 else 400.0


def sheet_skew(dark):
    """Scan rotation as a slope (rows per col): the angle that makes the long frame/grid lines sharpest, on a 1/4
    sample (dark pixels sheared into row bins at each trial angle, sum of squares = sharpness)."""
    import numpy as np
    sm = dark[::4, ::4]; ys, xs = np.nonzero(sm)
    if ys.size > 3_000_000:
        k = np.random.default_rng(0).choice(ys.size, 3_000_000, replace=False); ys, xs = ys[k], xs[k]
    best = (-1, 0.0)
    for t in np.arange(-FIT_SLOPE_MAX, FIT_SLOPE_MAX + 1e-9, 0.0002):          # (~25 s on a 100k scan)
        hr = np.bincount(np.round(ys - t * xs).astype(np.int64) + sm.shape[1]); hc = np.bincount(np.round(xs + t * ys).astype(np.int64) + sm.shape[0])
        sc = float((hr.astype(np.float64) ** 2).sum() + (hc.astype(np.float64) ** 2).sum())
        if sc > best[0]:
            best = (sc, float(t))
    return best[1]


def sheet_frame(dark, pw, ph):
    """Neatline corners (col, row) NW, NE, SW, SE in a scan. dark = bool ink mask; pw/ph = expected frame size px.
    The scan's rotation first (sheet_skew); then dark-pixel profiles along it. Top+bottom (left+right) = the pair of
    long lines the sheet's size apart (FIT_SIZE_TOL); rows + cols scales must agree (a grid line can fake one side), then
    most ink. Then every 200 px along each side the line's centre, robust straight-line fits."""
    import numpy as np
    H, W = dark.shape
    t = sheet_skew(dark)
    pr, pc = np.zeros(H), np.zeros(W)                  # ink per line, lines at the scan's angle; central 80% only
    for r0 in range(0, H, 1024):                       # in strips: nonzero on the whole mask = GBs
        ys, xs = np.nonzero(dark[r0:r0 + 1024]); ys = ys.astype(np.float32) + r0; xs = xs.astype(np.float32)
        for along_rows, p_, n_ in ((True, pr, H), (False, pc, W)):
            if along_rows:
                k = (xs > W * .1) & (xs < W * .9); idx = np.round(ys[k] - t * (xs[k] - W / 2)).astype(np.int64)
            else:
                k = (ys > H * .1) & (ys < H * .9); idx = np.round(xs[k] + t * (ys[k] - H / 2)).astype(np.int64)
            p_ += np.bincount(idx[(idx >= 0) & (idx < n_)], minlength=n_)[:n_]
    ys = xs = idx = k = None

    def band(p, ln):                                   # a 3-px band (a tilted line spreads)
        p = p / ln
        return np.maximum(np.maximum(p, np.roll(p, 1)), np.roll(p, -1))

    def peaks(p, lo, hi):                               # local maxima (distinct lines) in p[lo:hi]
        lo, hi = max(1, lo), min(len(p) - 1, hi)
        return [i for i in range(lo, hi) if p[i] >= p[i - 1] and p[i] > p[i + 1]]

    def pairs(p, size, lim):                            # every strong line pair the sheet's size apart: (score, a, b)
        tol = size * FIT_SIZE_TOL; out = []
        for a in peaks(p, 0, int(lim - size + tol) + 1):   # any margin width
            if p[a] < 0.08:
                continue
            for b in peaks(p, int(a + size - tol), int(a + size + tol) + 1):
                out.append((float(min(p[a], p[b])), a, b))
        if not out or max(o[0] for o in out) < 0.1:
            raise CliError(E_FAIL, "map frame not found (no pair of long lines the sheet's size apart)")
        top = max(o[0] for o in out)
        return [o for o in out if o[0] >= 0.5 * top]
    pr, pc = band(pr, W * .8), band(pc, H * .8)
    # a 1 km grid line can sit just inside the frame and match the size within tol (8259-2 bottom: 53 px in). Paper
    # shrinks about evenly both ways: of the strong pairs, the rows + cols pair whose scales agree (0.3%), then the
    # most ink, then the closest scales
    R_, C_ = pairs(pr, ph, H), pairs(pc, pw, W)
    _, (_, t0, b0), (_, l0, r0) = min((((d_ := abs((rb - ra) / ph - (cb - ca) / pw)) > 0.003, -(rs + cs_), d_),
                                       (rs, ra, rb), (cs_, ca, cb)) for rs, ra, rb in R_ for cs_, ca, cb in C_)
    BIN, WIN = 200, 12

    def side(horizontal, c0, lo_, hi_):                # c0 = the line at the scan centre; follows the angle t
        pts = []
        for s0 in range(lo_, hi_ - BIN, BIN):
            m = s0 + BIN / 2
            c = int(round(c0 + t * (m - W / 2))) if horizontal else int(round(c0 - t * (m - H / 2)))
            lim = H if horizontal else W
            if c - WIN < 0 or c + WIN + 1 > lim:
                continue
            blk = dark[c - WIN:c + WIN + 1, s0:s0 + BIN] if horizontal else dark[s0:s0 + BIN, c - WIN:c + WIN + 1].T
            k = np.flatnonzero(blk.mean(axis=1) >= 0.6)
            if k.size:                                  # the dark band's centre (the frame is 3-4 px thick)
                run = max(np.split(k, np.flatnonzero(np.diff(k) > 1) + 1), key=len)
                pts.append((m, c - WIN + run.mean() + 0.5))   # +0.5: GCPs count from pixel corners
        if len(pts) < 10:
            raise CliError(E_FAIL, f"map frame not found ({len(pts)} points on one side)")
        p = np.array(pts, float)
        for _ in range(4):                              # drop points off the line (text/symbols touching the frame)
            a, b = np.polyfit(p[:, 0], p[:, 1], 1); res = p[:, 1] - (a * p[:, 0] + b)
            p = p[np.abs(res) <= max(1.5, 2.5 * np.std(res))]
            if len(p) < 10:
                raise CliError(E_FAIL, "map frame not found (too few points on a straight line)")
        a, b = np.polyfit(p[:, 0], p[:, 1], 1)
        if abs(a) > FIT_SLOPE_MAX:
            raise CliError(E_FAIL, f"map frame not found (a side is skewed {a:.4f})")
        return a, b

    top = side(True, t0, l0 + 50, r0 - 50); bot = side(True, b0, l0 + 50, r0 - 50)
    lef = side(False, l0, t0 + 50, b0 - 50); rig = side(False, r0, t0 + 50, b0 - 50)

    def cross(h, v):                                    # y = a x + b ; x = c y + d
        a, b = h; c, d = v
        y = (a * d + b) / (1 - a * c); return c * y + d, y
    return [tuple(float(v) for v in cross(h, v_)) for h, v_ in ((top, lef), (top, rig), (bot, lef), (bot, rig))]


def sheet_fit(ctx, tif, out, box, scale):
    """100k/50k scan -> COG on MGA (GDA2020, the sheet's own zone), cut to the map frame, ~2.5 m (50k) / 5 m (100k).
    box = (W, S, E, N) of the sheet in AGD66 degrees. Returns fit stats. GDAL/PROJ errors -> CliError (one sheet
    fails, the batch goes on)."""
    try:
        return _sheet_fit(ctx, tif, out, box, scale)
    except RuntimeError as e:
        raise CliError(E_FAIL, f"{tif.name}: {str(e).strip()[:200]}")


def _sheet_fit(ctx, tif, out, box, scale):
    from osgeo import gdal, osr
    import numpy as np
    gdal.UseExceptions(); osr.UseExceptions()
    ds = gdal_open(tif); W, H = ds.RasterXSize, ds.RasterYSize
    dpi = sheet_scan_dpi(ds)
    if ds.RasterCount >= 3:
        bands = [1, 2, 3]; expand = ["-b", "1", "-b", "2", "-b", "3"]             # (a 4th band = alpha: not ink)
    elif ds.GetRasterBand(1).GetColorTable() is not None:
        bands = None; expand = ["-expand", "rgb"]                                 # paletted scan
    else:
        bands = [1]; expand = ["-b", "1", "-b", "1", "-b", "1"]                   # greyscale scan
    if bands is None:                                   # read the palette scan through an RGB VRT
        ds = None; ds = gdal.Translate("", str(tif), format="VRT", rgbExpand="rgb"); bands = [1, 2, 3]
    w, s, e, n = box
    pxm = dpi / 0.0254 / scale                          # px per ground metre on the print
    latm = math.radians((s + n) / 2)
    pw = (e - w) * 111320 * math.cos(latm) * pxm; ph = (n - s) * 110574 * pxm
    dark = np.empty((H, W), bool)
    try:
        for r0 in range(0, H, 2048):                    # max(R,G,B) < FIT_DARK, in strips (a 9k x 11k RGB = 300 MB)
            a = ds.ReadAsArray(0, r0, W, min(2048, H - r0), band_list=bands)
            dark[r0:r0 + a.shape[-2]] = (a.max(axis=0) if a.ndim == 3 else a) < FIT_DARK; a = None
            if ABORT.is_set():
                raise Aborted()
    finally:
        ds = None
    try:
        px = sheet_frame(dark, pw, ph)
    except CliError as ex:
        raise CliError(ex.code, f"{tif.name}: {ex.msg}", ex.hint) from None
    dark = None
    zone = utm_zone((w + e) / 2)
    if not 48 <= zone <= 58:                           # AMG (AGD66) zones 48-58 = EPSG 20248..20258
        raise CliError(E_FAIL, f"{tif.name}: outside the AMG zones")
    agd = osr.SpatialReference(); agd.ImportFromEPSG(4202)
    amg = osr.SpatialReference(); amg.ImportFromEPSG(20200 + zone)
    g94 = osr.SpatialReference(); g94.ImportFromEPSG(4283)
    mga = osr.SpatialReference(); mga.ImportFromEPSG(7846 + zone - 46)   # GDA2020 / MGA zone 46..59 = EPSG 7846..7859
    for s_ in (agd, amg, g94, mga):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    # the paper sheet is drawn on AMG: scan px -> AMG is affine (paper + scanner); the frame corners check it.
    # AMG -> MGA2020 (projection + datum, smooth but not affine) goes in through a 10x10 grid of points.
    ta = osr.CoordinateTransformation(agd, amg); ti = osr.CoordinateTransformation(amg, agd)
    o1 = osr.CoordinateTransformationOptions(); o1.SetOperation("urn:ogc:def:coordinateOperation:EPSG::1278")
    t1 = osr.CoordinateTransformation(agd, g94, o1); t2 = osr.CoordinateTransformation(g94, mga)
    A = np.array([[c, r, 1] for c, r in px])
    X = np.array([ta.TransformPoint(lo, la)[:2] for lo, la in ((w, n), (e, n), (w, s), (e, s))])
    coef, *_ = np.linalg.lstsq(A, X, rcond=None); rr = X - A @ coef
    worst = float(np.hypot(rr[:, 0], rr[:, 1]).max())   # how far the paper is from a plain affine (uneven shrink)
    if worst > FIT_MAX_RES_M:
        raise CliError(E_FAIL, f"{tif.name}: frame corners don't fit the sheet ({worst:.0f} m off): not fitted")
    # scan px -> AMG: the projective map through the 4 corners exactly (takes up uneven paper shrink; the affine
    # misfit above stays the quality figure)
    P = np.array(px, float); pm, ps = P.mean(0), P.std(); xm, xs = X.mean(0), X.std()   # normalised (else SVD flips)
    Pn = (P - pm) / ps; Xn = (X - xm) / xs
    M_ = []
    for (c, r), (x, y) in zip(Pn, Xn):
        M_.append([c, r, 1, 0, 0, 0, -c * x, -r * x, -x]); M_.append([0, 0, 0, c, r, 1, -c * y, -r * y, -y])
    hom = np.linalg.svd(np.array(M_, float))[2][-1].reshape(3, 3); hom /= hom[2, 2]
    for (c, r), (x, y) in zip(Pn, Xn):                  # exact through the 4 corners, or refuse
        u = hom @ np.array([c, r, 1.0])
        if abs(u[0] / u[2] - x) * xs > 0.01 or abs(u[1] / u[2] - y) * xs > 0.01:
            raise CliError(E_FAIL, f"{tif.name}: corner fit failed (degenerate frame)")

    def to_mga(c, r):
        u = hom @ np.array([(c - pm[0]) / ps, (r - pm[1]) / ps, 1.0])
        ex, ny = u[0] / u[2] * xs + xm[0], u[1] / u[2] * xs + xm[1]
        lo, la = ti.TransformPoint(float(ex), float(ny))[:2]
        lo2, la2 = t1.TransformPoint(lo, la)[:2]; return t2.TransformPoint(lo2, la2)[:2]
    cs = np.array([p[0] for p in px]); rs = np.array([p[1] for p in px])
    gcps = [(float(c), float(r), *to_mga(c, r)) for c in np.linspace(cs.min(), cs.max(), 10)
            for r in np.linspace(rs.min(), rs.max(), 10)]
    corners = [to_mga(c, r) for c, r in px]
    res = 2.5 if scale <= 50000 else 5.0
    tag = f"{out.stem}.{os.getpid()}"; vrt = ctx.scratch() / f"{tag}.vrt"; cut = ctx.scratch() / f"{tag}.cut.csv"
    part = ctx.scratch() / f"{tag}.part.tif"
    try:
        g = ["gdal_translate", "-q", "-a_srs", mga.ExportToWkt()]
        for c, r, x, y in gcps:
            g += ["-gcp", f"{c:.3f}", f"{r:.3f}", f"{x:.3f}", f"{y:.3f}"]
        _run(g + expand + ["-of", "VRT", tif, vrt], ctx)
        ring = [corners[i] for i in (0, 1, 3, 2, 0)]
        cut.write_text('WKT\n"POLYGON((' + ",".join(f"{x:.3f} {y:.3f}" for x, y in ring) + '))"\n')
        _run(["gdalwarp", "-q", "-overwrite", "-order", "2", "-t_srs", mga.ExportToWkt(), "-tr", res, res, "-tap",
              "-r", "bilinear", "-cutline", cut, "-cutline_srs", mga.ExportToWkt(), "-crop_to_cutline", "-dstalpha",
              "-wo", "NUM_THREADS=ALL_CPUS", "-of", "COG", *photo_co(ctx),
              "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "BIGTIFF=IF_SAFER", vrt, part], ctx)
        if not gdal_ok(part, deep=True):
            raise CliError(E_FAIL, f"{out.name}: conversion failed its check", "re-run")
        install_file(part, out)
    finally:
        for p_ in (vrt, cut, part):
            p_.unlink(missing_ok=True)
    return {"corner_fit_m": round(worst, 1), "res_m": res, "epsg": int(mga.GetAuthorityCode(None)),
            "scan_m_per_px": round(float(np.hypot(*coef[0])), 2)}


def sheet_box(ctx, scale, number):
    """(W, S, E, N) degrees of a sheet from the index (snapped to the whole minute: the index has float noise)."""
    from osgeo import ogr
    ds = ro_ogr(sheets_dir(ctx) / "index.gpkg")
    if ds is None:
        raise CliError(E_NOTFOUND, "no sheet index", "lmap sheets index --yes")
    L = ds.GetLayerByName(f"s{scale}")
    if L is None:
        raise CliError(E_NOTFOUND, f"sheet index has no 1:{scale} layer", "lmap sheets index --yes")
    L.SetAttributeFilter("number = '%s'" % str(number).replace("'", ""))
    f = L.GetNextFeature(); e = f.GetGeometryRef().GetEnvelope() if f else None
    f = L = ds = None
    if e is None:
        return None
    m = lambda v: round(v * 60) / 60
    return (m(e[0]), m(e[2]), m(e[1]), m(e[3]))


def sheet_layer_path(w):
    return str(Path(w["file"]).with_suffix(".cog.tif"))


SHEETS_OLD_WARN = ("HISTORICAL ONLY - DO NOT NAVIGATE OR TAKE GRID REFERENCES FROM THE GA 1:100k/1:50k SHEETS. They are "
                   "1961-2008 prints: roads/buildings decades out of date, the scan fitted to MGA is ~20 m off (measured "
                   "Townsville 8259-2: 18 m E, 5 m S), and the grid PRINTED on them is AGD66, ~200 m off MGA/GPS. "
                   "Use `lmap map` (built from current data) for anything that matters.")


def sheet_paths(ctx, want):
    """Set file (print PDF/scan) / layer (.cog.tif) / have on each sheet dict, in the shared data/australia/sheets cache."""
    D = sheets_dir(ctx)
    for w in want:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{w['number']} {w['name']}").strip("_")
        w["file"] = str(D / w["scale"] / (safe + (".pdf" if w["scale"] == "250k" else ".tif")))
        w["layer"] = sheet_layer_path(w)               # 250k: from the GeoPDF; 100k/50k: the scan fitted to its frame
        w["have"] = Path(w["file"]).is_file() and Path(w["layer"]).is_file()
    return want


def sheets_fetch(ctx, todo, got, missing, failed):
    """Download + make the map layer of each sheet in todo (hold out-sheets.lock). Fills got/missing/failed.
    Returns None, or 'aborted'/'drained' if stopped (finished sheets are kept)."""
    for w in todo:
        if DRAIN.is_set() or ABORT.is_set():
            return "aborted" if ABORT.is_set() else "drained"
        f = Path(w["file"]); f.parent.mkdir(parents=True, exist_ok=True)
        if Path(w["layer"]).is_file() and f.is_file():  # another run made it while we waited for the lock
            got.append(w["number"]); w["have"] = True; continue
        ctx.log(f"  {w['scale']} {w['number']} {w['name']} ({w['year']})")
        if not f.is_file():                            # (already have the PDF: only the map layer is missing; offline ok)
            url = sheet_url(ctx, w["ecat"])
            if not url:
                missing.append(w["number"]); ctx.log(f"  {w['scale']} {w['number']} {w['name']}: no download link"); continue
            tmp = ctx.scratch() / f"sheet-{f.name}"
            try:
                r = http_get_parallel(ctx, url, tmp, cap=4, check=lambda p: gdal_ok(p))
            except Aborted:
                return "aborted"
            except CliError as e:
                failed.append({"number": w["number"], "error": e.msg}); continue
            if r is None:
                missing.append(w["number"]); continue
            if r is False:
                return "aborted" if ABORT.is_set() else "drained"
            try:
                install_file(tmp, f)
            finally:
                tmp.unlink(missing_ok=True)
        if not Path(w["layer"]).is_file():
            try:
                if w["scale"] == "250k":
                    sheet_layer(ctx, f, Path(w["layer"]))
                else:
                    box = sheet_box(ctx, w["scale"], w["number"])
                    if box is None:
                        raise CliError(E_FAIL, "sheet not in the index")
                    w["fit"] = sheet_fit(ctx, f, Path(w["layer"]), box, 50000 if w["scale"] == "50k" else 100000)
            except Aborted:
                return "aborted"
            except CliError as e:                      # print file kept; the next run retries the layer
                failed.append({"number": w["number"], "error": e.msg}); continue
        got.append(w["number"]); w["have"] = True
    return None


def sheets_in_box(ctx, scale, bbox):
    """Sheets of one series overlapping the box (w,s,e,n degrees; edge-touching ones left out)."""
    from osgeo import ogr
    ds = ro_ogr(sheets_dir(ctx) / "index.gpkg"); L = ds.GetLayerByName(f"s{scale}")
    w, s, e, n = bbox
    bx = ogr.CreateGeometryFromWkt(f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))")
    L.SetSpatialFilterRect(w, s, e, n); out = []
    for f in L:
        g = f.GetGeometryRef()
        if g is None or not g.Intersects(bx) or g.Intersection(bx).GetArea() < 1e-7:
            continue
        out.append({"scale": scale, "number": f["number"], "name": (f["name"] or "").strip(), "year": f["year"],
                    "ecat": f["ecat"], "available": bool(f["available"]), "dist_km": 0})
    L = ds = None
    return sorted(out, key=lambda x: x["number"])


SHEET_RES = {"250k": 25, "100k": 5, "50k": 2.5}       # m/px of the sheet layers (sheet_layer / sheet_fit)
AREA_SHEETS_MAX = 12                                   # sheets of one scale per area (more: `lmap sheets get`)


def area_sheet(ctx, d, out, sc, bbox, crs):
    """An area's GA sheet layer: the sheets of one scale over the box, fetched into the shared data/australia/sheets
    cache (once for every area), then cut to the box on the area's MGA zone -> d/sheet-<scale>.tif."""
    if not (sheets_dir(ctx) / "index.gpkg").is_file():     # built once by cmd_area_create before the Dag
        return {"skipped": "no sheet index (no internet?); re-run online (or `lmap sheets index --yes`)"}
    if sc == "250k":
        from osgeo import gdal
        if gdal.GetDriverByName("PDF") is None:
            return {"skipped": "GDAL has no PDF driver (needed for the 250k GeoPDFs)"}
    want = sheets_in_box(ctx, sc, bbox)
    if not want:
        return {"skipped": f"no GA 1:{sc[:-1]} 000 sheet here"}
    if len(want) > AREA_SHEETS_MAX:
        return {"skipped": f"{len(want)} 1:{sc[:-1]}k sheets: too many for an area (max {AREA_SHEETS_MAX}); "
                           f"use `lmap sheets get`"}
    sheet_paths(ctx, want)
    todo = [w for w in want if w["available"] and not w["have"]]
    if any(not Path(w["file"]).is_file() for w in todo) and not net_ok("d28rz98at9flks.cloudfront.net"):
        return {"skipped": f"no internet: {len(todo)} 1:{sc[:-1]}k sheets to download; re-run online"}
    got, missing, failed = [], [], []
    if todo:
        with Lock(STATE / "locks" / "out-sheets.lock", wait=ctx):
            if sheets_fetch(ctx, todo, got, missing, failed):
                raise Aborted()
    if failed:
        raise CliError(E_UPSTREAM, f"sheets {', '.join(x['number'] for x in failed)}: {failed[0]['error']}",
                       "re-run (sheets already fetched are kept)")
    use = [w for w in want if Path(w["layer"]).is_file()]
    if not use:
        return {"skipped": f"GA has no download for the 1:{sc[:-1]}k sheets here ({', '.join(missing)})"}
    r = SHEET_RES[sc]
    from osgeo import gdal
    _ds = gdal_open(use[0]["layer"]); nb = _ds.RasterCount; _ds = None   # fitted scans are RGBA already: no 5th band
    part = ctx.scratch() / f"area-{d.name}-{out.stem}.{os.getpid()}.part.tif"
    try:
        with BuildSlot(ctx, f"{d.name}/{out.name}"):
            run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", *bbox, "-t_srs", crs, "-tr", r, r,
                 "-r", "bilinear", *(["-dstalpha"] if nb == 3 else []), "-of", "COG", "-co", "BIGTIFF=IF_SAFER", *photo_co(ctx, 90),
                 "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "BLOCKSIZE=512", "-wo", "NUM_THREADS=ALL_CPUS",
                 *[w["layer"] for w in use], part], ctx)
        if not gdal_ok(part):
            raise CliError(E_FAIL, f"{out.name}: gdalwarp output failed gdalinfo")
        install_file(part, out)
    finally:
        for x in part.parent.glob(part.name + "*"):
            x.unlink(missing_ok=True)
    return {"file": out.name, "from": f"ga-sheets-{sc}", "res_m": r,
            "sheets": [f"{w['number']} {w['name']} ({w['year']})" for w in use], **({"missing": missing} if missing else {})}


def cmd_sheets_get(ctx):
    """Download the GA sheets around a point: 250k (PDF + map layer), 100k / 50k (print scan + layer fitted to MGA)."""
    need_root(ctx); a = ctx.a; need_tool("gdalwarp")
    from osgeo import gdal
    if "250k" in a.scale and gdal.GetDriverByName("PDF") is None:
        raise CliError(E_FAIL, "GDAL has no PDF driver (needed for the 250k GeoPDFs)", "apt install gdal-bin (with poppler)")
    scales = [x.strip().lower() for x in a.scale.split(",") if x.strip()]
    bad = [x for x in scales if x not in SHEET_SERIES]
    if bad or not scales:
        raise CliError(E_USAGE, f"--scale {a.scale}: use {','.join(SHEET_SERIES)} (comma list)")
    if bool(a.at) == bool(a.sheet):
        raise CliError(E_USAGE, "need --at POS or --sheet NUMBER (one of them)",
                       "lmap sheets get --at '55K DU 79234 70557' --radius-km 50 --dry-run")
    if not 0 <= a.radius_km <= 300:
        raise CliError(E_USAGE, f"--radius-km {a.radius_km}: 0-300")
    ix = sheets_dir(ctx) / "index.gpkg"
    if not ix.is_file():
        raise CliError(E_NOTFOUND, "no sheet index yet", "lmap sheets index --yes")
    if a.at:
        lat, lon = parse_pos(a.at)
        want = [x for sc in scales for x in sheets_find(ctx, sc, lat, lon, a.radius_km)]
    else:
        from osgeo import ogr
        want = []; ds = ro_ogr(ix)
        for sc in scales:
            L = ds.GetLayerByName(f"s{sc}")
            if L is None:
                raise CliError(E_NOTFOUND, f"sheet index has no 1:{sc} layer", "lmap sheets index --yes")
            L.SetAttributeFilter("number = '%s'" % a.sheet.replace("'", ""))
            want += [{"scale": sc, "number": f["number"], "name": (f["name"] or "").strip(), "year": f["year"], "ecat": f["ecat"],
                      "available": bool(f["available"]), "dist_km": 0} for f in L]
            L = None
        ds = None
    if not want:
        raise CliError(E_NOTFOUND, "no sheets there", "a point inside Australia, or another --scale")
    if len(want) > 60:
        raise CliError(E_USAGE, f"{len(want)} sheets: max 60 per run", "smaller --radius-km or fewer scales")
    sheet_paths(ctx, want)
    todo = [w for w in want if w["available"] and not w["have"]]
    plan = {"action": "sheets.get", "sheets": want, "to_get": len(todo),
            "est_mb": sum(SHEET_SERIES[w["scale"]]["est_mb"] for w in todo if not Path(w["file"]).is_file()),
            "note": "250k: AUSTopo 2025, overview only (~50-100 m)" + ("; 100k/50k: HISTORICAL, see WARNING"
                    if any(w["scale"] != "250k" for w in want) else ""),
            **({"WARNING": SHEETS_OLD_WARN} if any(w["scale"] != "250k" for w in want) else {})}
    if "WARNING" in plan:
        ctx.log("!" * 78 + "\n!! " + SHEETS_OLD_WARN + "\n" + "!" * 78)
    if a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not todo:
        return {"ok": True, **plan, "unchanged": True}
    if not a.yes:
        raise CliError(E_REFUSED, f"refusing to download {len(todo)} sheets (~{plan['est_mb']} MB) without --yes",
                       "preview: same command with --dry-run")
    install_signals(); t0 = time.time(); got, missing, failed = [], [], []
    with Lock(STATE / "locks" / "out-sheets.lock", wait=ctx):
        why = sheets_fetch(ctx, todo, got, missing, failed)
    if why:
        return {"ok": False, **plan, "got": got, "missing": missing, "failed": failed, "stopped": why,
                "hint": "re-run the same command to get the rest (finished sheets are kept)",
                "seconds": round(time.time() - t0, 1)}
    res = {"ok": not (missing or failed), **plan, "got": got, "seconds": round(time.time() - t0, 1),
           "next": "lmap au project --yes  (adds 'Map sheets (GA)')"}
    if missing or failed:
        res.update(missing=missing, failed=failed, exit_code=E_UPSTREAM,
                   hint="missing = GA has no download for it; failed = re-run to retry; the rest are saved")
    return res



def _m_between(a, b):
    la = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111320, (a[1] - b[1]) * 111320 * math.cos(la))


def cmd_terrain_viewshed(ctx):
    """What ground can be seen from a point (or a set of OPs): a layer in australia.qgz."""
    need_root(ctx); a = ctx.a
    need_tool("gdal_viewshed", "gdalwarp")
    k = a.refraction if a.refraction is not None else REFRACTION["light"]
    if not -1 <= k <= 1:
        raise CliError(E_USAGE, f"--refraction {k}: between -1 and 1", "0.143 light (default), 0.25 radio, 0 none")
    for nm, v in (("--height", a.height), ("--target-height", a.target_height)):
        if not 0 <= v <= 500:
            raise CliError(E_USAGE, f"{nm} {v}: metres above ground, 0-500")
    if not 0.1 <= a.radius_km <= VS_MAX_KM:
        raise CliError(E_USAGE, f"--radius-km {a.radius_km}: 0.1-{VS_MAX_KM}")
    if a.angles and a.points:
        raise CliError(E_USAGE, "--angles is for one observer (--from), not --points",
                       "run one viewshed per OP with --angles")
    if bool(a.points) == bool(a.from_):
        raise CliError(E_USAGE, "need --from POS or --points FILE (one of them)",
                       "lmap terrain viewshed --from '55K DU 79234 70557' --name castle-hill --yes")
    if a.points:
        if not Path(a.points).is_file():
            raise CliError(E_NOTFOUND, f"points file {a.points} not found")
        pts = read_points(a.points)
        if not pts:
            raise CliError(E_USAGE, f"{a.points}: no points")
        if len(pts) > VS_MAX_PTS:
            raise CliError(E_USAGE, f"{len(pts)} points; max {VS_MAX_PTS}")
    else:
        pts = [{"name": a.name or "A", "pos": parse_pos(a.from_), "height": None}]
    name = a.name or (Path(a.points).stem if a.points else to_mgrs(*pts[0]["pos"], digits=3).replace(" ", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name) or name.endswith("-angle"):
        raise CliError(E_USAGE, f"bad --name {name!r}", "letters, digits, - _ . (max 40), not ending in -angle")
    hh = lambda p: p["height"] if p["height"] is not None else a.height
    rad = a.radius_km * 1000
    out = ctx.path("data/australia") / "viewsheds" / f"{name}.cog.tif"
    aout = out.with_name(f"{name}-angle.cog.tif") if a.angles else None
    lons = [p["pos"][1] for p in pts]
    if max(lons) - min(lons) > 6:
        raise CliError(E_USAGE, f"observers span {max(lons) - min(lons):.1f} deg of longitude; max 6 (one UTM zone)")
    dem, ground = vs_pick_dem(ctx, pts, rad)
    lat0 = sum(p["pos"][0] for p in pts) / len(pts); lon0 = sum(p["pos"][1] for p in pts) / len(pts)
    zone = utm_zone(lon0); south = lat0 < 0; epsg = (32700 if south else 32600) + zone
    res = max(2.0, float(round(ground)))
    tr = _utm_tr(zone, south)
    xy = [tr.TransformPoint(p["pos"][1], p["pos"][0])[:2] for p in pts]
    x0 = math.floor((min(x for x, _ in xy) - rad) / res) * res; x1 = math.ceil((max(x for x, _ in xy) + rad) / res) * res
    y0 = math.floor((min(y for _, y in xy) - rad) / res) * res; y1 = math.ceil((max(y for _, y in xy) + rad) / res) * res
    W, H = int((x1 - x0) / res), int((y1 - y0) / res)
    base = {"name": name, "out": str(out), "dem": dem.path.name, "res_m": res, "utm_epsg": epsg, "size_px": [W, H],
            "radius_km": a.radius_km, "refraction_k": k, "target_height_m": a.target_height,
            "observers": [{"name": p["name"], "mgrs": to_mgrs(*p["pos"]), "height_m": hh(p)} for p in pts],
            "note": TERRAIN_NOTE}
    if aout:
        base["angle_out"] = str(aout)
    if W * H > 150_000_000:
        raise CliError(E_USAGE, f"window {W}x{H} px at {res:g} m is too big", "smaller --radius-km or fewer/closer points")
    if a.dry_run:
        return dict(ok=True, dry_run=True, action="terrain.viewshed", exists=out.is_file(), **base)
    if out.exists() and not a.yes:
        raise CliError(E_REFUSED, f"refusing to overwrite {out.name} without --yes", "pick another --name, or --yes")
    if not a.yes:
        raise CliError(E_REFUSED, "refusing to write the viewshed without --yes",
                       "preview: same command with --dry-run")
    install_signals()
    from osgeo import gdal
    import numpy as np
    gdal.UseExceptions()
    t0 = time.time(); sc = ctx.scratch(); tag = f"vs-{name}.{os.getpid()}"
    win, one, cnt, part, ang, apart = (sc / f"{tag}.{x}.tif" for x in ("dem", "one", "cnt", "part", "ang", "apart"))
    try:
        _run(["gdalwarp", "-q", "-overwrite", "-t_srs", f"EPSG:{epsg}", "-te", x0, y0, x1, y1, "-tr", res, res,
              "-r", "bilinear", "-ot", "Float32", *(["-srcnodata", dem.nd] if dem.nd is not None else []),
              "-dstnodata", "-32768", "-co", "TILED=YES", "-co", "COMPRESS=ZSTD",
              "-co", "BIGTIFF=IF_SAFER", "-multi", "-wo", "NUM_THREADS=ALL_CPUS", dem.path, win], ctx)
        wds = gdal_open(win); wprj = wds.GetProjection(); wb = wds.GetRasterBand(1)
        valid = np.zeros((H, W), bool)
        for r0 in range(0, H, 2048):                       # by blocks: a float copy of the window is 4 B/px
            z_ = wb.ReadAsArray(0, r0, W, min(2048, H - r0)); valid[r0:r0 + z_.shape[0]] = np.isfinite(z_) & (z_ != -32768)
        wds = wb = None; del z_
        if valid.mean() < 0.99:
            ctx.log(f"note: {100 * (1 - valid.mean()):.1f}% of the window has no terrain (sea / data edge): not seen")
        total = np.zeros((H, W), "uint8"); per = []
        for p, (x, y) in zip(pts, xy):
            if ABORT.is_set():
                raise Aborted()
            _run(["gdal_viewshed", "-q", "-ox", x, "-oy", y, "-oz", hh(p), "-tz", a.target_height, "-md", rad,
                  "-cc", 1 - k, "-vv", "1", "-iv", "0", "-ov", "0", "-of", "GTiff", "-co", "COMPRESS=ZSTD",
                  "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER", win, one], ctx)
            ods = gdal_open(one); og = ods.GetGeoTransform()       # cropped to this observer's circle
            v = ods.ReadAsArray() == 1; ods = None
            fx, fy = (og[0] - x0) / res, (y1 - og[3]) / res
            if abs(og[1] - res) > 1e-6 or abs(fx - round(fx)) > 1e-3 or abs(fy - round(fy)) > 1e-3:
                raise CliError(E_FAIL, f"gdal_viewshed output grid {og} doesn't line up with the window")
            ox, oy = int(round(fx)), int(round(fy))
            sx, sy = max(0, -ox), max(0, -oy); tx, ty = max(0, ox), max(0, oy)
            h_, w_ = min(v.shape[0] - sy, H - ty), min(v.shape[1] - sx, W - tx)
            if h_ > 0 and w_ > 0:
                vv = v[sy:sy + h_, sx:sx + w_] & valid[ty:ty + h_, tx:tx + w_]
                total[ty:ty + h_, tx:tx + w_] += vv
                per.append(round(float(vv.sum()) * res * res / 1e6, 2))
            else:
                per.append(0.0)
            del v
            one.unlink(missing_ok=True)
        del valid
        astats = vs_angles(ctx, win, total, xy[0], hh(pts[0]), a.target_height, k, (x0, y1, res), wprj, ang, apart) \
            if aout else None
        o = gdal.GetDriverByName("GTiff").Create(str(cnt), W, H, 1, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=ZSTD"])
        o.SetGeoTransform((x0, res, 0, y1, 0, -res)); o.SetProjection(wprj)
        b = o.GetRasterBand(1); b.SetNoDataValue(0); b.WriteArray(total)
        o.SetMetadataItem("LMAP_VIEWSHED", json.dumps({k_: base[k_] for k_ in ("observers", "radius_km",
                          "target_height_m", "refraction_k", "res_m")}))
        o = None
        _run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
              "-co", "OVERVIEW_RESAMPLING=NEAREST", "-co", "BIGTIFF=IF_SAFER", cnt, part], ctx)
        out.with_name(f"{name}-angle.cog.tif").unlink(missing_ok=True)   # old angles never sit beside a new count
        if aout:
            install_file(apart, aout)
        install_file(part, out)
    finally:
        for f_ in (win, one, cnt, part, ang, apart):
            f_.unlink(missing_ok=True)
    km2 = lambda n: round(float(n) * res * res / 1e6, 2)
    bc = np.bincount(total.ravel(), minlength=len(pts) + 1)
    resd = dict(ok=True, action="terrain.viewshed", **base, visible_km2=km2(bc[1:].sum()),
                seconds=round(time.time() - t0, 1))
    for o_, s_ in zip(resd["observers"], per):
        o_["visible_km2"] = s_
    if aout:
        resd["angle_mils"] = astats
    if len(pts) > 1:
        resd["seen_by"] = {str(i): km2(bc[i]) for i in range(1, len(bc)) if bc[i]}
    if not a.no_project:
        resd["next"] = "lmap au project --yes  (adds 'Seen from %s' under Terrain analysis, off)" % name
    return resd
