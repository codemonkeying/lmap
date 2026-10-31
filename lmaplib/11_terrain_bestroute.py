# lmap: terrain_bestroute. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: bestroute (GRASS r.walk + r.path)
# r.walk (GRASS 8.4 manual): cost = Langmuir walking time  T = a*dS + b*dH_up + c*dH_moderate_down + d*dH_steep_down
# (defaults a,b,c,d = 0.72, 6.0, 1.9998, -1.9998; slope_factor -0.2125 = tan(-12 deg) splits moderate/steep down)
# + lambda * friction * dS, friction = extra seconds per metre. -k knight's move; r.path traces the outdir map back.
# Friction here (s/m on top of walking; walking itself ~0.72 s/m): bush 0.5 (~ Tobler's x0.6 off track),
# roads/tracks/paths 0, creeks 3, rivers/canals 30, lakes/reservoirs 200 (= go round unless a long way).
BW_FRIC = {"bush": 0.5, "road": 0.0, "creek": 3.0, "river": 30.0, "lake": 200.0}
# Langmuir charges climbing by height only, so r.walk happily goes straight up a 60 deg face. Extra s/m by slope
# (our estimate, matching the walk style of `terrain slope`: 20-30 scramble, 30-40 climb, >40 not walkable):
BW_SLOPE = ((20, 1.0), (30, 10.0), (40, 100.0))
BW_MAX_PX = 30_000_000
BW_STRIP = int(os.environ.get("LMAP_BW_STRIP", "1024"))   # slope-penalty rows per strip
BW_NOROAD = ("proposed", "construction", "motorway", "motorway_link", "trunk_link")


def bw_grid(ctx, pts, margin_m, res_req):
    """UTM window round all points + margin, at the finest sensible DEM res (>= 5 m; coarser if huge)."""
    import numpy as np
    lat0 = sum(p["pos"][0] for p in pts) / len(pts); lon0 = sum(p["pos"][1] for p in pts) / len(pts)
    zone = utm_zone(lon0); south = lat0 < 0; tr = _utm_tr(zone, south)
    xy = [tr.TransformPoint(p["pos"][1], p["pos"][0])[:2] for p in pts]
    cl = math.cos(math.radians(lat0))
    lons, lats = [p["pos"][1] for p in pts], [p["pos"][0] for p in pts]
    dem = ground = None
    for d in terrain_dems(ctx):
        g = d.res_m * (cl if d.srs.IsProjected() and (d.srs.GetAttrValue("PROJECTION") or "").startswith("Mercator") else 1)
        if not np.isnan(d.sample(lons, lats)).any():
            dem, ground = d, g; break
    if dem is None:
        raise CliError(E_NOTFOUND, "no DEM has data at every point", "points inside Australia, on land")
    xs, ys = [x for x, _ in xy], [y for _, y in xy]
    res = float(res_req) if res_req else max(5.0, float(round(ground)))
    while True:
        x0 = math.floor((min(xs) - margin_m) / res) * res; x1 = math.ceil((max(xs) + margin_m) / res) * res
        y0 = math.floor((min(ys) - margin_m) / res) * res; y1 = math.ceil((max(ys) + margin_m) / res) * res
        W, H = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
        if W * H <= BW_MAX_PX:
            break
        if res_req:
            raise CliError(E_USAGE, f"window {W}x{H} px at {res:g} m is too big", "coarser --res or closer points")
        res = float(math.ceil(res * math.sqrt(W * H / BW_MAX_PX)))
    return dict(dem=dem, ground=ground, res=res, zone=zone, south=south, epsg=(32700 if south else 32600) + zone,
                xy=xy, box=(x0, y0, x1, y1), W=W, H=H)


