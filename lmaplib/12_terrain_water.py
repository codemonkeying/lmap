# lmap: terrain_water. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: water (GRASS r.watershed + r.stream.extract)
# r.watershed (GRASS 8.4): MFD flow accumulation (cells draining through each cell, -a = positive even where
# underestimated at the edge), tci = ln(a / tan(b)) topographic wetness index, basin = catchments over `threshold`
# cells. No sink-filling needed (least-cost search). r.stream.extract: streams start where accumulation >= threshold.
# Thresholds are given as upstream AREA (km2) so they mean the same at 2 m and 30 m.
# Works offline anywhere: the finest DEM with data round the point (2 m LiDAR, 10 m LiDAR, else 30 m), grid
# coarsened to stay under WF_MAX_PX.
WF_MAX_PX = int(os.environ.get("LMAP_WF_MAX_PX", 150_000_000))
WF_B_PER_PX = 40                        # Python peak bytes per cell in wf_finish (float32 grids + labels + masks)
WF_SEA_M, WF_SEA_LOW = 0.5, -3.0        # sea = ground in [-3, 0.5] m (or nodata) joined to the box edge or to nodata;
                                        # inland ground below sea level (Lake Eyre -15 m) and low pockets stay land


def wf_mem_mb():
    try:
        return int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1]) // 1024
    except (OSError, TypeError):
        return 4000


def wf_max_px():
    return int(min(WF_MAX_PX, wf_mem_mb() * 1e6 / WF_B_PER_PX))


def wf_pad(rad, res):
    """Computed box = asked box + this on every side (flow from outside comes in; edge pile-ups fall outside)."""
    return max(10 * res, min(0.25 * rad, 2000))


def wf_side(rad, res):
    return 2 * (rad + wf_pad(rad, res)) + 2 * res        # + a cell each side for the floor/ceil snap


def wf_pick(ctx, pos, rad_m, res_req):
    """Finest DEM with data at and round the point AND whose extent covers the whole padded box (the 30 m
    all-Australia DEM is the fallback). Grid: the DEM's own (min 2 m), coarsened to stay under wf_max_px()."""
    import numpy as np
    lat, lon = pos; cl = math.cos(math.radians(lat))
    lats, lons = [lat], [lon]
    for r_, n_ in ((rad_m * 0.95, 16), (rad_m / 2, 8)):
        for i in range(n_):
            t = 2 * math.pi * i / n_
            lats.append(lat + r_ * math.cos(t) / 111320); lons.append(lon + r_ * math.sin(t) / (111320 * cl))
    zone = utm_zone(lon); south = lat < 0
    cx, cy = _utm_tr(zone, south).TransformPoint(lon, lat)[:2]; inv = _utm_tr(zone, south, inverse=True)
    dems = terrain_dems(ctx)
    for i, d in enumerate(dems):
        g = d.res_m * (cl if d.srs.IsProjected() and (d.srs.GetAttrValue("PROJECTION") or "").startswith("Mercator") else 1)
        v = d.sample(lons, lats)
        if np.isnan(v[0]) or np.mean(~np.isnan(v)) < 0.75:            # coast: sea probes can be nodata
            continue
        if i < len(dems) - 1:                                          # a patch must cover the padded box
            h = rad_m + wf_pad(rad_m, max(2.0, float(round(g))))
            ring = [inv.TransformPoint(cx + sx * h, cy + sy * h)[:2] for sx in (-1, 0, 1) for sy in (-1, 0, 1) if sx or sy]
            p = np.array(d.tr.TransformPoints(ring))[:, :2]
            xs = sorted((d.gt[0], d.gt[0] + d.w * d.gt[1])); ys = sorted((d.gt[3], d.gt[3] + d.h * d.gt[5]))
            if not ((p[:, 0] >= xs[0]) & (p[:, 0] <= xs[1]) & (p[:, 1] >= ys[0]) & (p[:, 1] <= ys[1])).all():
                continue
        break
    else:
        raise CliError(E_NOTFOUND, "no DEM has data at and around that point", "a point on land in Australia")
    if res_req:
        if res_req < g / 2:
            raise CliError(E_USAGE, f"--res {res_req:g} is finer than half the terrain's {g:.0f} m grid: more cells, "
                           "no more detail", f"--res {max(2, math.ceil(g / 2))} or more, or leave it out")
        res = float(res_req)
    else:
        res = max(2.0, float(round(g)))
    mx = wf_max_px()
    if not res_req:
        while (wf_side(rad_m, res) / res) ** 2 > mx:
            res = max(res + 1, float(math.ceil(wf_side(rad_m, res) / math.sqrt(mx))))
    if (wf_side(rad_m, res) / res) ** 2 > mx:
        side = wf_side(rad_m, res)
        raise CliError(E_USAGE, f"{side / 1000:.1f} km box (with pad) at {res:g} m is {(side / res) ** 2 / 1e6:.0f}M cells, "
                       f"limit {mx / 1e6:.0f}M (memory)", "coarser --res or smaller --radius-km")
    return d, g, res


def wf_mask_sea(demf):
    """Set sea to nodata in the warped DEM, in place. Returns the land fraction."""
    from osgeo import gdal
    import numpy as np
    from scipy import ndimage
    ds = gdal_open(demf, update=True); b = ds.GetRasterBand(1); z = b.ReadAsArray()
    nd = ~np.isfinite(z) | (z == -32768)
    lab, n = ndimage.label(nd | ((z <= WF_SEA_M) & (z >= WF_SEA_LOW)))
    seeds = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1], lab[nd]]))
    m = np.zeros(n + 1, bool); m[seeds] = True; m[0] = False
    sea = m[lab]; lab = None
    z[sea] = -32768; b.WriteArray(z); b.FlushCache(); ds = None
    return float(1 - sea.mean())


# Breaching: bare-earth terrain has road/rail embankments but not the culverts and bridges under them, so flow
# piles up along the bank. Every OSM waterway (river/stream/canal + drains/ditches) that crosses a road or railway,
# and every waterway OSM tags as a culvert, gets a notch: the cells where the waterway is under the embankment
# are lowered to the lower of the ground just either side (only ever lowered). Railways, drains and culvert tags
# are not in osm-australia.gpkg: au-breach.gpkg is cut once from the raw .osm.pbf (~100 s, ~35 MB).
WF_BR_HALF = 8.0           # m: embankment half-width either side of the road/rail centreline
WF_BR_MAX = 120.0          # m: a waterway longer than this inside the bank runs ALONG it (table drain): not cut
WF_BR_NOROAD = ("footway", "path", "cycleway", "steps", "pedestrian", "bridleway", "corridor", "proposed",
                "construction", "platform", "elevator", "via_ferrata")
WF_BR_SQL = ("SELECT waterway, railway, (coalesce(other_tags,'') LIKE '%\"tunnel\"=>\"culvert\"%') AS culvert, geometry "
             "FROM lines WHERE railway IN ('rail','narrow_gauge','light_rail','preserved') "
             "OR waterway IN ('drain','ditch') OR (waterway IN ('river','stream','canal') "
             "AND other_tags LIKE '%\"tunnel\"=>\"culvert\"%')")


def wf_breach_src(ctx):
    """data/australia/osm/au-breach.gpkg (railways, drains/ditches, culvert waterways), (re)built from the .osm.pbf
    when missing or older. None if neither exists."""
    pbf = ctx.path(ctx.sources["osm-australia"]["from"]) if "osm-australia" in ctx.sources \
        else ctx.path("data/australia/osm/australia-latest.osm.pbf")
    out = ctx.path("data/australia/osm/au-breach.gpkg"); tag = out.with_name(out.name + ".sql")   # what it was cut with
    sig = hashlib.sha1(WF_BR_SQL.encode()).hexdigest()[:12]
    stale = lambda: pbf.is_file() and (not out.is_file() or out.stat().st_mtime < pbf.stat().st_mtime
                                       or not tag.is_file() or tag.read_text() != sig)
    if stale():
        with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx):
            if stale():
                ctx.log("building au-breach.gpkg (OSM railways, drains, culverts; all AU, ~2 min)")
                part = ctx.scratch() / f"au-breach.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
                try:
                    _run(["ogr2ogr", "-f", "GPKG", part, pbf, "-nln", "x", "-nlt", "LINESTRING", "-dialect", "SQLITE",
                          "-sql", WF_BR_SQL], ctx, timeout=3600,     # OSM driver temp (~1 GB) -> scratch, not cwd
                         env={**os.environ, "CPL_TMPDIR": str(ctx.scratch())})
                    from osgeo import ogr
                    d = ro_ogr(part); n = d.GetLayer(0).GetFeatureCount(); d = None
                    if n < 1000:                       # a short/empty read must not replace a good file
                        raise CliError(E_FAIL, f"au-breach came out short ({n} lines); not installed", "re-run")
                    install_file(part, out); tag.write_text(sig)
                finally:
                    part.unlink(missing_ok=True)
    return out if out.is_file() else None


def wf_breach(ctx, demf, epsg):
    """Notch the warped UTM DEM in place where waterways pass under roads/railways. Returns counts."""
    from osgeo import gdal, ogr, osr
    import numpy as np
    from scipy import ndimage
    osm = ctx.path(ctx.sources["osm-australia"]["dest"]) if "osm-australia" in ctx.sources \
        else ctx.path("data/australia/osm/osm-australia.gpkg")
    try:
        brs = wf_breach_src(ctx); why = None
    except CliError as e:                               # a failed build must not lose the water run: roads only
        brs = None; why = f"au-breach.gpkg build failed ({e}): roads only"; ctx.log(why)
    D = gdal_open(demf, update=True); gt = D.GetGeoTransform(); W, H = D.RasterXSize, D.RasterYSize
    res = gt[1]; x0, y1 = gt[0], gt[3]; x1, y0 = x0 + W * res, y1 - H * res
    utm = osr.SpatialReference(); utm.ImportFromEPSG(epsg); utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    w4 = osr.SpatialReference(); w4.ImportFromEPSG(4326); w4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    inv = osr.CoordinateTransformation(utm, w4)
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    cs = np.array(inv.TransformPoints([(x0, y0), (x1, y0), (x0, y1), (x1, y1), (xm, y0), (xm, y1), (x0, ym), (x1, ym)]))
    bb = (cs[:, 0].min() - .005, cs[:, 1].min() - .005, cs[:, 0].max() + .005, cs[:, 1].max() + .005)
    mem = ogr.GetDriverByName("Memory").CreateDataSource("b")
    Lw = mem.CreateLayer("water", utm, ogr.wkbLineString); Lb = mem.CreateLayer("bank", utm, ogr.wkbLineString)
    Lw.CreateField(ogr.FieldDefn("culvert", ogr.OFTInteger))
    used = {"roads": 0, "railways": 0, "waterways": 0, "culverts": 0}

    def take(src, layer, where, into, key, cul=None):
        if src is None or not src.is_file():
            return
        ds = ro_ogr(src); L = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
        if L is None:
            return
        lsr = (L.GetSpatialRef() or w4).Clone(); lsr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(lsr, utm)
        L.SetSpatialFilterRect(*bb); L.SetAttributeFilter(where)
        for f in L:
            ge = f.GetGeometryRef()
            if ge is None:
                continue
            ge = ge.Clone(); ge.Transform(tr)
            for g2 in wf_lines(ge):
                nf = ogr.Feature(into.GetLayerDefn()); nf.SetGeometry(g2)
                if cul is not None:
                    c = int(bool(f[cul])); nf["culvert"] = c; used["culverts"] += c
                into.CreateFeature(nf); used[key] += 1
        L = None; ds = None

    take(osm, "roads", "highway NOT IN (%s)" % ",".join(f"'{h}'" for h in WF_BR_NOROAD), Lb, "roads")
    take(osm, "waterways", "waterway IN ('river','stream','canal')", Lw, "waterways")
    if brs is not None:
        take(brs, None, "railway IS NOT NULL", Lb, "railways")
        take(brs, None, "waterway IS NOT NULL", Lw, "waterways", cul="culvert")
    out = {"breaches": 0, "breach_skipped_long": 0, "osm": used, "breach_data": "ok" if brs else (why or
           "no au-breach.gpkg (no .osm.pbf): roads only, no railways/drains/culvert tags")}
    if not used["waterways"] or not (used["roads"] or used["railways"] or used["culverts"]):
        Lw = Lb = mem = None; D = None
        return out
    # bank mask: roads/rails rasterised, widened to +-WF_BR_HALF
    m = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte); m.SetGeoTransform(gt); m.SetProjection(utm.ExportToWkt())
    gdal.RasterizeLayer(m, [1], Lb, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    bank = m.GetRasterBand(1).ReadAsArray().astype(bool); m = None
    k = max(1, int(math.ceil(WF_BR_HALF / res)))
    yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
    bank = ndimage.binary_dilation(bank, structure=(xx * xx + yy * yy) <= k * k)
    b = D.GetRasterBand(1); z = b.ReadAsArray(); nd = b.GetNoDataValue()
    ok = np.isfinite(z) & (z != nd) if nd is not None else np.isfinite(z)
    step = res / 2; ext = WF_BR_HALF + res

    def cell(xs, ys):
        c = np.floor((xs - x0) / res).astype(np.int64); r = np.floor((y1 - ys) / res).astype(np.int64)
        return r, c, (c >= 0) & (c < W) & (r >= 0) & (r < H)

    def lower(rr, cc, zl):
        sel = ok[rr, cc]
        z[rr[sel], cc[sel]] = np.minimum(z[rr[sel], cc[sel]], zl)

    for f in Lw:
        ge = f.GetGeometryRef(); n_ = ge.GetPointCount()
        p = np.array([ge.GetPoint_2D(i) for i in range(n_)])
        seg = np.hypot(*np.diff(p, axis=0).T); cum = np.concatenate([[0], np.cumsum(seg)])
        if cum[-1] <= 0:
            continue
        s = np.arange(0, cum[-1] + step / 2, step)
        xs = np.interp(s, cum, p[:, 0]); ys = np.interp(s, cum, p[:, 1])
        r, c, inb = cell(xs, ys)
        if not inb.any():
            continue
        if f["culvert"] == 1:                           # the culvert itself: whole line down to the lower ground
            if cum[-1] > WF_BR_MAX + 2 * WF_BR_HALF:      # just beyond either end (mapped pipes are short)
                out["breach_skipped_long"] += 1; continue
            u0 = (p[0] - p[1]) / max(seg[0], 1e-9); u1 = (p[-1] - p[-2]) / max(seg[-1], 1e-9)
            er, ec, ein = cell(np.array([p[0][0] + u0[0] * ext, p[-1][0] + u1[0] * ext]),
                               np.array([p[0][1] + u0[1] * ext, p[-1][1] + u1[1] * ext]))
            zs = [z[er[i], ec[i]] for i in (0, 1) if ein[i] and ok[er[i], ec[i]]]
            if len(zs) == 2:
                lower(r[inb], c[inb], min(zs)); out["breaches"] += 1
            continue
        inside = np.zeros(len(s), bool); inside[inb] = bank[r[inb], c[inb]]
        if not inside.any():
            continue
        d = np.diff(np.concatenate([[0], inside.view(np.int8), [0]]))
        for a_, e_ in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):   # run = samples a_..e_-1 in the bank
            if a_ == 0 or e_ >= len(s) or not (inb[a_ - 1] and inb[e_]):     # no ground on both sides
                continue
            if s[e_ - 1] - s[a_] > WF_BR_MAX:
                out["breach_skipped_long"] += 1; continue
            zs = [z[q] for q in ((r[a_ - 1], c[a_ - 1]), (r[e_], c[e_])) if ok[q]]
            if not zs:
                continue
            lower(r[a_:e_], c[a_:e_], min(zs)); out["breaches"] += 1
    b.WriteArray(z); b.FlushCache(); b = D = None
    Lw = Lb = mem = None
    return out


WF_GRASS = r"""set -e
r.in.gdal -o input="$WF_DEM" output=dem --q
g.region raster=dem
r.watershed -a elevation=dem accumulation=acc tci=tci basin=basin threshold=$WF_BASIN memory=$WF_MEM --q
r.stream.extract elevation=dem accumulation=acc threshold=$WF_STREAM stream_length=$WF_SLEN \
  stream_vector=streams memory=$WF_MEM --q
r.out.gdal -c -f input=acc output="$WF_ACC" type=Float32 createopt=COMPRESS=ZSTD,TILED=YES,BIGTIFF=IF_SAFER --q
r.out.gdal -c -f input=tci output="$WF_TCI" type=Float32 createopt=COMPRESS=ZSTD,TILED=YES,BIGTIFF=IF_SAFER --q
nl=$(v.info -t map=streams | sed -n 's/^lines=//p')
if [ "${nl:-0}" -gt 0 ]; then
  v.out.ogr input=streams output="$WF_VEC" output_layer=streams format=GPKG --q --overwrite
fi
nb=$(r.univar -g map=basin | sed -n 's/^n=//p')
if [ "${nb:-0}" -gt 0 ]; then
  r.to.vect -s input=basin output=basins type=area --q
  U=""; [ -f "$WF_VEC" ] && U="-u"
  v.out.ogr $U input=basins output="$WF_VEC" output_layer=basins format=GPKG --q --overwrite
fi
"""