def bw_friction(ctx, g, path, roads, demf=None):
    """Float32 friction raster (s/m) on the grid from OSM roads/waterways (+ QLD lakes/reservoirs if present),
    plus a steep-slope penalty from the warped DEM (roads keep theirs: cuttings and switchbacks are walkable)."""
    from osgeo import gdal, ogr, osr
    import numpy as np
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    x0, y0, x1, y1 = g["box"]; res = g["res"]
    utm = osr.SpatialReference(); utm.ImportFromEPSG(g["epsg"]); utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    w4 = osr.SpatialReference(); w4.ImportFromEPSG(4326); w4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    inv = osr.CoordinateTransformation(utm, w4)
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2                  # corners + edge midpoints (edges curve in lon/lat)
    cs = np.array(inv.TransformPoints([(x0, y0), (x1, y0), (x0, y1), (x1, y1), (xm, y0), (xm, y1),
                                       (x0, ym), (x1, ym)]))[:, :2]
    bb = tuple(float(v) for v in (cs[:, 0].min() - 0.01, cs[:, 1].min() - 0.01, cs[:, 0].max() + 0.01, cs[:, 1].max() + 0.01))
    bbg = ogr.CreateGeometryFromWkt("POLYGON((%r %r,%r %r,%r %r,%r %r,%r %r))" % (
        bb[0], bb[1], bb[2], bb[1], bb[2], bb[3], bb[0], bb[3], bb[0], bb[1]))
    o = gdal.GetDriverByName("GTiff").Create(str(path), g["W"], g["H"], 1, gdal.GDT_Float32, ["TILED=YES", "COMPRESS=ZSTD"])
    o.SetGeoTransform((x0, res, 0, y1, 0, -res)); o.SetProjection(utm.ExportToWkt())
    o.GetRasterBand(1).Fill(BW_FRIC["bush"])
    mem = ogr.GetDriverByName("Memory").CreateDataSource("m")
    used = {}

    def burn(name, src, layer, where, val, buf, poly=False):
        if not src.is_file():
            return
        ds = ro_ogr(src)
        L = ds.GetLayerByName(layer)
        if L is None:
            return
        lsr = L.GetSpatialRef()
        lsr = lsr.Clone() if lsr is not None else w4.Clone()
        lsr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(lsr, utm)
        if lsr.IsGeographic():
            L.SetSpatialFilterRect(*bb)
        else:                                               # the filter is in the layer's own CRS
            fg = bbg.Clone(); fg.Transform(osr.CoordinateTransformation(w4, lsr)); L.SetSpatialFilter(fg)
        if where:
            L.SetAttributeFilter(where)
        M = mem.CreateLayer(name, utm, ogr.wkbUnknown); n = 0
        for f in L:
            ge = f.GetGeometryRef()
            if ge is None:
                continue
            ge = ge.Clone(); ge.Transform(tr)
            if buf:
                ge = ge.Buffer(buf, 2)
            nf = ogr.Feature(M.GetLayerDefn()); nf.SetGeometry(ge); M.CreateFeature(nf); n += 1
        if n:
            gdal.RasterizeLayer(o, [1], M, burn_values=[val], options=["ALL_TOUCHED=TRUE"] if not poly else [])
            used[name] = n
        ds = None
    osm = ctx.path(ctx.sources["osm-australia"]["dest"]) if "osm-australia" in ctx.sources \
        else ctx.path("data/australia/osm/osm-australia.gpkg")
    Q = ctx.path("data/australia/queensland")
    burn("creeks", osm, "waterways", "waterway NOT IN ('river','canal')", BW_FRIC["creek"], res / 2)   # 2 px: no knight hop
    burn("rivers", osm, "waterways", "waterway IN ('river','canal')", BW_FRIC["river"], res)   # 2+ px: no knight hop
    burn("lakes", Q / "qld-surface-water-state.gpkg", "lakes", None, BW_FRIC["lake"], 0, poly=True)
    burn("reservoirs", Q / "qld-surface-water-all-state.gpkg", "reservoirs", None, BW_FRIC["lake"], 0, poly=True)
    if roads != "ignore":
        val = BW_FRIC["road"] if roads == "use" else 5.0
        burn("roads", osm, "roads", "highway NOT IN (%s)" % ",".join(f"'{h}'" for h in BW_NOROAD), val, 0)
    if demf is not None:
        b = o.GetRasterBand(1)
        dz = gdal_open(demf); zb = dz.GetRasterBand(1)
        for r0 in range(0, g["H"], BW_STRIP):              # strips with a 1-row overlap for the gradient
            if ABORT.is_set():
                raise Aborted()
            a0, a1 = max(0, r0 - 1), min(g["H"], r0 + BW_STRIP + 1)
            z = zb.ReadAsArray(0, a0, g["W"], a1 - a0).astype("float32"); z[z == -32768] = np.nan
            if z.shape[0] < 2:
                break
            gy, gx = np.gradient(z, res)
            sl = np.degrees(np.arctan(np.hypot(gx, gy)))[r0 - a0:r0 - a0 + min(BW_STRIP, g["H"] - r0)]
            sl = np.nan_to_num(sl, nan=0.0)                # next to a hole: r.walk won't enter the NULL cell anyway
            f = b.ReadAsArray(0, r0, g["W"], sl.shape[0])
            add = np.zeros_like(f)
            for deg, v in BW_SLOPE:
                add[sl >= deg] = v
            road = f == BW_FRIC["road"] if roads == "use" else np.zeros(f.shape, bool)
            f = np.where(road, f, f + add).astype("float32")
            b.WriteArray(f, 0, r0)
        dz = None
        used["slope"] = "extra s/m at " + ", ".join(f">={d} deg {v:g}" for d, v in BW_SLOPE)
    o = None
    if not osm.is_file():
        ctx.log(f"note: {osm} missing: no roads/creeks in the friction (terrain only)")
    return used


BW_GRASS = r"""set -e
r.in.gdal -o input="$BW_DEM" output=dem --q
r.in.gdal -o input="$BW_FRIC" output=fric --q
g.region raster=dem
i=0
for leg in $BW_LEGS; do
  s=${leg%%;*}; e=${leg##*;}
  r.walk -k elevation=dem friction=fric output=cost$i outdir=dir$i start_coordinates=$s stop_coordinates=$e \
    walk_coeff=$BW_COEFF lambda=$BW_LAMBDA slope_factor=$BW_SF memory=$BW_MEM --q --overwrite
  r.path input=dir$i format=degree vector_path=path$i start_coordinates=$e --q --overwrite
  echo "COST $i $(r.what map=cost$i coordinates=$e separator=pipe null_value=NULL)"
  if [ $i -eq 0 ]; then u=""; else u="-u"; fi
  v.out.ogr $u input=path$i output="$BW_OUT" output_layer=leg$i format=GPKG --q --overwrite
  i=$((i+1))
done
"""


def bw_run(ctx, g, demf, fricf, outg, lam):
    """r.walk + r.path per leg in a throwaway GRASS project. -> [(seconds, [(E, N), ...]) per leg]"""
    from osgeo import ogr
    ogr.UseExceptions()
    legs = " ".join(f"{a[0]:.2f},{a[1]:.2f};{b[0]:.2f},{b[1]:.2f}" for a, b in zip(g["xy"][:-1], g["xy"][1:]))
    sc = ctx.scratch(); gh = sc / f"bw-home.{os.getpid()}"; gh.mkdir(exist_ok=True)   # HOME + TMPDIR: the
    script = gh / "bw.sh"; script.write_text(BW_GRASS)                                  # tmp project dies with it
    env = {**os.environ, "BW_DEM": str(demf), "BW_FRIC": str(fricf), "BW_OUT": str(outg), "BW_LEGS": legs,
           "BW_COEFF": "0.72,6.0,1.9998,-1.9998", "BW_LAMBDA": str(lam), "BW_SF": "-0.2125",
           "BW_MEM": "2000", "TMPDIR": str(gh), "HOME": str(gh), "GRASS_VERBOSE": "0"}
    try:
        r = _run(["grass", "--tmp-project", f"EPSG:{g['epsg']}", "--exec", "bash", script], ctx, env=env, timeout=1800)
    finally:
        shutil.rmtree(gh, ignore_errors=True)
    costs = {}
    for ln in (r or "").splitlines():
        m = re.match(r"COST (\d+) (.*)$", ln.strip())
        if m:
            f = m[2].split("|")
            costs[int(m[1])] = None if len(f) < 4 or f[3] in ("NULL", "*", "") else float(f[3])
    ds = ro_ogr(outg); out = []
    for i in range(len(g["xy"]) - 1):
        L = ds.GetLayerByName(f"leg{i}")
        segs = []
        if L is not None:
            for f in L:
                ge = f.GetGeometryRef()
                for k in range(ge.GetGeometryCount() or 1):
                    sub = ge.GetGeometryRef(k) if ge.GetGeometryCount() else ge
                    sg = [sub.GetPoint_2D(j) for j in range(sub.GetPointCount())]
                    if len(sg) >= 2:
                        segs.append(sg)
        c = costs.get(i)
        line = bw_join(segs, g["xy"][i], g["xy"][i + 1])
        if c is None or len(line) < 2:
            raise CliError(E_NOTFOUND, f"no way found for leg {i + 1}", "points on land, away from the window edge; "
                           "a bigger --margin-km if the way round is long")
        out.append((c, line))
    ds = None
    return out