def wf_classify(km2):
    return "river" if km2 >= 50 else ("creek" if km2 >= 2 else "gully")


def wf_lines(g):
    """LineStrings (>= 2 points) in an OGR geometry (clip results can be multi / collections)."""
    from osgeo import ogr
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbLineString:
        if g.GetPointCount() >= 2:
            yield g
    elif t in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
        for k in range(g.GetGeometryCount()):
            yield from wf_lines(g.GetGeometryRef(k))


def wf_polys(g, into):
    from osgeo import ogr
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbPolygon:
        into.AddGeometry(g)
    elif t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for k in range(g.GetGeometryCount()):
            wf_polys(g.GetGeometryRef(k), into)
    return into


def cmd_terrain_water(ctx):
    """Where water runs and sits: flow lines (gullies/creeks), catchments, wetness and likely wet/dig spots."""
    need_root(ctx); a = ctx.a
    if not a.from_:
        raise CliError(E_USAGE, "need --from POS", "lmap terrain water --from '55K DU 76962 61149' --radius-km 5 --yes")
    pos = parse_pos(a.from_)
    if not 0.5 <= a.radius_km <= 80:
        raise CliError(E_USAGE, f"--radius-km {a.radius_km}: 0.5-80")
    if a.res is not None and not 2 <= a.res <= 100:
        raise CliError(E_USAGE, f"--res {a.res}: 2-100 m")
    if not 0.005 <= a.stream_km2 <= 10:
        raise CliError(E_USAGE, f"--stream-km2 {a.stream_km2}: 0.005-10")
    if not 0.01 <= a.catchment_km2 <= 10000 or a.catchment_km2 < a.stream_km2:
        raise CliError(E_USAGE, f"--catchment-km2 {a.catchment_km2}: 0.01-10000 and not below --stream-km2")
    if not 0 <= a.wet_spots <= 1000:
        raise CliError(E_USAGE, f"--wet-spots {a.wet_spots}: 0-1000")
    name = a.name or ("water-" + to_mgrs(*pos, digits=3).replace(" ", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", name) or name.endswith("-wetness"):
        raise CliError(E_USAGE, f"bad --name {name!r}", "start with a letter/digit; letters, digits, - _ . (max 40)")
    rad = a.radius_km * 1000
    dem, ground, res = wf_pick(ctx, pos, rad, a.res)
    zone = utm_zone(pos[1]); south = pos[0] < 0; epsg = (32700 if south else 32600) + zone
    tr = _utm_tr(zone, south); cx, cy = tr.TransformPoint(pos[1], pos[0])[:2]
    pad = wf_pad(rad, res)                               # computed on a bigger box; results kept inside --radius-km
    x0 = math.floor((cx - rad - pad) / res) * res; x1 = math.ceil((cx + rad + pad) / res) * res
    y0 = math.floor((cy - rad - pad) / res) * res; y1 = math.ceil((cy + rad + pad) / res) * res
    W, H = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
    keep = (cx - rad, cy - rad, cx + rad, cy + rad)
    cell_km2 = res * res / 1e6
    stream_c = int(round(a.stream_km2 / cell_km2))
    if stream_c < 5:
        raise CliError(E_USAGE, f"--stream-km2 {a.stream_km2} is only {stream_c} cells at {res:g} m (every cell a stream)",
                       f"--stream-km2 {5 * cell_km2:.4g} or more, or a finer --res")
    basin_c = max(stream_c, int(round(a.catchment_km2 / cell_km2)))
    W_ = ctx.path("data/australia") / "water"; vec = W_ / f"{name}.gpkg"; wet = W_ / f"{name}-wetness.cog.tif"
    base = {"name": name, "centre": to_mgrs(*pos), "radius_km": a.radius_km, "dem": dem.path.name,
            "dem_res_m": round(ground, 1), "res_m": res, "utm_epsg": epsg, "size_px": [W, H], "pad_m": round(pad),
            "stream_km2": a.stream_km2, "catchment_km2": a.catchment_km2, "out": [str(vec), str(wet)],
            "breach": a.breach,
            "note": ("bare-earth terrain, cut through road/rail banks where OSM maps a waterway crossing or a culvert; "
                     "unmapped culverts, pipes and drains are not known" if a.breach else
                     "bare-earth terrain only (--no-breach): flow lines run along road embankments on flat ground")
                    + ". Wet spots are where water should collect, not proof of water"}
    if a.dry_run:
        return dict(ok=True, dry_run=True, action="terrain.water", exists=vec.exists() or wet.exists(), **base)
    if (vec.exists() or wet.exists()) and not a.yes:
        raise CliError(E_REFUSED, f"refusing to overwrite {name} without --yes", "another --name, or --yes")
    if not a.yes:
        raise CliError(E_REFUSED, "refusing to write without --yes", "preview: same command with --dry-run")
    need_tool("grass", "gdalwarp", "gdal_translate")
    install_signals()
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    t0 = time.time(); sc = ctx.scratch(); gh = sc / f"wf-home.{os.getpid()}"; gh.mkdir(exist_ok=True)
    demf, accf, tcif, vtmp = gh / "dem.tif", gh / "acc.tif", gh / "tci.tif", gh / "v.gpkg"
    vpart, wpart = gh / "out.gpkg", gh / "out.tif"           # built locally: SQLite can't lock on a network share
    parts = []
    try:
        _run(["gdalwarp", "-q", "-overwrite", "-t_srs", f"EPSG:{epsg}", "-te", x0, y0, x1, y1, "-tr", res, res,
              "-r", "bilinear", "-ot", "Float32", *(["-srcnodata", dem.nd] if dem.nd is not None else []),
              "-dstnodata", "-32768", "-co", "TILED=YES", "-co", "COMPRESS=ZSTD", "-co", "BIGTIFF=IF_SAFER",
              "-multi", "-wo", "NUM_THREADS=ALL_CPUS", dem.path, demf], ctx)
        if wf_mask_sea(demf) < 0.01:
            raise CliError(E_NOTFOUND, "no land in the window (all sea or nodata)", "a point on land")
        if ABORT.is_set():
            raise Aborted()
        br = wf_breach(ctx, demf, epsg) if a.breach else {"breaches": 0, "breach_skipped_long": 0,
                                                          "breach_data": "off (--no-breach)"}
        if ABORT.is_set():
            raise Aborted()
        script = gh / "wf.sh"; script.write_text(WF_GRASS)
        env = {**os.environ, "WF_DEM": str(demf), "WF_ACC": str(accf), "WF_TCI": str(tcif), "WF_VEC": str(vtmp),
               "WF_BASIN": str(basin_c), "WF_STREAM": str(stream_c),
               "WF_SLEN": str(max(1, int(round(30 / res)))), "WF_MEM": str(max(300, min(8000, int(wf_mem_mb() * 0.6)))),
               "TMPDIR": str(gh), "HOME": str(gh), "GRASS_VERBOSE": "0"}
        with BuildSlot(ctx, f"water/{name}"):
            _run(["grass", "--tmp-project", f"EPSG:{epsg}", "--exec", "bash", script], ctx, env=env, timeout=7200)
        if ABORT.is_set():
            raise Aborted()
        stats = wf_finish(ctx, accf, tcif, vtmp, vpart, wpart, res, epsg, a.wet_spots, base, keep)
        if ABORT.is_set():
            raise Aborted()
        W_.mkdir(parents=True, exist_ok=True)
        for s_, o_ in ((wpart, wet), (vpart, vec)):              # copy both, then rename both: a pair or nothing
            parts.append(Path(f"{o_}.{os.getpid()}.part")); copy_synced(s_, parts[-1])
        if ABORT.is_set():
            raise Aborted()
        replace_retry(parts[0], wet); replace_retry(parts[1], vec)
    finally:
        for p_ in parts:
            p_.unlink(missing_ok=True)
        shutil.rmtree(gh, ignore_errors=True)
    out = dict(ok=True, action="terrain.water", **base, **br, **stats, seconds=round(time.time() - t0, 1))
    out["next"] = "lmap au project --yes  (adds 'Water flow %s' under Terrain analysis, off)" % name
    return out


def wf_finish(ctx, accf, tcif, vtmp, vpart, wpart, res, epsg, n_spots, base, keep):
    """Streams get upstream km2 + class; catchments merged per basin; both cut to the asked box. Wet spots from the
    wetness index, inside the box. Wetness COG (top of the range only)."""
    from osgeo import gdal, ogr, osr
    import numpy as np
    from scipy import ndimage
    cell_km2 = res * res / 1e6
    A = gdal_open(accf); gt = A.GetGeoTransform(); ab = A.GetRasterBand(1); prj = A.GetProjection()
    up = ab.ReadAsArray().astype("float32", copy=False); and_ = ab.GetNoDataValue(); ab = A = None
    T = gdal_open(tcif); tci = T.GetRasterBand(1).ReadAsArray().astype("float32", copy=False)
    tnd = T.GetRasterBand(1).GetNoDataValue(); T = None
    ok = np.isfinite(up)
    if and_ is not None:
        ok &= up != np.float32(and_)
    land = ok & np.isfinite(tci)
    if tnd is not None:
        land &= tci != np.float32(tnd)
    if not land.any():
        raise CliError(E_NOTFOUND, "no land in the window (all sea or nodata)")
    up[~ok] = 0; ok = None; np.abs(up, out=up); up *= cell_km2           # km2 draining through each cell
    H, W = up.shape
    utm = osr.SpatialReference(); utm.ImportFromEPSG(epsg); utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    w4 = osr.SpatialReference(); w4.ImportFromEPSG(4326); w4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to4 = osr.CoordinateTransformation(utm, w4)
    cell = lambda x, y: (min(H - 1, max(0, int((gt[3] - y) / res))), min(W - 1, max(0, int((x - gt[0]) / res))))
    kc0, kc1 = [min(W, max(0, int(round((v - gt[0]) / res)))) for v in (keep[0], keep[2])]   # the asked-for box
    kr0, kr1 = [min(H, max(0, int(round((gt[3] - v) / res)))) for v in (keep[3], keep[1])]
    # wetness: "wet" = top 5% of land cells AND >= the stream area / 4 upstream (not every flat paddock)
    tl = tci[land]; wet_t, lo_t, hi_t = [float(v) for v in np.percentile(tl, [95, 80, 99.9])]; tl = None
    wetm = land & (tci >= wet_t) & (up >= base["stream_km2"] / 4)
    eg = max(3, int(round(100 / res)))                  # flow piles up where it leaves the computed box
    wetm[:eg] = wetm[-eg:] = False; wetm[:, :eg] = wetm[:, -eg:] = False
    lab, n = ndimage.label(wetm, structure=np.ones((3, 3)))
    wetm = None
    spots = []
    if n:
        idx = np.arange(1, n + 1)
        sizes = ndimage.sum_labels(lab > 0, lab, idx)
        upmax = ndimage.maximum(up, lab, idx)                   # rank: water through the patch
        best = ndimage.maximum_position(tci, lab, idx)          # mark: its wettest cell
        for k, (r, c) in enumerate(best):
            if sizes[k] < 3 or not (kr0 <= r < kr1 and kc0 <= c < kc1):
                continue
            spots.append((float(upmax[k]), int(sizes[k]), r, c))
        spots.sort(reverse=True)
    lab = None
    spots = spots[:n_spots]
    # vector: streams (+ upstream km2, class), catchments, wet spots - lines/areas cut to the asked box
    kp = ogr.Geometry(ogr.wkbPolygon); ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((keep[0], keep[1]), (keep[2], keep[1]), (keep[2], keep[3]), (keep[0], keep[3]), (keep[0], keep[1])):
        ring.AddPoint_2D(x, y)
    kp.AddGeometry(ring)
    ext = (gt[0], gt[3] - H * res, gt[0] + W * res, gt[3])
    src = ro_ogr(vtmp) if vtmp.exists() else None
    drv = ogr.GetDriverByName("GPKG"); vpart.unlink(missing_ok=True)
    ds = drv.CreateDataSource(str(vpart))
    S = ds.CreateLayer("streams", utm, ogr.wkbLineString)
    for nm, t in (("upstream_km2", ogr.OFTReal), ("class", ogr.OFTString)):
        S.CreateField(ogr.FieldDefn(nm, t))
    ncls = {"gully": 0, "creek": 0, "river": 0}; km = {"gully": 0.0, "creek": 0.0, "river": 0.0}
    Ls = src.GetLayerByName("streams") if src is not None else None
    for f in (Ls if Ls is not None else []):
        ge = f.GetGeometryRef()
        if ge is None or ge.GetPointCount() < 2 or not ge.Intersects(kp):
            continue
        n_ = ge.GetPointCount()                         # upstream area: the most along the line, leaving out both
        if n_ > 2:                                      # ends (one is a junction with a bigger stream, either way round)
            u = max(float(up[cell(*ge.GetPoint_2D(j))]) for j in range(1, n_ - 1))
        else:
            (xa, ya), (xb, yb) = ge.GetPoint_2D(0), ge.GetPoint_2D(1)
            u = float(up[cell((xa + xb) / 2, (ya + yb) / 2)])
        g0 = ogr.Geometry(ogr.wkbLineString)
        for j in range(n_):
            g0.AddPoint_2D(*ge.GetPoint_2D(j))
        part = g0 if kp.Contains(g0) else g0.Intersection(kp)
        cl = wf_classify(u); got = False
        for g2 in wf_lines(part):
            nf = ogr.Feature(S.GetLayerDefn()); nf.SetGeometry(g2); nf["upstream_km2"] = round(u, 3); nf["class"] = cl
            S.CreateFeature(nf); km[cl] += g2.Length() / 1000; got = True
        ncls[cl] += got
    B = ds.CreateLayer("catchments", utm, ogr.wkbMultiPolygon)
    B.CreateField(ogr.FieldDefn("area_km2", ogr.OFTReal)); B.CreateField(ogr.FieldDefn("cut", ogr.OFTInteger))
    nb = ncut = 0; groups = {}
    Lb = src.GetLayerByName("basins") if src is not None else None
    if Lb is not None:
        dfn = Lb.GetLayerDefn(); names = [dfn.GetFieldDefn(k).GetName() for k in range(dfn.GetFieldCount())]
        key = "value" if "value" in names else ("cat" if "cat" in names else None)   # r.to.vect: value = basin id
        for f in Lb:
            ge = f.GetGeometryRef()
            if ge is None:
                continue
            k_ = f[key] if key else f.GetFID()
            wf_polys(ge, groups.setdefault(k_, ogr.Geometry(ogr.wkbMultiPolygon)))
    for mp in groups.values():
        if mp.GetGeometryCount() == 0 or not mp.Intersects(kp):
            continue
        full = mp.GetArea(); ins = mp.Clone() if kp.Contains(mp) else wf_polys(mp.Intersection(kp), ogr.Geometry(ogr.wkbMultiPolygon))
        if ins.GetGeometryCount() == 0 or ins.GetArea() <= 0:
            continue
        e = mp.GetEnvelope()                            # (minx, maxx, miny, maxy): touching the computed edge = truncated
        cut = (ins.GetArea() < full * 0.999 or e[0] <= ext[0] + 2 * res or e[1] >= ext[2] - 2 * res
               or e[2] <= ext[1] + 2 * res or e[3] >= ext[3] - 2 * res)
        nf = ogr.Feature(B.GetLayerDefn()); nf.SetGeometry(ins); nf["area_km2"] = round(ins.GetArea() / 1e6, 3)
        nf["cut"] = int(cut); B.CreateFeature(nf); nb += 1; ncut += cut
    P = ds.CreateLayer("wet_spots", utm, ogr.wkbPoint)
    for nm, t in (("rank", ogr.OFTInteger), ("mgrs", ogr.OFTString), ("upstream_km2", ogr.OFTReal),
                  ("wet_m2", ogr.OFTInteger64), ("wetness", ogr.OFTReal)):
        P.CreateField(ogr.FieldDefn(nm, t))
    out_spots = []
    for k, (u, sz, r, c) in enumerate(spots):
        x, y = gt[0] + (c + 0.5) * res, gt[3] - (r + 0.5) * res
        lo, la = to4.TransformPoint(x, y)[:2]; m = to_mgrs(la, lo)
        nf = ogr.Feature(P.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(x, y); nf.SetGeometry(g)
        nf["rank"] = k + 1; nf["mgrs"] = m; nf["upstream_km2"] = round(u, 3); nf["wet_m2"] = int(sz * res * res)
        nf["wetness"] = round(float(tci[r, c]), 1); P.CreateFeature(nf)
        if k < 10:
            out_spots.append({"rank": k + 1, "mgrs": m, "upstream_km2": round(u, 2), "wet_m2": int(sz * res * res)})
    S = B = P = nf = Ls = Lb = None
    ds = None; src = None
    land_km2 = round(float(land[kr0:kr1, kc0:kc1].sum()) * cell_km2, 1)
    up = None
    # wetness raster: 0 = nodata, 1..250 = wetness from the 80th percentile up (the dry bulk is left out)
    b8 = np.zeros((H, W), "uint8")
    mm = land & (tci >= lo_t); land = None
    b8[mm] = np.clip(1 + (tci[mm] - lo_t) / max(hi_t - lo_t, 1e-6) * 249, 1, 250).astype("uint8")
    mm = tci = None
    tmp = wpart.with_suffix(".raw.tif")
    o = gdal.GetDriverByName("GTiff").Create(str(tmp), W, H, 1, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=ZSTD"])
    o.SetGeoTransform(gt); o.SetProjection(prj); bb = o.GetRasterBand(1); bb.SetNoDataValue(0); bb.WriteArray(b8)
    o.SetMetadataItem("LMAP_WATER", json.dumps({"wet_threshold": round(wet_t, 2), "scale": [round(lo_t, 2), round(hi_t, 2)],
                                                "res_m": res}))
    bb = o = None
    try:
        _run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "OVERVIEW_RESAMPLING=NEAREST",
              "-co", "BIGTIFF=IF_SAFER", tmp, wpart], ctx)
    finally:
        tmp.unlink(missing_ok=True)
    return {"streams": ncls, "stream_km": {k: round(v, 1) for k, v in km.items()}, "catchments": nb,
            "catchments_cut": ncut, "wet_spots": len(spots), "top_wet_spots": out_spots, "land_km2": land_km2}


# Print sheet: the area's project in a layout. Map in the area's MGA zone (= UTM on GDA2020; MGRS is the same
# grid on WGS84, ~1.7 m apart - invisible at 1:25k). 1 km grid, labelled with the 2-digit MGRS numbers.
PROJ_PY = r"""
import sys, json, os, re
from qgis.core import *
_ORIG = {}                                           # clone layer id -> project layer id (legend)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont
QgsApplication.setPrefixPath('/usr', True); app = QgsApplication([], False); app.initQgis()
a = json.loads(sys.argv[1])
p = QgsProject.instance(); assert p.read(a['qgz']), 'cannot open project'
"""

# contour choice shared by print and image: sets _vis (layers to draw) and pick
PICK_PY = r"""
_src = lambda l: l.source().split('|')[0]
_ph = lambda l: re.match(r'phone-(topo|photo)', os.path.basename(_src(l))) is not None   # the project's phone-map tiles: never drawn into a map
_vis = [l.layer() for l in p.layerTreeRoot().findLayers() if l.isVisible() and l.layer() and not _ph(l.layer())]
_all = [l.layer() for l in p.layerTreeRoot().findLayers() if l.layer() and not _ph(l.layer())]
_isc = lambda l: (os.path.basename(_src(l)) in ('qld-contours.gpkg', 'contours-dem.gpkg', 'contours-best.gpkg', 'contours-lidar.gpkg', 'nsw-contours.gpkg')
                  or 'qld-contours' in _src(l) or 'contours.mbtiles' in l.source())   # view tiles (statewide) never print
_lid = next((l for l in _all if 'qld-contours' in _src(l) and 'layername=contour_lidar' in l.source()), None)
_dem = next((l for l in _all if os.path.basename(_src(l)) == 'contours-dem.gpkg'), None)
con = a['contours']; pick = None
if con != 'ticked':
    _bl = next((l for l in _all if os.path.basename(_src(l)) == 'contours-best.gpkg'
                and l.name().startswith(f"Contours {a['interval']}m")), None)
    if con == 'best' and _bl is not None:          # the project's own 10 m / 20 m view of the best lines (none: exit 4)
        pick = _bl.clone(); pick.setName(_bl.name()); p.addMapLayer(pick, False); _ORIG[pick.id()] = _bl.id()
    elif con == 'lidar':
        pick = _lid
    elif con == 'dem':
        pick = _dem
    if pick is not None:
        pick.setScaleBasedVisibility(False)
    at = next((i for i, l in enumerate(_vis) if _isc(l)), None)
    _vis = [l for l in _vis if not _isc(l)]
    if pick is not None:
        if at is None:
            at = next((i for i, l in enumerate(_vis) if l.type() == QgsMapLayerType.RasterLayer), len(_vis))
        _vis.insert(min(at, len(_vis)), pick)
# --interval on a dem/lidar-only area (no contours-best, e.g. outside QLD): thin the lines to it (was ignored)
if pick is not None and con in ('dem', 'lidar') and a['interval'] != 10 and pick.fields().indexOf('elevation_m') >= 0:
    _i = _vis.index(pick) if pick in _vis else None
    _o0 = pick; pick = pick.clone(); p.addMapLayer(pick, False); _ORIG[pick.id()] = _ORIG.get(_o0.id(), _o0.id())
    _ss = pick.subsetString(); _f = f'CAST(round("elevation_m") AS INTEGER) % {a["interval"]} = 0'
    pick.setSubsetString(f'({_ss}) AND {_f}' if _ss else _f)     # keep the layer's own filter
    if _i is not None: _vis[_i] = pick
# contours are the lowest line work: under roads, tracks, water, points (projects list contour groups high up)
_cl = [l for l in _vis if l is pick or _isc(l)]
if _cl:
    _vis = [l for l in _vis if l not in _cl]
    _ri = next((i for i, l in enumerate(_vis) if l.type() == QgsMapLayerType.RasterLayer), len(_vis))
    _vis[_ri:_ri] = _cl
"""

# --style topo (print default): a paper map, not a photo. Imagery/topo/colour rasters out; hillshade faint over a
# paper tone; contours (the picked set) heavy brown with heavier labelled index lines; creeks + water bodies blue
# from the area's QLD layers (watercourses aren't in the project); tracks/roads/field lines/places/heights from
# the project's own styled layers, drawn whether ticked or not. --style photo = the ticked layers as before.
TOPO_PY = r"""
PAPER_RGB = (252, 250, 240)
SMALL_SCALE = 300000                                 # coarser than this (phone z11 1:454k and out, print overviews): overview, towns only, no point clutter; z12 (1:227k) keeps all
if a.get('style') == 'topo':
    _d = os.path.dirname(a['qgz'])
    def _gp(fn, ln, name):
        pth = os.path.join(_d, fn)
        if not os.path.isfile(pth):
            return None
        l = QgsVectorLayer(f'{pth}|layername={ln}', name, 'ogr')
        if not l.isValid() or l.featureCount() == 0:
            return None
        p.addMapLayer(l, False); return l
    def _proj(test):                                 # project layers (ticked or not) whose source matches
        return [l for l in _all if test(os.path.basename(_src(l)), l.source(), l.name())]
    def _ln(col, w, dash=None):
        s = QgsLineSymbol.createSimple({'color': col, 'width': str(w), 'capstyle': 'round', 'joinstyle': 'round'})
        if dash:
            s.symbolLayer(0).setUseCustomDashPattern(True); s.symbolLayer(0).setCustomDashVector(dash)
        return s
    _STY = a['mstyle']
    def _sty(cls):                                   # a style.toml [line.<cls>] row (colour, dash, width = line_mm)
        r = _STY['line'][cls]
        s = _ln(r['colour'], r.get('width', _STY['line_mm']), [float(x) for x in r['dash']] or None)
        if r['dash']:
            s.symbolLayer(0).setPenCapStyle(Qt.FlatCap)   # round caps would close short gaps
        return s
    def _gtrk(kind):                                 # 1:50k+: one dashed line from the style table
        return _sty('track_4wd' if kind == '4wd' else 'track')
    def _trk():                                      # official roads/tracks = the project's 'Track' (dash-dot yellow)
        if a['scale'] >= 50000: return _gtrk('track')
        _f = 0.6 if a['scale'] >= 50000 else 1.0     # 1:50k+: thinner (GA vehicle track 0.2-0.4 mm)
        s = _ln('0,0,0,170', 0.5 * _f, [2.0, 0.6, 0.4, 0.6]); y = _ln('255,220,40,255', 0.35 * _f, [2.0, 0.6, 0.4, 0.6])
        s.appendSymbolLayer(y.symbolLayer(0).clone()); return s
    WATER, WFILL = '40,110,200,255', '170,205,240,255'
    def _noscale(l):                                 # topo shows everything at print scale: rule + label limits off
        l.setScaleBasedVisibility(False)
        rd = l.renderer() if l.type() == QgsMapLayerType.VectorLayer else None
        rd = rd.clone() if rd is not None else None
        if isinstance(rd, QgsRuleBasedRenderer):
            for ru in rd.rootRule().descendants():
                ru.setMinimumScale(0); ru.setMaximumScale(0)
            l.setRenderer(rd)
        lb = l.labeling() if l.type() == QgsMapLayerType.VectorLayer else None
        lb = lb.clone() if lb is not None else None  # type-check the fresh clone: a cached sip wrapper of a deleted labeling can lie
        if isinstance(lb, QgsRuleBasedLabeling):
            for ru in lb.rootRule().descendants():
                ru.setMinimumScale(0); ru.setMaximumScale(0)
                if ru.settings():
                    st_ = QgsPalLayerSettings(ru.settings()); st_.scaleVisibility = False; ru.setSettings(st_)
            l.setLabeling(lb)
        elif lb is not None and hasattr(lb, 'settings'):
            st_ = QgsPalLayerSettings(lb.settings()); st_.scaleVisibility = False; l.setLabeling(QgsVectorLayerSimpleLabeling(st_))
    WATER, WFILL = '40,110,200,255', '170,205,240,255'
    _top = []
    # the user's imported layers (lmap import add; group 'Imported'): on top of everything, as ticked
    _top += [l for l in _vis if os.path.basename(_src(l)).startswith('imp-')]
    # points + labels: places, named peaks, spot heights (field-feature points: only the ticked ones)
    _top += _proj(lambda b, s, n: b == 'spot-heights.gpkg' or (b == 'osm.gpkg' and n in ('Towns/places', 'Named peaks (OSM)')))
    _top += [l for l in _vis if l.type() == QgsMapLayerType.VectorLayer and l.geometryType() == Qgis.GeometryType.Point
             and l not in _top]
    # roads/tracks: OSM roads (project style), QLD official roads (track style), field lines (fences, tracks)
    _rl = _proj(lambda b, s, n: b == 'osm.gpkg' and 'layername=lines' in s and n.startswith('Roads'))
    if a['scale'] >= 50000:                          # 1:50k+: GA-style thin streets/tracks, no casing (hills read)
        for _k, _l in enumerate(_rl):
            _c = _l.clone(); p.addMapLayer(_c, False); _rr = _c.renderer()
            if isinstance(_rr, QgsRuleBasedRenderer):
                _rr = _rr.clone()
                for _ru in _rr.rootRule().children():
                    if _ru.label().startswith('Minor'):
                        _s = _ln('110,110,110,255', 0.14); _s.symbolLayer(0).setRenderingPass(1); _ru.setSymbol(_s)
                    elif _ru.label().startswith(('Track', '4WD')):
                        _ru.setSymbol(_gtrk('4wd' if _ru.label().startswith('4WD') else 'track'))
                    elif _ru.label().startswith('Path') and _ru.symbol() is not None:
                        for _j in range(_ru.symbol().symbolLayerCount()):
                            _sl = _ru.symbol().symbolLayer(_j); _sl.setWidth(_sl.width() * 0.6)
                _c.setRenderer(_rr)
            _rl[_k] = _c
    _top += _rl
    qr = _gp('qld-roads.gpkg', 'roads_tracks', 'Roads & tracks (QLD)')
    if qr:
        qr.setRenderer(QgsSingleSymbolRenderer(_trk())); _top.append(qr)
    for ln_, nm_ in (('roads', 'Roads & tracks (NSW)'), ('fire_trails', 'Fire trails (NSW)')):   # same track style
        nr = _gp('nsw-roads.gpkg', ln_, nm_)
        if nr:
            nr.setRenderer(QgsSingleSymbolRenderer(_trk())); _top.append(nr)
    for _l in _proj(lambda b, s, n: b == 'field.gpkg' and 'field_lines' in s):
        if a['scale'] >= 50000 and _l.renderer() is not None and isinstance(_l.renderer().clone(), QgsCategorizedSymbolRenderer):   # its tracks = the OSM (clone: type-check an owned object)
            _l = _l.clone(); p.addMapLayer(_l, False); _cr = _l.renderer().clone()               # Roads tracks: drawn twice
            for _i, _c in enumerate(_cr.categories()):
                if _c.value() in ('track', '4WD track'): _cr.updateCategoryRenderState(_i, False)
            _l.setRenderer(_cr)
        _top.append(_l)
    # watercourses: the state government lines as the surveyors wrote them; OSM only where no state data.
    # Solid = flows all year, dashed = not (style rows watercourse / watercourse_dry). One rule per source field:
    #   QLD perenniality text ('Perennial' solid) + upper_scale (largest scale a line is for: kept if >= sheet)
    #   NSW perenniality code (0 n/a, 1 perennial, 2 non-perennial, 3 mainly dry)
    _orv = _proj(lambda b, s, n: b == 'osm.gpkg' and n.startswith('Rivers'))
    wc = _gp('qld-surface-water.gpkg', 'watercourses', 'Creeks (QLD)')
    nw = _gp('nsw-water.gpkg', 'watercourses', 'Creeks (NSW)')
    def _wren(dry_expr):
        r = QgsRuleBasedRenderer(_sty('watercourse')); rt = r.rootRule(); rt.removeChildAt(0)
        rt.appendChild(QgsRuleBasedRenderer.Rule(_sty('watercourse_dry'), 0, 0, dry_expr, _STY['line']['watercourse_dry']['legend']))
        rt.appendChild(QgsRuleBasedRenderer.Rule(_sty('watercourse'), 0, 0, 'ELSE', _STY['line']['watercourse']['legend']))
        return r
    if not (wc or nw):
        _top += _orv
    if wc:
        if a['scale'] > 25000 and 'upper_scale' in wc.fields().names():
            wc.setSubsetString(f'"upper_scale" IS NULL OR "upper_scale" >= {int(a["scale"])}')
        wc.setRenderer(_wren("\"perenniality\" IN ('Intermittent', 'Ephemeral')")); _top.append(wc)
    if nw:
        nw.setRenderer(_wren('"perenniality" IN (2, 3)')); _top.append(nw)
    na = _gp('nsw-water.gpkg', 'water_areas', 'Water areas (NSW)')
    if na:
        na.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({'color': WFILL, 'outline_color': WATER,
                                                                            'outline_width': '0.15'}))); _top.append(na)
    for ln_, nm_ in (('lakes', 'Lakes'), ('reservoirs', 'Reservoirs'), ('farm_dams', 'Dams')):
        wb = _gp('qld-surface-water.gpkg', ln_, nm_)
        if wb:
            sy = QgsFillSymbol.createSimple({'color': WFILL, 'outline_color': WATER, 'outline_width': '0.15'})
            if wb.geometryType() == Qgis.GeometryType.Point:
                sy = QgsMarkerSymbol.createSimple({'name': 'circle', 'color': WFILL, 'outline_color': WATER, 'size': '1.6'})
            elif wb.geometryType() == Qgis.GeometryType.Line:
                sy = _ln(WATER, 0.2)
            wb.setRenderer(QgsSingleSymbolRenderer(sy)); _top.append(wb)
    # contours UNDER water (creeks/lakes read across the lines)
    if pick is not None:                             # contours: heavier for paper
        rd = pick.renderer().clone()
        if isinstance(rd, QgsRuleBasedRenderer):
            for ru in rd.rootRule().children():
                ix = ru.label().startswith('Index')
                ru.setSymbol(_ln('150,85,25,255', a['cw'][1] if ix else a['cw'][0]))   # contour_widths()
            pick.setRenderer(rd)
        _top.append(pick)
    elif con == 'ticked':                            # contours as ticked in the project
        _top += [l for l in _vis if _isc(l)]
    sea = _gp('sea.gpkg', 'sea', 'Sea')               # sea (OSM water polygons): under everything vector, over shading;
    # cut 2% outside the sheet in the area SQL, so the outline's cut edges fall outside the map frame
    if sea:
        sea.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({'color': WFILL, 'outline_color': WATER,
                                                                             'outline_width': '0.2'}))); _top.append(sea)
    hs = next((l for l in _all if l.type() == QgsMapLayerType.RasterLayer and l.name() == 'Hillshade'), None)
    if hs is not None:                               # no blend mode: one would rasterise the whole PDF map
        hs = hs.clone(); p.addMapLayer(hs, False)
        hs.renderer().setOpacity(0.4); hs.brightnessFilter().setBrightness(80); _top.append(hs)
    _vg = os.path.join(_d, 'vegetation.tif')
    if os.path.isfile(_vg):                          # tree cover pale green, scrub paler (ESA WorldCover 2021)
        vg = QgsRasterLayer(_vg, 'Vegetation (ESA WorldCover 2021)')
        if vg.isValid():
            p.addMapLayer(vg, False)
            vg.setRenderer(QgsPalettedRasterRenderer(vg.dataProvider(), 1, [
                QgsPalettedRasterRenderer.Class(10, QColor(205, 230, 190), 'Trees'),
                QgsPalettedRasterRenderer.Class(20, QColor(228, 238, 212), 'Scrub')]))
            _top.append(vg)
    def _fmt(st_, pt=None):                          # one text size + halo for every name (style text_pt, halo_mm)
        st_ = QgsPalLayerSettings(st_); tf_ = st_.format(); tf_.setSize(float(pt or _STY['text_pt']))
        tf_.setSizeUnit(Qgis.RenderUnit.Points); b_ = tf_.buffer(); b_.setEnabled(float(_STY['halo_mm']) > 0)
        b_.setSize(float(_STY['halo_mm'])); b_.setSizeUnit(Qgis.RenderUnit.Millimeters); tf_.setBuffer(b_)
        if st_.placement == Qgis.LabelPlacement.OverPoint and not st_.isExpression or st_.fieldName == 'elevation_m':
            st_.placement = Qgis.LabelPlacement.AroundPoint; st_.dist = 0.8   # beside the symbol, not on the summit
        st_.setFormat(tf_); return st_
    def _sz_all(l):
        if l.type() != QgsMapLayerType.VectorLayer or not l.labelsEnabled() or l.labeling() is None:
            return
        _pt = float(_STY['place_pt']) if l.name().startswith(('Towns/', 'Villages/')) else None   # places: place_pt (3 mm)
        lb = l.labeling().clone()
        if isinstance(lb, QgsRuleBasedLabeling):
            for ru in lb.rootRule().descendants():
                if ru.settings():
                    ru.setSettings(_fmt(ru.settings(), _pt))
        elif hasattr(lb, 'settings'):
            lb = QgsVectorLayerSimpleLabeling(_fmt(lb.settings(), _pt))
        l.setLabeling(lb)
    seen = set(); _vis = []
    for l in _top:
        if l is not None and l.id() not in seen:
            seen.add(l.id()); _noscale(l); _vis.append(l)
            if a['scale'] >= 50000 and l.type() == QgsMapLayerType.VectorLayer and l.labelsEnabled() \
                    and os.path.basename(_src(l)) == 'osm.gpkg' and l.name().startswith(('Rivers', 'Roads')):
                _lb = l.labeling().clone()           # clone: own the object (no stale sip wrapper); 1:50k+: creek/road names half size (were swamping the hills)
                if isinstance(_lb, QgsVectorLayerSimpleLabeling):
                    _st = QgsPalLayerSettings(_lb.settings()); _tf = _st.format()   # size: style text_pt (_sz_all)
                    _st.setFormat(_tf); _st.priority = 2
                    _st.lineSettings().setMergeLines(True)   # OSM roads are many short ways: a name never fit one
                    if l.name().startswith('Roads'):     # 1:50k: main road names only (streets crowded out places)
                        _st.isExpression = True; _st.priority = 6   # main road names beat creeks (2), yield to places (10)
                        _st.thinningSettings().setAllowDuplicateRemoval(True)   # dual carriageways: one name, not two
                        _st.thinningSettings().setMinimumDistanceToDuplicate(60); _st.thinningSettings().setMinimumDistanceToDuplicateUnit(Qgis.RenderUnit.Millimeters)
                        _st.fieldName = ("CASE WHEN \"highway\" IN ('motorway','trunk','primary','secondary') "
                                         "THEN \"name\" END")
                    l.setLabeling(QgsVectorLayerSimpleLabeling(_st))
                del _lb
            if a['scale'] >= 50000 and l.type() == QgsMapLayerType.VectorLayer and l.labelsEnabled() \
                    and l.name() in ('Towns/places', 'Named peaks (OSM)'):
                _lb = l.labeling().clone()           # clone (see above); place/peak names: placed first, never dropped
                if isinstance(_lb, QgsVectorLayerSimpleLabeling):
                    _st = QgsPalLayerSettings(_lb.settings()); _st.priority = 10
                    _st.placementSettings().setOverlapHandling(Qgis.LabelOverlapHandling.AllowOverlapIfRequired)
                    l.setLabeling(QgsVectorLayerSimpleLabeling(_st))
                del _lb
            if 'field_points' in l.source() and a['scale'] > 30000:     # 1:50k: symbols only, no labels; towers kept, poles off.
                l.setLabelsEnabled(False); l.setSubsetString("kind <> 'power pole'")   # towers stay (bush landmarks); poles = the line. In place: legend stays
            _sz_all(l)
    if a['scale'] > SMALL_SCALE:                     # overview scales (phone z11 and out): _noscale lifted every
        # limit, so towers drew as red X chains, every suburb stacked and dam/height dots covered the map
        _vis = [l for l in _vis if not ('field_points' in l.source() or l.name() in ('Dams', 'Farm dams')
                                        or l.name().startswith('Villages/')      # statewide small places
                                        or os.path.basename(_src(l)) == 'spot-heights.gpkg')]
        for l in _vis:
            if l.name() == 'Towns/places':
                _ss = l.subsetString(); _f = "\"place\" IN ('city','town')"
                l.setSubsetString(f'({_ss}) AND {_f}' if _ss else _f)
    if not _vis:                                     # [] = "all project layers" in a map item: draw paper only
        _e = QgsVectorLayer('Point?crs=EPSG:4326', 'empty', 'memory'); p.addMapLayer(_e, False); _vis = [_e]
"""

# photo styling shared by print sheets and phone tiles: needs _vis, pick, _src; sets _white_bg
PHOTO_PY = r"""
_white_bg = False
# photo sheets: contours must read on any ground colour (--photo-contours). orange = bright line + dark halo
# (works on pale sand and dark scrub); white = brown line + white halo; faded = photo at 65% under dark-brown
# lines (classic photomap); plain = the project's thin brown (gets lost on imagery).
_pv = a.get('pcon', 'plain')
_prd = pick.renderer().clone() if pick is not None and pick.renderer() is not None else None   # owned clone: type check can't see a stale wrapper
if a.get('style') == 'photo' and _pv != 'plain' and isinstance(_prd, QgsRuleBasedRenderer):
    pick.setRenderer(_prd)
    for _r in _prd.rootRule().descendants():
        _sy = _r.symbol()
        if _sy is None:
            continue
        _w = _sy.width(); _ix = _r.label().startswith('Index')
        if _pv in ('white', 'orange'):
            _top = QColor(160, 100, 40) if _pv == 'white' else QColor(255, 150, 30)
            _und = QColor(255, 255, 255, 150) if _pv == 'white' else QColor(0, 0, 0, 150)
            _sy.setColor(_top); _sy.setOpacity(1.0)
            if _ix:                                  # halo on index lines only (steep hills filled in when all had one);
                _sy.insertSymbolLayer(0, QgsSimpleLineSymbolLayer(_und, _w + 0.08))   # line widths = topo
        elif _pv == 'faded':
            _sy.setColor(QColor(110, 50, 0)); _sy.setOpacity(1.0)
    if _pv == 'faded':
        for _l in _vis:
            if _l.type() == QgsMapLayerType.RasterLayer and os.path.basename(_src(_l)).split('.')[0] in ('sat', 'sat-even', 'aerial'):
                _l.renderer().setOpacity(0.65)       # the photo only, not hillshade/topo
        _white_bg = True
_isw = lambda l: (os.path.basename(_src(l)) in ('qld-surface-water.gpkg', 'nsw-water.gpkg')
                  or (os.path.basename(_src(l)) == 'osm.gpkg' and l.name().startswith(('Rivers', 'Water'))))
if pick is not None and pick in _vis:              # water over contours (photo: project order had it under)
    _w = [l for l in _vis[_vis.index(pick) + 1:] if l.type() == QgsMapLayerType.VectorLayer and _isw(l)]
    if _w:
        _vis = [l for l in _vis if l not in _w]; _i = _vis.index(pick); _vis[_i:_i] = _w
"""


PRINT_PY = PROJ_PY + r"""
from qgis.PyQt.QtGui import QPainter
W, H = a['paper']
_PT = 25.4 / 72                                       # QGIS PDF export snaps the page/map to whole points and draws
W, H = round(W / _PT) * _PT, round(H / _PT) * _PT     # the map a hair off-scale otherwise (measured 0.6 m/km at A4 300 dpi)
L = QgsPrintLayout(p); L.initializeDefaults(); L.setName('sheet')
pg = L.pageCollection().page(0); pg.setPageSize(QgsLayoutSize(W, H, QgsUnitTypes.LayoutMillimeters))
_fr = a['frame']; M, top, foot = _fr['M'], _fr['top'], _fr['foot']   # margins: title strip, footer (page_frame)
narrow = _fr['mode'] == 'narrow'; compact = _fr['mode'] == 'compact'     # narrow: info own row; compact: A5/A6
mp = QgsLayoutItemMap(L); mp.setFrameEnabled(True); L.addLayoutItem(mp)
mp.attemptMove(QgsLayoutPoint(M, top, QgsUnitTypes.LayoutMillimeters))
_mw, _mh = round((W - 2 * M) / _PT) * _PT, round((H - top - foot) / _PT) * _PT   # whole points too (same reason)
mp.attemptResize(QgsLayoutSize(_mw, _mh, QgsUnitTypes.LayoutMillimeters))
mp.setCrs(p.crs())
cx, cy = a['center_xy']; mw = _mw / 1000 * a['scale']; mh = _mh / 1000 * a['scale']
mp.setExtent(QgsRectangle(cx - mw / 2, cy - mh / 2, cx + mw / 2, cy + mh / 2)); mp.setScale(a['scale'])
""" + PICK_PY + TOPO_PY + r"""
# grid overlaid on the map (both styles): the project's MGRS grid lines on top, labels off
# (the margin carries the numbers). The layout grid below only does the zebra frame + margin numbers.
_gl = next((l for l in _all if os.path.basename(_src(l)) == 'grid.gpkg' and 'grid_lines' in l.source()), None)
if _gl is not None and a.get('grid', True):
    _g2 = _gl.clone(); _g2.setName('Grid lines'); _g2.setLabelsEnabled(False); _g2.setScaleBasedVisibility(False); p.addMapLayer(_g2, False); _ORIG[_g2.id()] = _gl.id()
    # print: solid black, no transparency (the project's 170/255 alpha at 0.12 mm printed brownish over the tint)
    _rd = _g2.renderer().clone(); _g2.setRenderer(_rd)   # owned clone; older projects / hand-edited: maybe single-symbol
    _rs = [(r.symbol(), r.label()) for r in _rd.rootRule().children()] if isinstance(_rd, QgsRuleBasedRenderer) \
        else [(_rd.symbol(), '1 km')] if isinstance(_rd, QgsSingleSymbolRenderer) else []
    for _sy, _lb in _rs:
        if _sy is not None:
            _sy.setColor(QColor(0, 0, 0)); _sy.setOpacity(0.0 if a.get('grid', True) else 1.0); _sy.setWidth({'100 km': 0.6, '10 km': 0.35}.get(_lb, 0.18))
    # opacity 0 with the grid on: the layout grids draw the visible lines. Two copies ~0.6 m/km apart looked
    # doubled in PDF viewers zoomed out. The layer stays in the PDF: pdf_georef_fix reads its geometry.
    _vis = [_g2] + [l for l in _vis if l.id() != _gl.id()]
# photo overlay (--overlay): minimal = index contours + roads/tracks + places (+grid); none = imagery only (+grid)
_ov = a.get('overlay', 'full') if a.get('style') == 'photo' else 'full'
if _ov != 'full':
    _keep = lambda l: l.type() == QgsMapLayerType.RasterLayer or l.name() == 'Grid lines'
    if _ov == 'minimal':
        _rd = lambda l: (os.path.basename(_src(l)) in ('qld-roads.gpkg', 'nsw-roads.gpkg') or l.name().startswith('Roads')
                         or l.name() == 'Towns/places' or os.path.basename(_src(l)).startswith('imp-'))
        _vis = [l for l in _vis if _keep(l) or l is pick or _rd(l)]
        for _i, _l in enumerate(_vis):                 # OSM roads: main roads + tracks only (streets show on the photo)
            if os.path.basename(_src(_l)) == 'osm.gpkg' and _l.name().startswith('Roads'):
                _c = _l.clone(); p.addMapLayer(_c, False); _ss = _c.subsetString()
                _f = "\"highway\" NOT IN ('residential','service','living_street','pedestrian','footway','cycleway','steps','path')"
                _c.setSubsetString(f'({_ss}) AND {_f}' if _ss else _f); _vis[_i] = _c
        _r2 = pick.renderer().clone() if pick is not None and pick.renderer() is not None else None
        if isinstance(_r2, QgsRuleBasedRenderer):
            for _ru in _r2.rootRule().children():
                if not _ru.label().startswith('Index'):
                    _ru.setActive(False)
            pick.setRenderer(_r2)
    else:
        _vis = [l for l in _vis if _keep(l)]; pick = None
    _vis = [l for l in _vis if not (l.type() == QgsMapLayerType.RasterLayer and l.name() in ('Hillshade', 'Elevation (coloured)'))] or _vis
""" + PHOTO_PY + r"""
if _white_bg:
    mp.setBackgroundColor(QColor(255, 255, 255))
mp.setLayers(_vis)                                 # (grid.gpkg lines stay: the GeoPDF georef check reads them)
if a.get('style') == 'topo':
    mp.setBackgroundColor(QColor(PAPER_RGB[0], PAPER_RGB[1], PAPER_RGB[2]))
# 1 km grid; labels = the 2 MGRS digits (km inside the 100 km square)
g = QgsLayoutItemMapGrid('MGRS 1 km', mp); mp.grids().addGrid(g)
iv = 1000 if a['scale'] <= 100000 else 10000      # = the 1 km lines (drawn to 1:100k)
g.setIntervalX(iv); g.setIntervalY(iv); g.setCrs(p.crs())
# lines come from the grid.gpkg overlay above; layout grid = zebra frame + margin numbers only
g.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
if _gl is not None and a.get('grid', True):        # the visible lines: layout grids draw AFTER the map, labels
    for _gn, _giv, _gw in (('1 km lines', iv, 0.18), ('10 km lines', iv * 10, 0.35)):   # included = on top of all
        _gg = QgsLayoutItemMapGrid(_gn, mp); mp.grids().addGrid(_gg); _gg.setCrs(p.crs())
        _gg.setIntervalX(_giv); _gg.setIntervalY(_giv); _gg.setStyle(QgsLayoutItemMapGrid.Solid)
        _gg.setAnnotationEnabled(False); _gg.setFrameStyle(QgsLayoutItemMapGrid.NoFrame)
        _gg.setLineSymbol(QgsLineSymbol.createSimple({'color': '0,0,0,255', 'width': str(_gw)}))
g.setAnnotationEnabled(True); g.setAnnotationFormat(QgsLayoutItemMapGrid.CustomFormat)
g.setAnnotationExpression("lpad(to_string(floor((@grid_number % 100000) / 1000)), 2, '0')" if iv == 1000 else
                          "to_string(floor((@grid_number % 100000) / 10000))")
for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right, QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
    g.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame, side)
    g.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, side)
f = QgsTextFormat(); f.setFont(QFont('Liberation Sans')); f.setSize(5 if compact else 6.5); g.setAnnotationTextFormat(f)
g.setFrameStyle(QgsLayoutItemMapGrid.Zebra); g.setFrameWidth(0.7 if compact else 1.2)
if compact:
    g.setAnnotationFrameDistance(0.5)
def text(t, x, y, w, h, size=8, bold=False):
    lb = QgsLayoutItemLabel(L); lb.setText(t); ft = QgsTextFormat(); fo = QFont('Liberation Sans'); fo.setBold(bold)
    ft.setFont(fo); ft.setSize(size); lb.setTextFormat(ft); L.addLayoutItem(lb)
    lb.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters)); lb.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
    return lb
_ts = 9 if compact else 14                         # title strip type scales with the sheet
text(a['title'], M, 0.8 if compact else 3, W * 0.5 - M if compact else W * 0.6, top - 3 if compact else top - 2, _ts, True)
text(f"1:{a['scale']:,}".replace(',', ' ') + f"    {a['sq']}", W * 0.6, 0.4 if compact else 4, W * 0.4 - M, 4.2 if compact else top - 3,
     7.5 if compact else 11, True).setHAlign(2)
if a.get('incomplete'):                           # a layer this style needs is missing: say so ON the sheet
    _inc = text('INCOMPLETE: ' + a['incomplete'], W * 0.3, 0.4 if compact else 3, W * 0.4, 4.2 if compact else top - 3,
                7.5 if compact else 12, True); _inc.setHAlign(4); _f = _inc.textFormat(); _f.setColor(QColor(200, 0, 0)); _inc.setTextFormat(_f)
# the interval ACTUALLY drawn, measured from the lines on this sheet (never assumed): gcd of the gaps between
# the distinct heights inside the map frame (capped read; spatial index)
_ci_m = None
if pick is not None and pick.fields().indexOf('elevation_m') >= 0:
    import math
    _ct = QgsCoordinateTransform(p.crs(), pick.crs(), p)
    _rq = QgsFeatureRequest().setFilterRect(_ct.transformBoundingBox(mp.extent())).setLimit(20000)
    _rq.setSubsetOfAttributes(['elevation_m'], pick.fields())
    _ev = sorted({int(round(f['elevation_m'])) for f in pick.getFeatures(_rq) if f['elevation_m'] is not None})
    _g = 0
    for _x, _y in zip(_ev, _ev[1:]):
        _g = math.gcd(_g, _y - _x)
        if _g == 1: break
    _ci_m = _g or None
if con in ('dem', 'lidar', 'best') and not _ci_m and pick is not None:
    a['ci_text'] = f"CONTOUR INTERVAL {a['interval']} m (index {a['interval'] * 5} m)"
if _ci_m and con != 'ticked':                       # measured from the drawn lines
    a['ci_text'] = f"CONTOUR INTERVAL {_ci_m} m (index {_ci_m * 5} m)"
elif (con != 'ticked' or _ov == 'none') and pick is None:
    a['ci_text'] = 'NO CONTOURS'
if _ov == 'minimal' and pick is not None and _ci_m:   # only the index lines are drawn
    a['ci_text'] = f"INDEX CONTOURS ONLY, every {_ci_m * 5} m"
if compact:                                        # between the title and the scale, small
    text(a['ci_text'].replace('CONTOUR INTERVAL', 'CI'), W * 0.5, 4.7, W * 0.5 - M, 2.2, 4, True).setHAlign(2)  # under the scale
else:
    text(a['ci_text'], W * 0.45, 5.5, W * 0.55 - M - 48, 6, 8, True).setHAlign(2)   # contour interval (from the data used), left of the scale
yb = H - foot + 6
sb = QgsLayoutItemScaleBar(L); sb.setLinkedMap(mp); sb.setStyle('Single Box'); sb.setUnits(QgsUnitTypes.DistanceMeters)
sb.setUnitLabel('m'); sb.setNumberOfSegments(2); sb.setNumberOfSegmentsLeft(1)
sb.setUnitsPerSegment(500 if a['scale'] <= 25000 else 1000 if a['scale'] <= 50000 else 5000)
if compact:
    sb.setNumberOfSegments(1); sb.setNumberOfSegmentsLeft(0); sb.setHeight(1.8)
    _room = min(52, (W - 2 * M - 22) / 2) - 12      # mm left of the declination text, less the end label
    sb.setUnitsPerSegment(next(u for u in (5000, 2000, 1000, 500, 250, 100) if u / a['scale'] * 1000 <= _room or u == 100)); sb.setLabelBarSpace(0.8); _sf = QgsTextFormat(sb.textFormat()); _sf.setSize(6); sb.setTextFormat(_sf)
L.addLayoutItem(sb); sb.attemptMove(QgsLayoutPoint(M, yb, QgsUnitTypes.LayoutMillimeters))
info = (f"GRID  MGA zone {a['zone']} (GDA2020) = UTM.  MGRS {a['zone']}{a['band']} {a['sq']}: read the 2-digit numbers on the "
        f"grid lines (easting first). MGRS/GPS/ATAK are WGS84: ~1.7 m from MGA, ignore at this scale.\n"
        f"NORTH  Grid convergence {abs(a['conv_mil']):.0f} mils {'east' if a['conv_mil'] >= 0 else 'west'}.  "
        f"Grid-magnetic angle (GMA) {abs(a['gma_mil']):.0f} mils {'east' if a['gma_mil'] >= 0 else 'west'}: "
        f"GRID -> MAG {'subtract' if a['gma_mil'] >= 0 else 'add'} {abs(a['gma_mil']):.0f};  "
        f"MAG -> GRID {'add' if a['gma_mil'] >= 0 else 'subtract'} {abs(a['gma_mil']):.0f}.\n"
        f"CONTOURS  {a['ci_text'].capitalize()}.  {a['contour_note']}\n"
        + (f"{a['acc']}\n" if a.get('acc') else "") +
        f"DATA  Imagery: {a['imagery']}.  Terrain {a['dem']}.  Built {a['made']} by lmap {a['ver']} - things change; check on the ground.")
# bottom margin, left to right: scale bar over the declination block | north diagram | info | legend.
# Narrow paper (portrait): info moves to its own row under the rest (foot is taller, see above).
X0 = M + 122                                      # right of the north diagram
IW = 74 if a['legend'] else W - M - X0            # info: a narrow wrapped column, the legend gets the rest
LW = (W - M - X0 - IW - 3 if W >= 250 else W - M - X0) if a['legend'] else 0
if compact:
    pass                                          # A5/A6: no notes block
elif narrow:
    text(info, M, H - 27, W - 2 * M, 24, 5.5)
else:
    text(info, X0, yb - 1, IW, foot - 6, 5)
# north diagram: true / grid / magnetic north; both angles scaled by one factor (order + sides kept), values in mils
import math, os, tempfile
dg, dm = a['conv_mil'] * 360 / 6400, a['decl_mil'] * 360 / 6400
big = max(abs(dg), abs(dm), 1e-9); k = min(max(3, 7 / big), 25 / big)
ag, am = dg * k, dm * k
def _arm(ang, txt, col, head, sx, ln=66):       # sx: label side (+ right, - left, 0 above)
    t = math.radians(ang); x2, y2 = 30 + ln * math.sin(t), 96 - ln * math.cos(t)
    lx, ly, anc = (x2, y2 - 8, 'middle') if sx == 0 else (x2 + sx, y2 + 3, 'start' if sx > 0 else 'end')
    return (f'<line x1="30" y1="96" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="1.6"/>{head(x2, y2)}'
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-family="Liberation Sans" font-size="8" fill="{col}" text-anchor="{anc}">{txt}</text>')
def star(x, y, r=5):                              # 5-point star as a polygon (no font glyph needed)
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.4; t = math.radians(i * 36)
        pts.append(f"{x + rr * math.sin(t):.1f},{y - rr * math.cos(t):.1f}")
    return f'<polygon points="{" ".join(pts)}" fill="#000"/>'
gn = lambda x, y: f'<rect x="{x - 2.5:.1f}" y="{y - 2.5:.1f}" width="5" height="5" fill="#000"/>'
hb = lambda x, y: f'<polygon points="{x - 3:.1f},{y + 6:.1f} {x + 3:.1f},{y + 6:.1f} {x:.1f},{y - 2:.1f}" fill="#b00000"/>'
svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-30 0 120 100" width="120" height="100">'
       + _arm(0, 'TN', '#000', star, 0)
       + _arm(ag, f"GN {abs(a['conv_mil']):.0f}", '#000', gn, 5 if ag >= 0 else -5, 46)   # GN shorter: labels never meet
       + _arm(am, f"MN {abs(a['decl_mil']):.0f}", '#b00000', hb, 5 if am >= 0 else -5) + '</svg>')
svgf = tempfile.NamedTemporaryFile(suffix='.svg', delete=False); svgf.write(svg.encode()); svgf.close()
pic = QgsLayoutItemPicture(L); pic.setPicturePath(svgf.name, Qgis.PictureFormat.SVG); L.addLayoutItem(pic)
if compact:                                      # small diagram at the right, declination text beside the scale bar
    pic.attemptMove(QgsLayoutPoint(W - M - 22, H - foot + 1, QgsUnitTypes.LayoutMillimeters))
    pic.attemptResize(QgsLayoutSize(22, foot - 2, QgsUnitTypes.LayoutMillimeters))
else:
    pic.attemptMove(QgsLayoutPoint(M + 80, yb - 4, QgsUnitTypes.LayoutMillimeters))
    pic.attemptResize(QgsLayoutSize(40, 33, QgsUnitTypes.LayoutMillimeters))
rate = a['decl_rate_mil']; rdeg = abs(rate) * 360 / 6400
grow = (rate >= 0) == (a['decl_mil'] >= 0)       # moving away from true north = growing
every = f"1 deg every {1 / rdeg:.0f} yr" if rdeg > 0.005 else "steady"
_dx = min(52, (W - 2 * M - 22) / 2)              # compact: declination text starts here (half the free width on A6 portrait)
text(f"MAGNETIC DECLINATION\n{abs(a['decl_mil']):.0f} mils ({abs(a['decl_deg']):.1f} deg) {a['decl_dir'].upper()} at {a['year']:.1f} ({a.get('wmm', 'WMM')})\n"
     f"{'grows' if grow else 'shrinks'} {abs(rate):.1f} mils/yr ({every})\n"
     f"GMA {abs(a['gma_mil']):.0f} mils.  Diagram angles not to scale.", *((M + _dx, H - foot + 4, W - 2 * M - _dx - 22, foot - 5, 4.5)
        if compact else (M, yb + 13, 76, 15, 6)), False)
if compact:                                       # grid zone line (the GRID note's essentials) under the scale bar
    text(f"MGA zone {a['zone']} (GDA2020), MGRS {a['zone']}{a['band']} {a['sq']}: easting first.\n"
         f"GRID->MAG {'subtract' if a['gma_mil'] >= 0 else 'add'} {abs(a['gma_mil']):.0f} mils.", M, H - 9, _dx - 2, 8, 4.5)
# stroke-only marks (x = 'cross2', cross, line): a 0.4 mm stroke drew the x ~2x the other symbols. Thin stroke,
# drawn inside symbol_mm, so it is the same size as the rest (map and legend)
_sw = float(a['mstyle']['symbol_mm']) / 4      # arm width (print runs in its own process; the project is never saved): a quarter of the symbol, like the filled cross
def _thin(_s):
    for _sl in _s.symbolLayers():
        if not isinstance(_sl, QgsSimpleMarkerSymbolLayer):
            continue
        if _sl.shape() in (Qgis.MarkerShape.Cross, Qgis.MarkerShape.Cross2, Qgis.MarkerShape.Line):
            _sl.setStrokeWidth(_sw); _sl.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
            _sl.setSize(max(float(a['mstyle']['symbol_mm']) - _sw, _sw)); _sl.setPenCapStyle(Qt.FlatCap)   # from the style, never compounds
    return _s
for l in _vis:
    _r = l.renderer().clone() if l.type() == QgsMapLayerType.VectorLayer and l.renderer() is not None else None
    if _r is not None: l.setRenderer(_r)            # owned clone, edited in place below
    if isinstance(_r, QgsCategorizedSymbolRenderer):
        for _i, _c in enumerate(_r.categories()):
            _r.updateCategorySymbol(_i, _thin(_c.symbol().clone()))
    elif isinstance(_r, QgsSingleSymbolRenderer):
        _r.setSymbol(_thin(_r.symbol().clone()))
if a['legend'] and not compact:
    lg = QgsLayoutItemLegend(L); lg.setLinkedMap(mp); lg.setLegendFilterByMapEnabled(True); lg.setTitle('')
    lg.setColumnCount(max(1, min(int(LW // 32), 8))); lg.setSplitLayer(True); lg.setEqualColumnWidth(False)
    lg.setSymbolHeight(2.5); lg.setSymbolWidth(4); lg.setColumnSpace(2)
    for st in (QgsLegendStyle.SymbolLabel, QgsLegendStyle.Group, QgsLegendStyle.Subgroup):
        sty = lg.rstyle(st); tf = QgsTextFormat(sty.textFormat()); tf.setSize(5.5); sty.setTextFormat(tf)
    lg.setAutoUpdateModel(False); mdl = lg.model().rootGroup()
    _on = {_ORIG.get(l.id(), l.id()) for l in _vis}   # clones (grid, contour picks) stand for their project layer
    for nl in list(mdl.findLayers()):                 # only what is drawn on this sheet; no imagery/terrain colour bands
        if (not nl.layer() or nl.layer().id() not in _on or nl.name() == 'Grid lines'   # grid: explained in the notes
                or (nl.layer().type() == QgsMapLayerType.RasterLayer and 'Slope' not in nl.name())):
            nl.parent().removeChildNode(nl)
    # a drawn clone (contour pick, grid) stands in for its project layer: the project layer's zoom limits would
    # filter it out of the legend, so the legend shows the clone actually drawn
    _cl = {_ORIG[l.id()]: l for l in _vis if l.id() in _ORIG}
    for nl in list(mdl.findLayers()):
        _c = _cl.get(nl.layer().id()) if nl.layer() else None
        if _c is not None:
            _par = nl.parent(); _i = _par.children().index(nl)
            _par.insertLayer(_i, _c).setName(nl.name()); _par.removeChildNode(nl)
    # places are not spot heights: their own heading
    for nl in list(mdl.findLayers()):
        if nl.name() == 'Towns/places':
            _l = nl.layer(); _par = nl.parent(); _i = _par.children().index(nl); _par.removeChildNode(nl)
            _pg = _par.insertGroup(_i, 'Places'); _pg.addLayer(_l).setName('Towns/places')
            QgsLegendRenderer.setNodeLegendStyle(_pg, QgsLegendStyle.Group)
    # drawn layers the project tree doesn't hold (sheet-only clones: 1:50k roads/field lines, state watercourses,
    # tracks): add them, so every line on the sheet is in the legend
    _have = {nl.layer().id() for nl in mdl.findLayers() if nl.layer()}
    _lines = None
    for l in _vis:
        if (l.type() == QgsMapLayerType.VectorLayer and l.id() not in _have and _ORIG.get(l.id(), l.id()) not in _have
                and l.name() not in ('empty', 'Grid lines', '1 km lines', '10 km lines')):
            if _lines is None:
                _lines = mdl.insertGroup(0, 'Lines')
            _lines.addLayer(l); _have.add(l.id())
    def _prune(g):                                    # drop groups left empty
        for c in list(g.children()):
            if isinstance(c, QgsLayerTreeGroup):
                _prune(c)
                if not c.children():
                    g.removeChildNode(c)
    _prune(mdl)
    for g in mdl.findGroups(True):                    # group holding one layer: one heading, not two
        if len(g.children()) == 1 and not isinstance(g.children()[0], QgsLayerTreeGroup) and g.name() != 'Places':
            QgsLegendRenderer.setNodeLegendStyle(g, QgsLegendStyle.Hidden)
        for c in g.children():                        # 'Spot heights' > 'Spot heights (terrain)': drop the echo
            if (not isinstance(c, QgsLayerTreeGroup) and len(g.children()) > 1 and c.name().startswith(g.name())
                    and len(lg.model().layerLegendNodes(c)) > 1):   # single-symbol: its name is the only label
                QgsLegendRenderer.setNodeLegendStyle(c, QgsLegendStyle.Hidden)
    for st, sp in ((QgsLegendStyle.Symbol, 0.2), (QgsLegendStyle.SymbolLabel, 0.2), (QgsLegendStyle.Group, 0.8), (QgsLegendStyle.Subgroup, 0.6)):
        lg.rstyle(st).setMargin(QgsLegendStyle.Top, sp)
    if _lines is not None:
        QgsLegendRenderer.setNodeLegendStyle(_lines, QgsLegendStyle.Hidden)   # its layers carry their own headings
    L.addLayoutItem(lg); lg.attemptMove(QgsLayoutPoint(W - M - LW, yb - 2, QgsUnitTypes.LayoutMillimeters))
    # fit the 33 mm box: measure the rendered legend; more columns, then smaller text, until it fits
    _rc = QgsLayoutUtils.createRenderContextForLayout(L, None); _rc.setRendererScale(a['scale'])
    def _lsize():
        lg.updateFilterByMap(False); lg.refresh(); lg.setResizeToContents(True); lg.adjustBoxSize()   # map-filtered rows
        return QgsLegendRenderer(lg.model(), lg.legendSettings()).minimumSize(_rc)
    _sz = None
    for _ts in (5.5, 5.0, 4.5, 4.0, 3.5, 3.0):
        lg.setSymbolHeight(2.5 * _ts / 5.5)
        for st in (QgsLegendStyle.SymbolLabel, QgsLegendStyle.Group, QgsLegendStyle.Subgroup):
            _sy = lg.rstyle(st); _tf = QgsTextFormat(_sy.textFormat()); _tf.setSize(_ts); _sy.setTextFormat(_tf)
        for _nc in range(1, 9):
            lg.setColumnCount(_nc); _sz = _lsize()
            if _sz.width() > LW or _sz.height() <= 30:   # 3 mm spare: the measure runs a little short
                break
        if _sz.width() > LW and _nc > 1:
            lg.setColumnCount(_nc - 1); _sz = _lsize()
        if _sz.height() <= 30:
            break
    print(f"legend {lg.columnCount()} cols, text {_ts} pt, {_sz.width():.0f}x{_sz.height():.0f} mm in {LW:.0f}x33", file=sys.stderr)
    lg.setResizeToContents(False); lg.attemptResize(QgsLayoutSize(LW, 33, QgsUnitTypes.LayoutMillimeters))
ex = QgsLayoutExporter(L); s = QgsLayoutExporter.PdfExportSettings(); s.dpi = a['dpi']
s.writeGeoPdf = a['geopdf']; s.appendGeoreference = True; s.rasterizeWholeImage = False
s.forceVectorOutput = True    # lines/text as vectors: GeoPDF otherwise stored the map as 4 full-page images (A1 347 MB)
try:
    r = ex.exportToPdf(a['out'], s)
finally:
    os.unlink(svgf.name)
print(json.dumps({"result": int(r), "no_contours": a['contours'] not in ('none', 'ticked') and pick is None and _ov != 'none'}))
app.exitQgis()
"""


# Big flat image: the project's ticked layers (contours picked as in print) drawn tile by tile into one GeoTIFF.
# Each tile is drawn with a margin and cropped, so lines and symbols join across tile edges. Labels must lie wholly
# inside their tile: none is cut or doubled at a seam, but one that would cross a seam is left out.
IMAGE_PY = PROJ_PY + r"""
import numpy as np
from osgeo import gdal
from qgis.PyQt.QtGui import QImage, QPainter
from qgis.PyQt.QtCore import QSize
gdal.UseExceptions()
""" + PICK_PY + r"""
if a['contours'] not in ('none', 'ticked') and pick is None:     # say so before hours of drawing
    print(json.dumps({"result": 0, "no_contours": True})); app.exitQgis(); sys.exit(0)
crs = QgsCoordinateReferenceSystem(a['crs']); assert crs.isValid(), 'bad crs'
x0, y0, x1, y1 = a['extent']; res = a['res']; W, H = a['size']; T = a['tile']; Mg = a['margin']
gdal.SetCacheMax(512 * 1024 * 1024)
ds = gdal.GetDriverByName('GTiff').Create(a['out'], W, H, 3, gdal.GDT_Byte,
        ['TILED=YES', 'BLOCKXSIZE=512', 'BLOCKYSIZE=512', 'COMPRESS=ZSTD', 'BIGTIFF=YES', 'PHOTOMETRIC=RGB',
         'INTERLEAVE=BAND'])
ds.SetGeoTransform((x0, res, 0, y1, 0, -res)); ds.SetProjection(crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL))
bg = QColor(255, 255, 255)
les = QgsLabelingEngineSettings(p.labelingEngineSettings())   # partial candidates would be cut at a seam
les.setFlag(Qgis.LabelingFlag.UsePartialCandidates, False)
n = 0; tot = ((W + T - 1) // T) * ((H + T - 1) // T)
for ty in range(0, H, T):
    for tx in range(0, W, T):
        w, h = min(T, W - tx), min(T, H - ty)
        ms = QgsMapSettings(); ms.setLayers(_vis); ms.setDestinationCrs(crs); ms.setBackgroundColor(bg)
        ms.setOutputDpi(a['dpi']); ms.setOutputSize(QSize(w + 2 * Mg, h + 2 * Mg))
        ms.setExtent(QgsRectangle(x0 + (tx - Mg) * res, y1 - (ty + h + Mg) * res, x0 + (tx + w + Mg) * res, y1 - (ty - Mg) * res))
        assert abs(ms.mapUnitsPerPixel() - res) < res * 1e-6, f'tile scale {ms.mapUnitsPerPixel()} != {res}'
        ms.setTransformContext(p.transformContext()); ms.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
        # labels only wholly inside this tile: none cut or doubled at a seam (one crossing a seam is left out)
        ms.setLabelBoundaryGeometry(QgsGeometry.fromRect(
            QgsRectangle(x0 + tx * res, y1 - (ty + h) * res, x0 + (tx + w) * res, y1 - ty * res)))
        ms.setLabelingEngineSettings(les)
        img = QImage(w + 2 * Mg, h + 2 * Mg, QImage.Format_RGB32); img.fill(bg)
        pt = QPainter(img); job = QgsMapRendererCustomPainterJob(ms, pt); job.start(); job.waitForFinished(); pt.end()
        b = img.constBits(); b.setsize(img.sizeInBytes())
        arr = np.frombuffer(b, np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[Mg:Mg + h, Mg:Mg + w]
        for i, c in enumerate((2, 1, 0)):                 # Format_RGB32 = B, G, R, x in memory
            ds.GetRasterBand(i + 1).WriteArray(np.ascontiguousarray(arr[:, :, c]), tx, ty)
        n += 1; print(f"tile {n}/{tot}", file=sys.stderr, flush=True)
ds.FlushCache(); ds = None
print(json.dumps({"result": 0, "no_contours": False}))
app.exitQgis()
"""

PAPER = {"A1": (841, 594), "A2": (594, 420), "A3": (420, 297), "A4": (297, 210), "A5": (210, 148), "A6": (148, 105)}


GEOREF_MAX_SHIFT_M = 50   # a drawn km line further than this from a km value: misread, not QGIS's ~1 m snapping
                          # (snapping to the nearest km would move a 600 m error to the WRONG km, and pass)


def pdf_georef_fix(pdf):
    """Make a GeoPDF's georeference agree with its own drawn 1 km grid. QGIS writes the georef from
    the layout, but draws the map a hair off it (whole-point snapping; ~0.6 m/km measured). Reads the 'Grid lines'
    layer back through GDAL (= what Avenza/QGIS/GDAL get), fits drawn -> true km values, rewrites /GPTS
    (same byte length, xref unchanged) into a .part copy, reads that back, then renames it over pdf: a failure
    leaves the original untouched. Returns (max m off before, after) or None if there is no grid layer."""
    import re
    from osgeo import ogr, osr
    def grid(path=pdf):
        try:                                           # (raises or returns None by the exceptions switch)
            ds = ro_ogr(path)
        except RuntimeError:                           # no vector layers at all: nothing to check
            ds = None
        lyr = next((ds.GetLayer(i) for i in range(ds.GetLayerCount()) if ds.GetLayer(i).GetName() == "Grid lines"), None) if ds else None
        if lyr is None or lyr.GetSpatialRef() is None:
            return None, None, None
        E, N = [], []
        for f in lyr:
            g = f.GetGeometryRef(); pts = g.GetPoints() if g else None
            if not pts:
                continue
            xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
            if max(xs) - min(xs) < 1 and max(ys) - min(ys) > 500: E.append(sum(xs) / len(xs))
            if max(ys) - min(ys) < 1 and max(xs) - min(xs) > 500: N.append(sum(ys) / len(ys))
        return lyr.GetSpatialRef().Clone(), E, N
    off = lambda E, N: max(abs(round(v, -3) - v) for v in E + N)
    srs, E, N = grid()
    if srs is None or not E or not N:
        return None                                    # need a km line each way
    before = off(E, N)
    if before > GEOREF_MAX_SHIFT_M:                    # QGIS is ~1 m off; tens of metres means something is wrong:
        return round(before, 2), None                  # georeference left alone; the caller refuses the sheet

    def fit(v):
        """drawn -> true km: scale + shift from 2+ distinct lines; one line (A6 at 1:25k): shift only."""
        m, t = sum(v) / len(v), sum(round(x, -3) for x in v) / len(v)
        if len({round(x, -3) for x in v}) < 2:
            return 1.0, t - m
        k = sum((x - m) * (round(x, -3) - t) for x in v) / sum((x - m) ** 2 for x in v)
        return k, t - k * m
    fe, fn = fit(E), fit(N)
    d = bytearray(Path(pdf).read_bytes()); m = list(re.finditer(rb"/GPTS\s*\[([^\]]*)\]", d))
    if len(m) != 1:
        return None
    body = m[0].group(1); toks = body.split(); v = [float(t) for t in toks]
    geo = srs.CloneGeogCS()
    for r in (srs, geo):
        r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    fw = osr.CoordinateTransformation(geo, srs); bw = osr.CoordinateTransformation(srs, geo); new = []
    for i in range(0, len(v), 2):
        x, y, _ = fw.TransformPoint(v[i + 1], v[i])
        lon, lat, _ = bw.TransformPoint(fe[0] * x + fe[1], fn[0] * y + fn[1])
        for val, t in ((lat, toks[i]), (lon, toks[i + 1])):
            dec = len(t.split(b".")[1]) if b"." in t else 10; s = f"{val:.{dec}f}".encode()
            if len(s) > len(t):
                return None
            new.append(s.ljust(len(t)))
    out = []; pos = 0
    for t, n in zip(toks, new):
        j = body.index(t, pos); out.append(body[pos:j] + n); pos = j + len(t)
    nb = b"".join(out) + body[pos:]
    d[m[0].start(1):m[0].end(1)] = nb
    part = Path(str(pdf) + ".part")
    try:
        with open(part, "wb") as f:
            f.write(bytes(d)); f.flush(); os.fsync(f.fileno())
        try:
            _, E2, N2 = grid(part)
        except Exception:
            E2 = N2 = None                             # rewritten but unreadable: refuse (below)
        if not E2 or not N2:
            return round(before, 2), 999.0             # can't read the rewrite back: caller refuses the sheet
        os.replace(part, pdf)
    finally:
        part.unlink(missing_ok=True)
    return round(before, 2), round(off(E2, N2), 2)


ACC_SHARE = HERE / "share" / "accuracy.json"


def acc_entries(ctx):
    """Measured accuracy entries: share/accuracy.json (shipped) + <root>/accuracy.json (`lmap verify` adds)."""
    out = []
    for p in (ACC_SHARE, ctx.path("accuracy.json")):
        try:
            out += json.loads(Path(p).read_text()).get("entries", [])
        except (OSError, ValueError):
            pass
    return out


def _acc_in(e, box):
    """The whole sheet (box w,s,e,n degrees) inside the entry's measured region."""
    r = e.get("region")
    return bool(r) and r[0] <= box[0] and box[2] <= r[2] and r[1] <= box[1] and box[3] <= r[3]


def nsw_stated_vacc(p, box):
    """Sorted distinct NSW verticalaccuracy (m) of the contour lines in box (w,s,e,n degrees); [] if none/unknown."""
    from osgeo import ogr, osr
    if not readable(p):
        return []
    ds = ro_ogr(p); L = ds.GetLayerByName("contours") if ds is not None else None
    if L is None or L.GetLayerDefn().GetFieldIndex("verticalaccuracy") < 0:
        return []
    sr = L.GetSpatialRef(); w, s_, e, n = box
    if sr is not None and not sr.IsGeographic():
        ll = osr.SpatialReference(); ll.ImportFromEPSG(4326); ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        s2 = sr.Clone(); s2.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); t = osr.CoordinateTransformation(ll, s2)
        xs, ys = zip(*[t.TransformPoint(x, y)[:2] for x in (w, e) for y in (s_, n)]); w, s_, e, n = min(xs), min(ys), max(xs), max(ys)
    L.SetSpatialFilterRect(w, s_, e, n); L.SetIgnoredFields([f for f in ("classsubtype", "capturemethodcode", "capturesourcecode", "elevation_m", "objectid")
                                                              if L.GetLayerDefn().GetFieldIndex(f) >= 0])
    v = {round(float(f["verticalaccuracy"]), 1) for f in L if f["verticalaccuracy"]}
    return sorted(x for x in v if x > 0)


def acc_line(ctx, meta, d, con, scale, box, style):
    """Footer ACCURACY line, from what this sheet actually draws and what has been measured.
    Typical error = source vs ground (where measured, for this region) + lmap's drawing (mm on paper x scale).
    Anything unmeasured is said to be not verified; estimates are marked 'est.'. Medians (typical), rounded UP.
    Drawing entries count only for topo sheets (where they were measured); a sheet must lie wholly inside a
    measured region for that region's number."""
    import math as _m
    E = acc_entries(ctx); have = {i.get("key"): i.get("from") for i in meta.get("items", [])}
    def last(kind, **kw):
        c = [e for e in E if e.get("kind") == kind and all(e.get(k) == v for k, v in kw.items())]
        return max(c, key=lambda e: (e.get("date", ""), e.get("median_mm", e.get("median_m", 0)))) if c else None
    def plot_m(layer, ref):                             # worst (largest) measured drawing error for this layer+source
        c = [e for e in E if e.get("kind") == "plot" and e.get("layer") == layer and e.get("ref") == ref
             and e.get("median_mm") is not None and layer != "roads"     # roads are not measured: the state line
             and (layer != "contours" or e.get("method") == "drawn-src")] if style == "topo" else []
        return (max(e["median_mm"] for e in c) * scale / 1000) if c else None
    up = lambda v: max(1, _m.ceil(v - 1e-9))
    parts = []
    rsrcs = [s_ for k_, s_ in (("qld-roads", "qld-roads-state"), ("nsw-roads", "nsw-roads-state"))
             if k_ in have and readable(d / f"{k_}.gpkg")] + (["osm-au"] if "osm" in have else [])   # OSM roads drawn too
    for rsrc in rsrcs:
        nm = {"qld": "QLD", "nsw": "NSW"}.get(rsrc[:3], "OSM")
        here = [e for e in E if e.get("kind") == "source" and e.get("layer") == "roads" and e.get("source") == rsrc]
        hit = max((e for e in here if _acc_in(e, box)), key=lambda e: e.get("date", ""), default=None)
        pm = plot_m("roads", rsrc)
        how = lambda e_: e_.get("check") or (f"vs {e_['vs']}" if e_.get("vs") else "checked")   # what it was checked against
        p90 = lambda e_: f"; data alone 90% within ~{up(e_['p90_m'])} m" if e_.get("p90_m") is not None else ""
        if hit and pm is not None:
            parts.append(f"Roads ({nm}) ~{up(hit['median_m'] + pm)} m typical (data {how(hit)} here, + drawing){p90(hit)}")
        elif hit:
            parts.append(f"Roads ({nm}) data ~{up(hit['median_m'])} m typical ({how(hit)} here){p90(hit)}; drawing not verified")
        elif here:
            pl = ", ".join(dict.fromkeys(e.get("place", "?") for e in here))
            part_ = any(e.get("region") and overlaps(e["region"], box) for e in here)
            parts.append(f"Roads ({nm}): checked at {pl} only" + (", which covers only part of this sheet"
                                                                    if part_ else ", not on this sheet"))
        else:
            parts.append(f"Roads ({nm}): not verified")
    if "qld-surface-water" in have and readable(d / "qld-surface-water.gpkg"):
        pm = plot_m("watercourses", "qld-surface-water-state")
        parts.append(f"Creeks plotted typically ~{up(pm)} m from QLD data (the data itself not ground-checked)" if pm is not None
                     else "Creeks: not verified")
    if "nsw-water" in have and readable(d / "nsw-water.gpkg"):
        pm = plot_m("watercourses", "nsw-water-state")
        parts.append(f"Creeks (NSW) plotted typically ~{up(pm)} m from NSW data (the data itself not ground-checked)"
                     if pm is not None else "Creeks (NSW): not verified")
    bs = best_srcs(d / "contours-best.gpkg") if con == "best" and readable(d / "contours-best.gpkg") else set()
    if readable(d / "qld-contours.gpkg") and (con == "lidar" or (con == "best" and bs & {"lidar", "lidar-dem"})):
        pm = plot_m("contours", "qld-contours-state"); st = last("stated", layer="contours", source="qld-contours-state")
        h = f", height +/-{st['vertical_m']} m (QLD stated)" if st and _acc_in(st, box) else ""
        dem_only = con == "best" and "lidar" not in bs                  # only LiDAR-DEM lines: the QLD figure isn't theirs
        parts.append((f"Where LiDAR: contours plotted typically ~{up(pm)} m from QLD data{h}" if pm is not None and not dem_only
                      else "LiDAR contours: not verified"))
    if con == "dem" or "approx" in bs:
        dres = next((i.get("res_m") for i in meta.get("items", []) if i.get("key") == "dem"), None) or 30
        err = 10 if dres <= 30 else 20                          # 90 m terrain (the fallback) is much coarser
        parts.append(f"Contours approx. +/-{err} m (est., {dres:g} m terrain)" + (" where no other source" if bs - {"approx"} else ""))
    if "nsw-5m" in bs:                              # NSW states 5 m DEM height accuracy ~0.9 m (LiDAR) .. ~5 m (photogrammetry)
        parts.append("NSW contours from the NSW 5 m elevation model (not field-checked here)")
    if "nsw" in bs:
        pm = plot_m("contours", "nsw-contours-state")
        st = max((e for e in E if e.get("kind") == "source" and e.get("layer") == "contours"
                  and e.get("source") == "nsw-contours-state" and _acc_in(e, box) and e.get("median_m") is not None),
                 key=lambda e: e.get("date", ""), default=None)
        h = (f", height typically ~{up(st['median_m'])} m ({st.get('how', 'checked')} here)" if st else ", height not verified")
        sv = nsw_stated_vacc(d / "nsw-contours.gpkg", box)
        if sv:                                     # NSW's own per-line figure (99a: overlapping batches -> the best kept)
            h += f", NSW stated +/-{sv[0]:g}" + (f" to {sv[-1]:g}" if len(sv) > 1 else "") + " m"
        parts.append((f"NSW contours (Govt mapping, not LiDAR) plotted typically ~{up(pm)} m from NSW data" if pm is not None
                      else "NSW contours (Govt mapping, not LiDAR): position not verified") + h)
    parts.append("photo, vegetation, OSM points: not verified")
    return "ACCURACY (typical; measured unless marked est./stated)  " + ".  ".join(parts) + "."


def _pdf_raster(ctx, pdf, crs, dpi):
    """Render page 1 with pdftoppm and georeference it from the PDF's own /GPTS,/LPTS,/BBox (what phone apps use).
    Returns (array RGB, geotransform, neatline box in crs)."""
    import re, numpy as np
    from osgeo import gdal, osr
    d = Path(pdf).read_bytes()
    def f(k):
        m = re.search(re.escape(k) + rb"\s*\[([^\]]*)\]", d)
        if not m:
            raise CliError(E_FAIL, f"verify: {k.decode()} not found in the PDF", "re-print with lmap >= 0.33")
        return [float(x) for x in m.group(1).split()]
    if len(re.findall(rb"/GPTS", d)) != 1:
        raise CliError(E_FAIL, "verify needs a GeoPDF with exactly one georeferenced map", "re-print with lmap >= 0.33")
    vp = d[max(0, d.index(b"/GPTS") - 3000):d.index(b"/GPTS")]       # the viewport dict that holds the /Measure
    bbs = re.findall(rb"/BBox\s*\[([^\]]*)\]", vp)
    gp, lp = f(b"/GPTS"), f(b"/LPTS")
    bb = [float(x) for x in (bbs[-1] if bbs else re.search(rb"/BBox\s*\[([^\]]*)\]", d).group(1)).split()]
    mb = f(b"/CropBox") if b"/CropBox" in d else f(b"/MediaBox")
    a_, b_ = osr.SpatialReference(), osr.SpatialReference(); a_.ImportFromEPSG(7844); b_.SetFromUserInput(crs)
    for r in (a_, b_):
        r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    T = osr.CoordinateTransformation(a_, b_); k = dpi / 72; P, G = [], []
    for i in range(0, 8, 2):
        x, y, _ = T.TransformPoint(gp[i + 1], gp[i]); G.append((x, y))
        P.append(((bb[0] + lp[i] * (bb[2] - bb[0]) - mb[0]) * k, (mb[3] - (bb[1] + lp[i + 1] * (bb[3] - bb[1]))) * k))
    A = np.c_[np.array(P), np.ones(4)]; Gx = np.array(G)
    cx = np.linalg.lstsq(A, Gx[:, 0], rcond=None)[0]; cy = np.linalg.lstsq(A, Gx[:, 1], rcond=None)[0]
    png = ctx.scratch() / f"verify-{os.getpid()}"
    run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", str(pdf), str(png)], ctx)
    try:
        ds = gdal_open(str(png) + ".png"); img = ds.ReadAsArray()[:3].astype("float32"); ds = None
    finally:
        Path(str(png) + ".png").unlink(missing_ok=True)
    gt = (cx[2], cx[0], cx[1], cy[2], cy[0], cy[1])
    g0 = gdal_open(pdf).GetGeoTransform()           # GDAL's own reading (72 dpi): must agree with ours
    if abs(g0[0] - gt[0]) > 5 or abs(g0[3] - gt[3]) > 5 or abs(g0[1] / (gt[1] * k) - 1) > 0.002:
        raise CliError(E_FAIL, "verify: could not read the PDF's map frame reliably (disagrees with GDAL)",
                       "re-print with lmap >= 0.33")
    return img, gt, (min(g[0] for g in G), min(g[1] for g in G), max(g[0] for g in G), max(g[1] for g in G))


def _profile_fit(mask, gt, lines, half, wmin, wmax):
    """Perpendicular profiles every 10 m along reference lines (crs coords): centre of the printed symbol (mask)
    across each; least-squares E/N shift of the print vs the reference + residuals (metres)."""
    import numpy as np
    from scipy import ndimage
    f = ndimage.gaussian_filter(mask.astype("float32"), 0.7); rows = []
    o = np.arange(-half, half + 0.5, 0.5)
    for p in lines:
        if len(p) < 2:
            continue
        seg = np.hypot(*np.diff(p, axis=0).T); cum = np.r_[0, np.cumsum(seg)]
        for s_ in np.arange(5, cum[-1] - 5, 10):
            i = min(max(np.searchsorted(cum, s_) - 1, 0), len(seg) - 1)
            if seg[i] < 1:
                continue
            u = (p[i + 1] - p[i]) / seg[i]; x, y = p[i] + (s_ - cum[i]) * u; n_ = np.array([-u[1], u[0]])
            X = x + o * n_[0]; Y = y + o * n_[1]
            c = (X - gt[0]) / gt[1]; r = (Y - gt[3]) / gt[5]
            if c.min() < 1 or r.min() < 1 or c.max() > f.shape[1] - 2 or r.max() > f.shape[0] - 2:
                continue
            v = ndimage.map_coordinates(f, [r, c], order=1)
            if v.max() < 0.5 or v[0] > 0.3 or v[-1] > 0.3:
                continue                                   # need one clear crossing
            m = v > 0.5
            if ndimage.label(m)[1] != 1 or not wmin <= m.sum() * 0.5 <= wmax:
                continue
            rows.append((n_[0], n_[1], float((o * m).sum() / m.sum())))
    if len(rows) < 50:
        return None
    A = np.array(rows); sh = np.linalg.lstsq(A[:, :2], A[:, 2], rcond=None)[0]; res = np.abs(A[:, 2] - A[:, :2] @ sh)
    return {"n": len(A), "shift_e_m": float(sh[0]), "shift_n_m": float(sh[1]),
            "median_m": float(np.median(res)), "p90_m": float(np.percentile(res, 90))}


def _ref_lines(path, layer, crs, box, where=None, mod10=False):
    import numpy as np
    from osgeo import ogr, osr
    ds = ro_ogr(path); L = ds.GetLayerByName(layer) if ds else None
    if L is None:
        return []
    t = osr.SpatialReference(); t.SetFromUserInput(crs)
    for r in (t, L.GetSpatialRef()):
        r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    T = osr.CoordinateTransformation(L.GetSpatialRef(), t); out = []
    if where:
        L.SetAttributeFilter(where)
    for ft in L:
        if mod10 and (ft.GetField("elevation_m") is None or round(ft.GetField("elevation_m")) % int(mod10)):   # drawn interval
            continue
        g = ft.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone(); g.Transform(T); g.FlattenTo2D()
        for pp in ([g.GetGeometryRef(i) for i in range(g.GetGeometryCount())] if g.GetGeometryCount() else [g]):
            a = np.array(pp.GetPoints() or [])
            if len(a) > 1:
                a = a[(a[:, 0] > box[0]) & (a[:, 0] < box[2]) & (a[:, 1] > box[1]) & (a[:, 1] < box[3])]
                if len(a) > 1:
                    out.append(a)
    return out


VERIFY_LAYERS = [   # layer, file, gpkg layer, reference source, print colour test (topo style), half, wmin, wmax, where
    # No roads rows: state roads print as a thin dark dashed line OVER OSM's orange fill, and the old colour
    # test found the orange fill - it measured OSM's drawing against the state data, not lmap's drawing of either.
    ("watercourses", "qld-surface-water.gpkg", "watercourses", "qld-surface-water-state",
     lambda R, G, B: (B > 150) & (R < 130) & (B - R > 60), 20, 1, 12, None),
    ("watercourses", "nsw-water.gpkg", "watercourses", "nsw-water-state",
     lambda R, G, B: (B > 150) & (R < 130) & (B - R > 60), 20, 1, 12, None),
    # contours: the lines the sheet DREW from that source (contours-best src), not the whole source file (where the
    # print used LiDAR-DEM / approx / the other state's lines instead, that was data vs data)
    ("contours", "contours-best.gpkg", "contours", "qld-contours-state",
     lambda R, G, B: (R > 100) & (R < 190) & (G > 50) & (G < 140) & (B < 110) & (R - B > 50), 20, 1, 8, "src = 'lidar'"),
    ("contours", "contours-best.gpkg", "contours", "nsw-contours-state",
     lambda R, G, B: (R > 100) & (R < 190) & (G > 50) & (G < 140) & (B < 110) & (R - B > 50), 20, 1, 8, "src = 'nsw'")]


def cmd_verify(ctx):
    """lmap verify NAME: measure a printed topo sheet against the data it was drawn from.
    1) GeoPDF georef vs its own drawn grid; 2) creeks / contours on the print vs the lines the sheet drew (QLD, NSW;
    roads not measured - the state line is drawn over OSM's fill),
    with a self-test (known 12E/7N shift must come back). --yes saves the results (mm on paper) to
    <root>/accuracy.json, which print footers read."""
    need_root(ctx); need_tool("pdftoppm")
    a = ctx.a; d = sheet_dir(ctx, a.name)
    if not (d / "area.json").is_file():
        raise CliError(E_NOTFOUND, f"no map or area called {a.name}", "lmap map ... --yes first")
    pdfs = sorted(d.glob("print-*.pdf"), key=lambda p: ("topo" in p.name, p.stat().st_mtime))   # newest topo first
    pdf = (d / a.pdf) if a.pdf else (pdfs[-1] if pdfs else None)
    if pdf is None or not pdf.is_file():
        raise CliError(E_NOTFOUND, f"no print in {d}", "lmap map ... --yes (or --pdf print-....pdf)")
    meta = json.loads((d / "area.json").read_text()); crs = meta["crs"]
    try:                                               # what the sheet drew (sidecar): interval, contour set, scale
        side = json.loads(pdf.with_suffix(".json").read_text())
    except (OSError, ValueError):
        side = {}
    m_ = re.search(r"-(\d+)k\.pdf$", pdf.name)
    scale = a.scale or side.get("scale") or (int(m_.group(1)) * 1000 if m_ else None)
    if not scale:
        raise CliError(E_USAGE, f"can't tell the scale of {pdf.name}", "--scale 25000")
    _bs = best_srcs(d / "contours-best.gpkg") if side.get("contours") == "best" and readable(d / "contours-best.gpkg") else set()
    todo = [L for L in VERIFY_LAYERS if readable(d / L[1])
            and (L[1] != "contours-best.gpkg" or L[8].split("'")[1] in _bs)]   # only sources this sheet drew
    civ = int(side.get("interval") or 0) if side.get("contours") == "best" else 0   # rows read contours-best lines
    if side.get("scale") and a.scale and int(side["scale"]) != int(a.scale):
        raise CliError(E_USAGE, f"--scale {a.scale} but {pdf.name} was printed at 1:{side['scale']}", "drop --scale")
    if not civ:                                        # not 'best' contours: no contour measurement
        if any(L[0] == "contours" for L in todo):
            ctx.log("verify: contours not measured (the sheet was not printed with --contours best)")
        todo = [L for L in todo if L[0] != "contours"]
    if "topo" not in pdf.name:
        raise CliError(E_USAGE, "verify reads topo-style sheets (by colour): print-topo-*.pdf", "--pdf print-topo-...pdf")
    plan = {"action": "verify", "pdf": str(pdf), "scale": scale, "layers": [f"{L[0]} ({L[3].split('-')[0].upper()})" for L in todo],
            "saves_to": str(ctx.path("accuracy.json")) if a.yes else None}
    if a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    import shutil as _sh, numpy as np
    from scipy import ndimage
    tmp = ctx.scratch() / f"verify-{os.getpid()}.pdf"; _sh.copyfile(pdf, tmp)
    try:
        g = pdf_georef_fix(tmp)                          # on a copy: (off before = this file's, after)
    except Exception as e:
        g = None; ctx.log(f"georef check failed: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    out = {"ok": True, **plan, "georef_vs_grid_m": round(g[0], 2) if g else None}
    with MemTurn(ctx):
        DPI = 300; img, gt, box = _pdf_raster(ctx, pdf, crs, DPI)
        R, G, B = img; inner = (box[0] + 150, box[1] + 150, box[2] - 150, box[3] - 150)
        mm = 1000 / scale; res = {}
        for name, fn, ly, ref, col, half, w0, w1, wh in todo:
            lines = _ref_lines(d / fn, ly, crs, inner, where=wh, mod10=civ if name == "contours" else 0)
            mask = col(R, G, B); r0 = _profile_fit(mask, gt, lines, half, w0, w1)
            rk = f"{name} ({ref.split('-')[0].upper()})"                # 'contours (QLD)', 'contours (NSW)'
            if r0 is None:
                res[rk] = {"note": "too few clear crossings", "ref": ref}; continue
            px = 25.4 / DPI / 1000 * scale                          # metres per pixel
            sr, sc_ = max(1, round(7 / px)), max(1, round(12 / px))  # whole pixels; the shift actually applied:
            te, tn = sc_ * px, -sr * px                            # east +, rows down = south
            r1 = _profile_fit(ndimage.shift(mask.astype("uint8"), (sr, sc_), order=0) > 0, gt, lines, half, w0, w1)
            rec = (((r1["shift_e_m"] - r0["shift_e_m"]) * te + (r1["shift_n_m"] - r0["shift_n_m"]) * tn) /
                   (te ** 2 + tn ** 2)) if r1 else None
            res[rk] = {"layer": name, "ref": ref, "n": r0["n"], **{k: round(r0[k], 1) for k in ("shift_e_m", "shift_n_m", "median_m", "p90_m")},
                         "selftest_recovery": round(rec, 2) if rec is not None else None,
                         "trusted": rec is not None and 0.6 <= rec <= 1.4}
        del img, R, G, B
    out["layers"] = res
    if g and g[0] > 0.5:
        out["warning"] = f"georef {g[0]} m off its own grid: re-print with lmap >= 0.33"
    if a.yes and (g is None or g[0] > 0.5):
        out["saved"] = []
        out["warning"] = (out.get("warning") or "georef vs grid could not be checked") + " (nothing saved)"
    elif a.yes:
        p = ctx.path("accuracy.json")
        try:
            cur = json.loads(p.read_text())
        except (OSError, ValueError):
            cur = {"entries": []}
        place = f"{a.name} {pdf.name}, lmap {VERSION}"
        for rk, r in res.items():
            if r.get("trusted"):
                name = r["layer"]                            # one entry per layer AND source (border sheets: QLD + NSW)
                cur["entries"] = [e for e in cur["entries"] if not (e.get("kind") == "plot" and e.get("layer") == name
                                                                   and e.get("ref") == r["ref"] and e.get("place") == place)]
                cur["entries"].append({"kind": "plot", "layer": name, "ref": r["ref"], "n": r["n"],
                                       "place": place,
                                       "shift_e_mm": round(r["shift_e_m"] * mm, 3), "shift_n_mm": round(r["shift_n_m"] * mm, 3),
                                       "median_mm": round(r["median_m"] * mm, 3), "p90_mm": round(r["p90_m"] * mm, 3),
                                       "recovery": r["selftest_recovery"], "date": time.strftime("%Y-%m-%d"), "by": "lmap verify",
                                       **({"method": "drawn-src", "interval_m": civ} if name == "contours" else {})})
        atomic_write(p, json.dumps(cur, indent=1)); out["saved"] = [n for n, r in res.items() if r.get("trusted")]
    return out


def page_frame(W, H):
    """Page layout in mm, shared by print and footprint: margin, title strip, footer, mode. compact (A5/A6):
    footer = scale bar + declination/GMA + grid zone + contour interval only (a pocket sheet is all map)."""
    if min(W, H) < 180:
        return {"M": 5, "top": 9, "foot": 25, "mode": "compact"}
    return {"M": 8, "top": 14, "foot": 64 if W < 250 else 38, "mode": "narrow" if W < 250 else "wide"}


def sheet_map_mm(W, H):
    f = page_frame(W, H)
    return W - 2 * f["M"], H - f["top"] - f["foot"]


def area_contours(d, con, iv):
    """--contours / --interval for print and image -> (contour set, interval, best gpkg, footer note)."""
    if iv != 10 and con not in ("best", "dem", "lidar"):
        raise CliError(E_USAGE, f"--interval {iv} works with --contours best, dem or lidar")
    best = d / "contours-best.gpkg"; lid = d / "qld-contours.gpkg"; demc = d / "contours-dem.gpkg"
    if con == "best" and not readable(best):
        con = "lidar" if readable(lid) else "dem" if readable(demc) else "none"
    need = {"lidar": lid, "dem": demc}.get(con)
    if need is not None and not readable(need):
        raise CliError(E_NOTFOUND, f"--contours {con}: {need.name} not in this area",
                       "--contours best|dem|none, or rebuild the area at a level with it")
    note = {"best": best_note(best) if readable(best) else "",
            "lidar": "QLD LiDAR 10 m (none drawn where not flown).", "dem": "APPROX from the terrain DEM (+/-10 m).",
            "none": "", "ticked": ""}[con]
    return con, iv, best, note


def best_srcs(best):
    """The contour sources actually in contours-best.gpkg ({'lidar', 'lidar-dem', 'nsw-5m', 'nsw', 'approx'} subset)."""
    # A file we can't read fails the sheet: labelling it "approx" would print lines from a broken file as if fine.
    ds = gdal_open(best, vector=True, hint="rebuild it: lmap area build <area> --yes (contours-best)")
    try:
        L = ds.GetLayerByName("contours")
        if L is None:
            raise CliError(E_FAIL, f"{Path(best).name}: no contours layer", "rebuild it: lmap area build <area> --yes")
        if L.GetLayerDefn().GetFieldIndex("src") < 0:
            raise CliError(E_FAIL, f"{Path(best).name}: no src field", "rebuild it: lmap area build <area> --yes")
        r = ds.ExecuteSQL("SELECT DISTINCT src FROM contours")
        if r is None:
            raise CliError(E_FAIL, f"{Path(best).name}: contour sources unreadable", "rebuild it: lmap area build <area> --yes")
        try:
            out = {f.GetField(0) for f in r}
        finally:
            ds.ReleaseResultSet(r)
        return {x for x in out if x}
    finally:
        ds = None


def best_note(best):
    """Footer contour note for --contours best, naming only what this area's lines came from."""
    s = best_srcs(best); parts = []
    if s & {"lidar", "lidar-dem"}:
        parts.append("LiDAR where flown")
    if "nsw-5m" in s:
        parts.append(("elsewhere " if parts else "") + "NSW Govt 5 m elevation model")
    if "nsw" in s:
        parts.append(("elsewhere " if parts else "") + "NSW Govt topographic mapping (not LiDAR)")
    if "approx" in s:
        parts.append(("elsewhere " if parts else "") + "APPROX from the terrain DEM (+/-10 m)")
    return (", ".join(parts) + ".") if parts else ""


PHOTO_STEEP_PCT = 15     # mean slope over the sheet above this -> contours 20 m (photo and topo) (a steep
                         # range at 26% swamped at 10 m; boxes at 8-9% read fine at 10 m)


def sheet_interval(d, crs, cx, cy, W, H, scale):
    """Contour interval for a sheet (topo or photo) from how steep the ground in its frame is (dem.tif, 30 m)."""
    dem = d / "dem.tif"
    if not readable(dem):
        return None
    try:
        from osgeo import gdal
        import numpy as np
        gdal.UseExceptions()
        hw, hh = W / 2000 * scale, H / 2000 * scale
        ds = gdal.Warp("", str(dem), format="MEM", dstSRS=crs, xRes=30, yRes=30,
                       outputBounds=(cx - hw, cy - hh, cx + hw, cy + hh), resampleAlg="bilinear")
        z = ds.ReadAsArray().astype("float64"); nd = ds.GetRasterBand(1).GetNoDataValue()
        if nd is not None:
            z[z == nd] = np.nan
        z[z < -1000] = np.nan
        gy, gx = np.gradient(z, 30)
        pct = float(np.nanmean(np.hypot(gx, gy)) * 100)
    except Exception:
        return None
    if pct != pct:
        return None
    return {"interval": 20 if pct > PHOTO_STEEP_PCT else 10, "mean_slope_pct": round(pct, 1)}


def cmd_area_print(ctx):
    """One printable sheet of an area: PDF (GeoPDF by default) at a fixed scale."""
    need_root(ctx); contour_widths(ctx.a)          # bad width: usage error before any work
    return print_sheet(ctx, sheet_dir(ctx, ctx.a.name))


def data_dates(ctx, meta):
    """'.  OSM data 2026-10-12.  QLD roads 2026-09-30' for the footer: when each vector source this sheet draws was
    last fetched (its file time in the library), so a reader knows how old the roads and creeks are."""
    want = {"osm": "OSM", "qld-roads": "QLD roads", "qld-surface-water": "QLD creeks", "nsw-water": "NSW creeks"}
    out = []
    for i in meta.get("items", []):
        k, src = i.get("key"), i.get("from")
        if k not in want or not src or src not in ctx.sources:
            continue
        p = ctx.path(ctx.sources[src].get("out") or ctx.sources[src].get("dest", ""))
        try:
            t = max((f.stat().st_mtime for f in ([p] if p.is_file() else p.glob("*.gpkg") if p.is_dir() else [])),
                    default=None)
        except OSError:
            t = None
        if t:
            out.append((want[k], time.strftime('%Y-%m-%d', time.localtime(t))))
    by = {}                                        # same date -> one entry: "OSM, QLD roads, QLD creeks 2026-10-20"
    for nm, dt in out:
        by.setdefault(dt, []).append(nm)
    return "".join(f".  {', '.join(n)} {dt}" for dt, n in sorted(by.items())) if by else ""


def print_sheet(ctx, d):
    """Print one sheet from the built area/map folder d (area.json + <name>.qgz; name = ctx.a.name)."""
    meta_p = d / "area.json"; qgz = d / f"{ctx.a.name}.qgz"
    if not readable(meta_p) or not readable(qgz):
        raise CliError(E_NOTFOUND, f"area {ctx.a.name} not built", f"lmap area create {ctx.a.name} ..."
                       if "areas" in d.relative_to(ctx.root).parts[:1] else "lmap map ... --yes")
    if getattr(ctx.a, "dpi", None) is None:       # --dpi > photo: LMAP_PHOTO_DPI > [build] photo_dpi > 1200 (topo: 300)
        _pd = 300
        if getattr(ctx.a, "style", None) == "photo":
            try:
                _pd = int(os.environ.get("LMAP_PHOTO_DPI") or read_config().get("photo_dpi") or 1200)
            except (CliError, TypeError, ValueError):
                raise CliError(E_USAGE, "config [build] photo_dpi must be a whole number", "e.g. [build] photo_dpi = 1200")
            if not 50 <= _pd <= 4800:
                raise CliError(E_USAGE, "photo_dpi must be 50-4800", "e.g. [build] photo_dpi = 1200")
        ctx.a.dpi = _pd
    meta = json.loads(meta_p.read_text()); bbox = meta["bbox"]; crs = meta["crs"]
    W, H = PAPER[ctx.a.paper]
    if ctx.a.portrait:
        W, H = H, W
    if ctx.a.center:
        lat, lon = [float(v) for v in ctx.a.center.split(",")]
    else:
        lat, lon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
    from osgeo import osr
    osr.UseExceptions()
    wgs, mga = osr.SpatialReference(), osr.SpatialReference(); wgs.ImportFromEPSG(4326); mga.SetFromUserInput(crs)
    for x in (wgs, mga):
        x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(wgs, mga); cx, cy, _ = tr.TransformPoint(lon, lat)
    # grid convergence: bearing of true north measured on the grid
    nx, ny, _ = tr.TransformPoint(lon, lat + 0.01)
    import math
    conv = -math.degrees(math.atan2(nx - cx, ny - cy))      # + = grid north east of true north
    yr = time.localtime().tm_year + time.localtime().tm_yday / 365.25
    try:
        decl = wmm_declination(lat, lon, yr, WMM_COF)
        wname, wstart, wend = wmm_model()
        ta, tb = max(wstart, yr - 0.5), min(wend - 1e-3, yr + 0.5)   # a year-wide window inside the model's span
        if tb - ta < 0.5:                                            # (a model is 5 years: always >= 0.5 yr)
            ta, tb = wstart, wend - 1e-3
        decl1 = decl + (wmm_declination(lat, lon, tb, WMM_COF) - wmm_declination(lat, lon, ta, WMM_COF)) / (tb - ta)
    except ModelExpired as e:                      # a sheet with a wrong declination is worse than no sheet
        raise CliError(E_FAIL, f"can't print declination: {e}",
                       "put NOAA's current WMM .COF in share/ (https://www.ncei.noaa.gov/products/world-magnetic-model)")
    mil = lambda deg: deg * 6400 / 360
    zone = int(crs.split(":")[1]) - 7800
    band = MGRS_BANDS[int((lat + 80) // 8)]
    sq = mgrs_square(zone, cx, cy + 10_000_000 if cy < 0 else cy)
    # the sheet's ground box (paper x scale; a little larger than the map frame): grid-zone / band edges inside it
    # mean the printed grid labels are only right on one side of the edge
    gw, gh = W / 1000 * ctx.a.scale / 2, H / 1000 * ctx.a.scale / 2
    back = osr.CoordinateTransformation(mga, wgs)
    ll = [back.TransformPoint(cx + dx, cy + dy)[:2] for dx in (-gw, gw) for dy in (-gh, gh)]
    lons, lats = [q[0] for q in ll], [q[1] for q in ll]
    zones = {int((x + 180) // 6) + 1 for x in (min(lons), max(lons))}
    bands = {MGRS_BANDS[int((y + 80) // 8)] for y in (min(lats), max(lats))}
    edge_warn = []
    if len(zones) > 1:
        edge_warn.append(f"sheet crosses UTM zones {'/'.join(map(str, sorted(zones)))}: the grid is zone {zone}; "
                         "coordinates beyond the zone edge are in the wrong zone's grid")
    if len(bands) > 1:
        edge_warn.append(f"sheet crosses MGRS bands {'/'.join(sorted(bands))}: the band letter printed ({band}) is "
                         "for the centre only")
    for w_ in edge_warn:
        ctx.log(f"WARNING: {w_}")
    label = ctx.a.label or f"{ctx.a.paper}-{ctx.a.scale // 1000}k"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", label):
        raise CliError(E_USAGE, f"bad --label {label!r}")
    out = d / f"print-{label}.pdf"
    iv_auto = None
    if ctx.a.interval is None and ctx.a.scale >= 50000:   # 1:50k and smaller: 20 m (GA 1:50k standard)
        ctx.a.interval = 20
    elif ctx.a.interval is None:                   # larger scales: steep ground (>15% mean slope) 20 m, else 10 m
        iv_auto = (sheet_interval(d, crs, cx, cy, W, H, ctx.a.scale)
                   if ctx.a.contours in ("best", "dem", "lidar") else None)
        ctx.a.interval = iv_auto["interval"] if iv_auto else 10
    con, iv, best, note = area_contours(d, ctx.a.contours, ctx.a.interval)
    imagery = imagery_text(meta.get("items", []), d)
    _hw = (W - 16) / 2000 * ctx.a.scale; _hh = (H - 52) / 2000 * ctx.a.scale       # sheet half size, m (generous)
    _dl = _hw / (111320 * math.cos(math.radians(lat))); _dp = _hh / 110540
    acc = acc_line(ctx, meta, d, con, ctx.a.scale, (lon - _dl, lat - _dp, lon + _dl, lat + _dp), ctx.a.style)
    dem = next((i.get("from") for i in meta.get("items", []) if i.get("key") == "dem"), "?")
    dem += data_dates(ctx, meta)          # "Roads/OSM data as of ..." rides the existing DATA line (no template change)
    args = {"qgz": str(qgz), "paper": [W, H], "frame": page_frame(W, H), "scale": ctx.a.scale, "center_xy": [cx, cy],
            "title": ctx.a.title or f"{ctx.a.name}  -  MGRS {zone}{band} {sq}", "zone": zone, "band": band, "sq": sq,
            "decl_deg": decl, "decl_mil": mil(decl), "decl_dir": "east" if decl >= 0 else "west",
            "decl_rate_mil": mil(decl1 - decl), "conv_mil": mil(conv), "gma_mil": mil(decl - conv), "year": yr, "wmm": wname,
            "imagery": imagery, "dem": dem, "made": time.strftime("%Y-%m-%d"), "ver": VERSION,
            "legend": not ctx.a.no_legend, "mstyle": map_style(), "pcon": getattr(ctx.a, "photo_contours", None) or PHOTO_CONTOURS, "overlay": (lambda v: "none" if v in (None, "auto") else v)(getattr(ctx.a, "overlay", None)), "grid": not getattr(ctx.a, "no_grid", False), "dpi": ctx.a.dpi, "geopdf": not ctx.a.plain_pdf, "out": "",
            "contours": con, "contour_note": note, "acc": acc, "best": str(best), "interval": iv, "style": ctx.a.style,
            "incomplete": getattr(ctx.a, "incomplete", None), "cw": contour_widths(ctx.a),
            "ci_text": {"best": f"CONTOUR INTERVAL {iv} m (index {iv * 5} m)", "lidar": "CONTOUR INTERVAL 10 m (index 50 m)",
                        "dem": "CONTOUR INTERVAL 10 m (index 50 m)", "none": "NO CONTOURS",
                        "ticked": "CONTOURS AS TICKED IN THE PROJECT"}[con]}
    if ctx.a.style == "topo":
        args["imagery"] = "none (topo style; --style photo for imagery)"
    # units: sheets; one per paper + style (the render is one QGIS run with no progress of its own: before it ends
    # the ETA counts down from past runs of the same paper + style)
    eta = WorkEta([(f"print-{ctx.a.style}-{ctx.a.paper}", 1, f"print {ctx.a.style} {ctx.a.paper}: drawing")],
                  run=getattr(ctx, "eta_run", None))
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "area.print", "out": str(out), "eta_s": eta.left(),
                **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in args.items() if k not in ("qgz", "out")}}
    sc = ctx.scratch() / f"print-{ctx.a.name}-{label}.{os.getpid()}.pdf"; sc.unlink(missing_ok=True); args["out"] = str(sc)
    try:
        eta.phase(0, t0=eta.t_start)                  # whole command: set-up + render + check + install
        with MemTurn(ctx):
            r = subprocess.run(preexec_fn=_pdeath, args=[sys.executable, "-c", PRINT_PY, json.dumps(args)], capture_output=True, text=True,
                               timeout=1800, env={**{k: v for k, v in os.environ.items() if k != "GDAL_NUM_THREADS"},
                                                  "QT_QPA_PLATFORM": "offscreen"})   # set, it breaks GeoPDF vectors
        line = next((l for l in r.stdout.splitlines() if l.startswith("{")), None)
        if r.returncode or not line or json.loads(line)["result"] != 0 or not sc.is_file():
            why = "no result line" if not line else (f"result={json.loads(line)['result']}" if json.loads(line)["result"] != 0
                                                      else "no sheet written") if not r.returncode else None
            child_fail(ctx, "print", r.returncode, [r.stderr or ("" if line else r.stdout) or ""], why)
        if json.loads(line).get("no_contours"):
            raise CliError(E_NOTFOUND, f"--contours {con}: the project has no such contour layer (sheet not written)",
                           f"lmap area create {ctx.a.name} ... --yes (rebuilds the project), or --contours ticked")
        gfix = None; gerr = False
        if args["geopdf"] and args["grid"]:
            try:
                gfix = pdf_georef_fix(sc)
            except Exception as e:                     # GDAL/OSR error: georef unchecked -> refused below
                gerr = f"{type(e).__name__}: {e}"
        if gfix and gfix[1] is None:
            raise CliError(E_FAIL, f"GeoPDF grid is drawn {gfix[0]} m from whole km values: not a snapping error, "
                                   "something is wrong with the sheet (not written)", "report it with the command used")
        if gfix and gfix[1] > 0.5:
            raise CliError(E_FAIL, f"GeoPDF georeference is {gfix[1]} m off its own grid after correction (sheet not written)")
        if gfix is None and args["geopdf"] and args["grid"]:   # a GPS position nobody checked is not shipped
            raise CliError(E_FAIL, "GeoPDF georeference could not be checked against the sheet's own grid "
                                   f"({gerr or 'no km grid line each way in the PDF, or not one plain /GPTS'}): sheet not written",
                           "--plain-pdf prints it without a GPS position; report it with the command used")
        out.with_suffix(".json").unlink(missing_ok=True)   # never leave an older sidecar beside a new PDF
        install_file(sc, out); eta.finish()
        try:                                           # what was drawn, for `lmap verify` (contour interval)
            atomic_write(out.with_suffix(".json"), json.dumps({"interval": iv, "contours": con, "style": ctx.a.style,
                                                              "scale": ctx.a.scale, "lmap": VERSION}))
        except OSError:
            pass
    finally:
        sc.unlink(missing_ok=True)
    return {"ok": True, "action": "area.print",
            **({"georef_vs_grid_m": {"before": gfix[0], "after": gfix[1]}} if gfix else {}), "file": str(out), "bytes": out.stat().st_size,
            "scale": f"1:{ctx.a.scale}", "paper": f"{ctx.a.paper} {'portrait' if ctx.a.portrait else 'landscape'}",
            "mgrs": f"{zone}{band} {sq}", "declination": f"{decl:+.1f} deg / {mil(decl):+.0f} mils",
            "gma_mils": round(mil(decl - conv)), "geopdf": not ctx.a.plain_pdf, "contours": con, "style": ctx.a.style,
            "interval": iv, **({"interval_auto": iv_auto} if iv_auto else {}),
            **({"warnings": edge_warn} if edge_warn else {})}