def bw_join(segs, a, b):
    """r.path parts -> one line from a to b: chain by nearest endpoints, starting at the part nearest a."""
    if not segs:
        return []
    d2 = lambda p, q: (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
    segs = [list(s_) for s_ in segs]
    k = min(range(len(segs)), key=lambda i: min(d2(segs[i][0], a), d2(segs[i][-1], a)))
    cur = segs.pop(k)
    if d2(cur[-1], a) < d2(cur[0], a):
        cur.reverse()
    while segs:
        k = min(range(len(segs)), key=lambda i: min(d2(segs[i][0], cur[-1]), d2(segs[i][-1], cur[-1])))
        s_ = segs.pop(k)
        if d2(s_[-1], cur[-1]) < d2(s_[0], cur[-1]):
            s_.reverse()
        cur += s_[1:] if d2(s_[0], cur[-1]) < 1e-6 else s_
    if d2(cur[0], b) < d2(cur[0], a):
        cur.reverse()
    return cur



def cmd_terrain_bestroute(ctx):
    """Easiest walking line between points: GRASS r.walk over the DEM + friction (roads, creeks, rivers, lakes)."""
    need_root(ctx); a = ctx.a
    if bool(a.points) == bool(a.from_ or a.to or a.via):
        raise CliError(E_USAGE, "need --from POS --to POS [--via POS ...] or --points FILE (one of them)",
                       "lmap terrain bestroute --from '55K DU 79234 70557' --to '55K DU 76962 61149'")
    if a.points:
        if not Path(a.points).is_file():
            raise CliError(E_NOTFOUND, f"points file {a.points} not found")
        pts = read_route(a.points)
    else:
        if not (a.from_ and a.to):
            raise CliError(E_USAGE, "need both --from and --to")
        pts = [{"name": "Start", "pos": parse_pos(a.from_), "height": None}]
        pts += [{"name": f"Via{i + 1}", "pos": parse_pos(v), "height": None} for i, v in enumerate(a.via or [])]
        pts += [{"name": "End", "pos": parse_pos(a.to), "height": None}]
    if not 2 <= len(pts) <= 20:
        raise CliError(E_USAGE, f"{len(pts)} points: need 2-20",
                       "a GPS track is too many: give the points to pass through (a GPX route or waypoints)")
    for p in pts:
        p["name"] = p["name"] or to_mgrs(*p["pos"], digits=3)
    if a.margin_km is not None and not 0 <= a.margin_km <= 50:
        raise CliError(E_USAGE, f"--margin-km {a.margin_km}: 0-50")
    if a.res is not None and not 2 <= a.res <= 100:
        raise CliError(E_USAGE, f"--res {a.res}: 2-100 m")
    if not 0 <= a.friction_weight <= 10:
        raise CliError(E_USAGE, f"--friction-weight {a.friction_weight}: 0-10")
    lons = [p["pos"][1] for p in pts]
    if max(lons) - min(lons) > 6:
        raise CliError(E_USAGE, "points span more than 6 deg of longitude")
    span = max(_m_between(p["pos"], q["pos"]) for p in pts for q in pts)
    for k in range(len(pts) - 1):
        if _m_between(pts[k]["pos"], pts[k + 1]["pos"]) < 1:
            raise CliError(E_USAGE, f"{pts[k]['name']} and {pts[k + 1]['name']} are the same place", "drop one")
    if span > 150_000:
        raise CliError(E_USAGE, f"points {span / 1000:.0f} km apart; max 150", "split it into shorter legs")
    outs = [x for x in (a.png, a.gpkg) if x]
    if a.gpkg and not a.gpkg.endswith(".gpkg"):
        raise CliError(E_USAGE, "--gpkg must end in .gpkg")
    for x in outs:
        if not Path(x).resolve().parent.is_dir():
            raise CliError(E_NOTFOUND, f"folder for {x} does not exist")
    margin = a.margin_km * 1000 if a.margin_km is not None else max(1000.0, 0.25 * span)
    g = bw_grid(ctx, pts, max(margin, 5 * (a.res or 5)), a.res)
    margin = max(margin, 5 * g["res"])                       # never a point on the edge cell
    base = {"points": len(pts), "res_m": g["res"], "utm_epsg": g["epsg"], "size_px": [g["W"], g["H"]],
            "margin_km": round(margin / 1000, 2), "dem": g["dem"].path.name, "roads": a.roads,
            "friction_s_per_m": {**BW_FRIC, **({"road": 5.0} if a.roads == "avoid" else {}), "slope": {str(d_): v for d_, v in BW_SLOPE}},
            "friction_weight": a.friction_weight,
            "note": "least-effort line on bare-earth terrain + OSM roads/creeks/rivers and QLD lakes. Scrub, fences, "
                    "private land, cliffs under a DEM cell and flooding are NOT known: check it on the map/aerial"}
    if a.dry_run:
        return dict(ok=True, dry_run=True, action="terrain.bestroute", start=to_mgrs(*pts[0]["pos"]),
                    end=to_mgrs(*pts[-1]["pos"]), legs=len(pts) - 1,
                    writes=[{"file": x, "exists": Path(x).exists()} for x in outs], **base)
    for x in outs:
        if Path(x).exists() and not a.yes:
            raise CliError(E_REFUSED, f"refusing to overwrite {x} without --yes")
    need_tool("grass", "gdalwarp")
    install_signals()
    t0 = time.time(); sc = ctx.scratch(); tag = f"bw.{os.getpid()}"
    demf, fricf, outg = sc / f"{tag}.dem.tif", sc / f"{tag}.fric.tif", sc / f"{tag}.path.gpkg"
    x0, y0, x1, y1 = g["box"]; d = g["dem"]
    try:
        _run(["gdalwarp", "-q", "-overwrite", "-t_srs", f"EPSG:{g['epsg']}", "-te", x0, y0, x1, y1,
              "-tr", g["res"], g["res"], "-r", "bilinear", "-ot", "Float32",
              *(["-srcnodata", d.nd] if d.nd is not None else []), "-dstnodata", "-32768",
              "-co", "TILED=YES", "-co", "COMPRESS=ZSTD", "-multi", "-wo", "NUM_THREADS=ALL_CPUS", d.path, demf], ctx)
        used = bw_friction(ctx, g, fricf, a.roads, demf)
        if ABORT.is_set():
            raise Aborted()
        legs_raw = bw_run(ctx, g, demf, fricf, outg, a.friction_weight)
    finally:
        for f_ in (demf, fricf, outg):
            f_.unlink(missing_ok=True)
    inv = _utm_tr(g["zone"], g["south"], inverse=True)
    # the traced line as waypoints; ends snapped to the real points (r.path gives cell centres)
    route_pts, named = [], []
    for i, (c, line) in enumerate(legs_raw):
        ll = [(la, lo) for lo, la, *_ in inv.TransformPoints(line)]
        ll[0], ll[-1] = pts[i]["pos"], pts[i + 1]["pos"]
        if i:
            ll = ll[1:]
        named.append(len(route_pts))
        route_pts += [{"name": "", "pos": q, "height": None} for q in ll]
    named.append(len(route_pts) - 1)
    for k, i in enumerate(named):
        route_pts[i]["name"] = pts[k]["name"]
    dems = terrain_dems(ctx)
    p = route_profile(dems, route_pts)
    tot = route_stats(p["d"], p["z"], 3.0, True)
    straight = sum(_m_between(a_["pos"], b_["pos"]) for a_, b_ in zip(pts[:-1], pts[1:]))
    legs = []
    for k, (i0, i1) in enumerate(zip(named[:-1], named[1:])):
        s0, s1 = p["wp"][i0], p["wp"][i1]
        st = route_stats(p["d"][s0:s1 + 1], p["z"][s0:s1 + 1], 3.0, True)
        legs.append({"from": pts[k]["name"], "to": pts[k + 1]["name"], "from_mgrs": to_mgrs(*pts[k]["pos"]),
                     "to_mgrs": to_mgrs(*pts[k + 1]["pos"]), **st, "r_walk_h": round(legs_raw[k][0] / 3600, 2),
                     "straight_m": round(_m_between(pts[k]["pos"], pts[k + 1]["pos"])),
                     "to_ground_m": round(float(p["z"][s1]), 1), "end_km": round(float(p["d"][s1]) / 1000, 3)})
    res = dict(ok=True, action="terrain.bestroute", start=to_mgrs(*pts[0]["pos"]), end=to_mgrs(*pts[-1]["pos"]),
               **tot, straight_m=round(straight), r_walk_h=round(sum(c for c, _ in legs_raw) / 3600, 2),
               legs=legs, step_m=round(p["step"], 2), off_track=True, hysteresis_m=3.0,
               friction_from={k: v for k, v in used.items()},
               terrain={dems[k].path.name: round(100 * float((p["src"] == k).sum()) / len(p["src"]))
                        for k in sorted(set(p["src"].tolist()))},
               seconds=round(time.time() - t0, 1), **base)
    if a.png:
        route_png(a.png, p, legs, f"best route {res['distance_m'] / 1000:.2f} km  +{res['climb_m']} m / "
                  f"-{res['descent_m']} m  r.walk {_hm(res['r_walk_h'])}  Tobler off-track {_hm(res['tobler_h'])}")
        res["png"] = str(Path(a.png).resolve())
    if a.gpkg:
        route_gpkg(Path(a.gpkg), p, route_pts, named, legs); res["gpkg"] = str(Path(a.gpkg).resolve())
    return res
