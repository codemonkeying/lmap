# lmap: terrain_route. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: route (distance, climb, time)
# Naismith: 5 km/h + 1 h per 600 m of ascent (Scottish Mountaineering Club; the common modern form).
# Tobler hiking function: W = 6 exp(-3.5 |dh/dx + 0.05|) km/h on a track; x 3/5 off track (Tobler 1993).
# Climb is counted with a hysteresis (default 3 m): DEM/LiDAR noise of +-1 m over 10,000 samples would
# otherwise add hundreds of metres of fake climb.
ROUTE_MAX_PTS = 5000


def read_route(path):
    """Ordered waypoints: a text points file (as `terrain los`), GPX route/track/waypoints, or any OGR line/points."""
    f = Path(path)
    if f.suffix.lower() not in (".gpx", ".kml", ".kmz", ".gpkg", ".geojson", ".json", ".shp"):
        return read_points(f)
    from osgeo import ogr, osr
    ogr.UseExceptions(); osr.UseExceptions()
    ds = ro_ogr(f); g4 = osr.SpatialReference(); g4.ImportFromEPSG(4326)
    g4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    order = ["route_points", "track_points", "routes", "tracks"] if f.suffix.lower() == ".gpx" else None
    names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
    for ln in [n for n in (order or names) if n in names] + (["waypoints"] if order and "waypoints" in names else []):
        L = ds.GetLayerByName(ln); sr = L.GetSpatialRef(); tr = None
        if sr:
            sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); tr = osr.CoordinateTransformation(sr, g4)
        pts = []; seg0 = None
        for ft in L:
            if ln == "track_points":                       # one track segment: a gap is not a straight leg
                key = (ft.GetField("track_fid"), ft.GetField("track_seg_id"))
                seg0 = seg0 or key
                if key != seg0:
                    continue
            if ln in ("tracks",) and pts:
                break
            g = ft.GetGeometryRef()
            if not g:
                continue
            g = g.Clone()
            if tr:
                g.Transform(tr)
            gt = ogr.GT_Flatten(g.GetGeometryType())
            parts = [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())] if gt in (ogr.wkbMultiLineString,) else [g]
            if gt == ogr.wkbMultiLineString:
                parts = parts[:1]                          # first segment (as track_points)
            for pg in parts:
                if ogr.GT_Flatten(pg.GetGeometryType()) == ogr.wkbPoint:
                    ix = next((i for i in range(ft.GetFieldCount()) if ft.GetFieldDefnRef(i).GetName().lower() == "name"), -1)
                    pts.append({"name": str((ft.GetField(ix) if ix >= 0 else None) or ""), "pos": (pg.GetY(), pg.GetX())})
                elif ogr.GT_Flatten(pg.GetGeometryType()) == ogr.wkbLineString:
                    pts += [{"name": "", "pos": (pg.GetY(i), pg.GetX(i))} for i in range(pg.GetPointCount())]
        if len(pts) >= 2:
            for i, p in enumerate(pts):
                p["name"] = p["name"] or ("start" if i == 0 else "end" if i == len(pts) - 1 else "")
                p["height"] = None
            return pts
    raise CliError(E_USAGE, f"{f}: no line or 2+ points found", "a GPX route/track, a line layer, or a points file")


def route_profile(dems, pts, step=None):
    """Ground along the polyline (straight UTM lines between waypoints), sampled every `step` m."""
    import numpy as np
    lat0, lon0 = pts[0]["pos"]; zone = utm_zone(lon0); south = lat0 < 0
    tr, inv = _utm_tr(zone, south), _utm_tr(zone, south, inverse=True)
    xy = np.array([tr.TransformPoint(p["pos"][1], p["pos"][0])[:2] for p in pts])
    seg = np.hypot(*np.diff(xy, axis=0).T); D = float(seg.sum())
    if D < 1:
        raise CliError(E_USAGE, "the route has no length")
    if D > 1_000_000:
        raise CliError(E_USAGE, f"{D / 1000:.0f} km: too long (max 1000 km)")
    if step is None:
        fine = dems[0]; hit = fine.sample([p["pos"][1] for p in pts], [p["pos"][0] for p in pts])
        step = max(2.0, fine.res_m) if fine.res_m < 20 and not np.all(np.isnan(hit)) else 10.0
    step = max(step, D / 400_000)
    cum = np.concatenate([[0], np.cumsum(seg)])
    d = np.unique(np.round(np.concatenate([np.arange(0, D, step), cum]), 6))   # waypoints are always samples
    cum = np.round(cum, 6)
    E = np.interp(d, cum, xy[:, 0]); N = np.interp(d, cum, xy[:, 1])
    ll = np.array(inv.TransformPoints(list(zip(E, N))))[:, :2]
    z = np.full(len(d), np.nan); src = np.full(len(d), -1)
    for k, dm in enumerate(dems):
        m = np.isnan(z)
        if not m.any():
            break
        v = dm.sample(ll[m, 0], ll[m, 1]); z[np.nonzero(m)[0]] = v; src[np.nonzero(m)[0][~np.isnan(v)]] = k
    if np.isnan(z).any():
        raise CliError(E_NOTFOUND, f"no terrain data for {int(np.isnan(z).sum())} of {len(d)} points on the route",
                       "is it outside Australia / over the sea?")
    wp = np.searchsorted(d, cum)
    return {"d": d, "z": z, "lon": ll[:, 0], "lat": ll[:, 1], "src": src, "D": D, "step": step, "wp": wp,
            "zone": zone, "south": south, "xy": xy}


def climb_descent(z, hyst):
    """Total up / down, ignoring wiggles smaller than hyst metres."""
    up = dn = 0.0; ref = float(z[0])
    for v in z[1:]:
        v = float(v)
        if v - ref >= hyst:
            up += v - ref; ref = v
        elif ref - v >= hyst:
            dn += ref - v; ref = v
    last = float(z[-1]) - ref                       # the end is a real point: count what's left of the last move
    return up + max(last, 0.0), dn + max(-last, 0.0)


def route_stats(d, z, hyst, off_track):
    import numpy as np
    dd = np.diff(d); dz = np.diff(z)
    ground = float(np.hypot(dd, dz).sum())
    up, dn = climb_descent(z, hyst)
    flat = float(d[-1] - d[0])
    naismith_h = flat / 5000 + up / 600
    # Tobler on ~20 m stretches (a 2 m LiDAR step makes every rock a 40 deg slope)
    w = max(1, int(round(20 / max(float(np.median(dd)) if len(dd) else 20, 1e-6))))
    idx = np.unique(np.concatenate([np.arange(0, len(d), w), [len(d) - 1]]))
    D2, Z2 = d[idx], z[idx]; sd, sz = np.diff(D2), np.diff(Z2)
    gr = np.clip(sz / np.maximum(sd, 1e-9), -1, 1)       # Tobler has no ceiling: a 60 deg LiDAR cliff = hours per
    v = 6 * np.exp(-3.5 * np.abs(gr + 0.05)) * (0.6 if off_track else 1.0)   # 10 m. Capped at 45 deg; km/h
    tobler_h = float((np.hypot(sd, sz) / 1000 / v).sum())
    steep = np.degrees(np.arctan(np.abs(sz) / np.maximum(sd, 1e-9)))
    return {"distance_m": round(flat), "ground_m": round(ground), "climb_m": round(up), "descent_m": round(dn),
            "naismith_h": round(naismith_h, 2), "tobler_h": round(tobler_h, 2),
            "max_slope_deg": round(float(steep.max()), 1) if steep.size else 0.0,
            "over_30deg_m": round(float(sd[steep > 30].sum())),
            "low_m": round(float(z.min()), 1), "high_m": round(float(z.max()), 1)}


def _hm(h):
    m = int(round(h * 60)); return f"{m // 60}:{m % 60:02d}"


def route_png(path, p, legs, title):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    d = p["d"] / 1000; z = p["z"]
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=110)
    ax.fill_between(d, z.min() - 5, z, color="#c8b48c", lw=0); ax.plot(d, z, color="#6b5a3a", lw=0.8)
    for i in p["wp"]:
        ax.axvline(d[i], color="#555", lw=0.5, ls=":")
    if legs:                                           # the start is a waypoint too
        ax.annotate(legs[0]["from"], (0, z[0]), fontsize=7, xytext=(2, 6), textcoords="offset points", ha="left")
    for lg in legs:
        ax.annotate(lg["to"], (lg["end_km"], lg["to_ground_m"]), fontsize=7, xytext=(0, 6), textcoords="offset points",
                    ha="center")
    ax.set_xlabel("distance (km)"); ax.set_ylabel("height (m)"); ax.set_xlim(0, d[-1]); ax.grid(alpha=0.25)
    ax.set_title(title, fontsize=10)
    fig.text(0.01, 0.01, "times are estimates (Naismith / Tobler); heat, load, scrub and creeks are not in them",
             fontsize=7, color="#a00")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)


def cmd_terrain_route(ctx):
    """Distance over the ground, climb/descent and walking time along waypoints or a GPX track."""
    need_root(ctx); a = ctx.a
    if not 0 <= a.hysteresis <= 50:
        raise CliError(E_USAGE, f"--hysteresis {a.hysteresis}: 0-50 m")
    if bool(a.points) == bool(a.via):
        raise CliError(E_USAGE, "need --points FILE or --via POS --via POS ... (one of them)",
                       "lmap terrain route --via '55K DU 79234 70557' --via '55K DU 76962 61149'")
    if a.points:
        if not Path(a.points).is_file():
            raise CliError(E_NOTFOUND, f"route file {a.points} not found")
        pts = read_route(a.points)
    else:
        pts = [{"name": f"WP{i + 1}", "pos": parse_pos(v), "height": None} for i, v in enumerate(a.via)]
    if len(pts) < 2:
        raise CliError(E_USAGE, "a route needs at least 2 points")
    outs = [x for x in (a.png, a.gpkg) if x]
    if a.gpkg and not a.gpkg.endswith(".gpkg"):
        raise CliError(E_USAGE, "--gpkg must end in .gpkg")
    for x in outs:
        if not Path(x).resolve().parent.is_dir():
            raise CliError(E_NOTFOUND, f"folder for {x} does not exist")
    if len(pts) > 50:                                      # a GPS track: drop fixes closer than 10 m (1 Hz logs)
        keep = [pts[0]]
        for q in pts[1:-1]:
            if q["name"] or _m_between(keep[-1]["pos"], q["pos"]) >= 10:
                keep.append(q)
        pts = keep + [pts[-1]]
    named = [i for i, p in enumerate(pts) if p["name"]] if len(pts) > 50 else list(range(len(pts)))
    if named[0] != 0:
        named = [0] + named
    if named[-1] != len(pts) - 1:
        named.append(len(pts) - 1)
    base = {"points": len(pts), "hysteresis_m": a.hysteresis, "off_track": a.off_track,
            "note": "estimates: Naismith 5 km/h + 1 h/600 m climb; Tobler hiking function"
                    + (" x0.6 off track" if a.off_track else " on a track (--off-track x0.6)")
                    + ". Heat, load, scrub, creeks and night are not in them. Leg climbs are counted per leg, so "
                      "they can add up to a few m more than the total"}
    if a.dry_run:
        return dict(ok=True, dry_run=True, action="terrain.route", start=to_mgrs(*pts[0]["pos"]),
                    end=to_mgrs(*pts[-1]["pos"]), legs=len(named) - 1,
                    writes=[{"file": x, "exists": Path(x).exists()} for x in outs], **base)
    if len(pts) > ROUTE_MAX_PTS:
        raise CliError(E_USAGE, f"{len(pts)} points after thinning to 10 m; max {ROUTE_MAX_PTS}", "split the track")
    for x in outs:
        if Path(x).exists() and not a.yes:
            raise CliError(E_REFUSED, f"refusing to overwrite {x} without --yes")
    dems = terrain_dems(ctx)
    p = route_profile(dems, pts)
    tot = route_stats(p["d"], p["z"], a.hysteresis, a.off_track)
    legs = []
    for i0, i1 in zip(named[:-1], named[1:]):
        s0, s1 = p["wp"][i0], p["wp"][i1]
        st = route_stats(p["d"][s0:s1 + 1], p["z"][s0:s1 + 1], a.hysteresis, a.off_track)
        (la0, lo0), (la1, lo1) = pts[i0]["pos"], pts[i1]["pos"]
        zl = utm_zone(lo0); tz = _utm_tr(zl, la0 < 0)
        (ea, na_), (eb, nb) = tz.TransformPoint(lo0, la0)[:2], tz.TransformPoint(lo1, la1)[:2]
        bp = {"zone": zl, "south": la0 < 0, "E": (ea, eb), "N": (na_, nb)}
        lg = {"from": pts[i0]["name"] or f"#{i0 + 1}", "to": pts[i1]["name"] or f"#{i1 + 1}",
              "from_mgrs": to_mgrs(*pts[i0]["pos"]), "to_mgrs": to_mgrs(*pts[i1]["pos"]), **st,
              "to_ground_m": round(float(p["z"][s1]), 1), "end_km": round(float(p["d"][s1]) / 1000, 3)}
        if math.hypot(eb - ea, nb - na_) >= 1:              # a repeated waypoint has no direction
            lg["bearing"] = _bearings(bp, *pts[i0]["pos"])
            if i1 - i0 > 1:
                lg["bearing"]["straight_line"] = True        # along a track: start -> end of the leg, not the path
        legs.append(lg)
    res = dict(ok=True, action="terrain.route", start=to_mgrs(*pts[0]["pos"]), end=to_mgrs(*pts[-1]["pos"]),
               **tot, legs=legs, step_m=round(p["step"], 2),
               terrain={dems[k].path.name: round(100 * float((p["src"] == k).sum()) / len(p["src"]))
                        for k in sorted(set(p["src"].tolist()))}, **base)
    if a.png:
        route_png(a.png, p, legs, f"{res['distance_m'] / 1000:.2f} km  +{res['climb_m']} m / -{res['descent_m']} m  "
                  f"Naismith {_hm(res['naismith_h'])}  Tobler {_hm(res['tobler_h'])}")
        res["png"] = str(Path(a.png).resolve())
    if a.gpkg:
        route_gpkg(Path(a.gpkg), p, pts, named, legs); res["gpkg"] = str(Path(a.gpkg).resolve())
    return res


def route_gpkg(path, p, pts, named, legs):
    from osgeo import ogr, osr
    ogr.UseExceptions(); osr.UseExceptions()
    tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp.gpkg"); tmp.unlink(missing_ok=True)
    try:
        _route_gpkg(tmp, p, pts, named, legs); replace_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True); raise


def _route_gpkg(tmp, p, pts, named, legs):
    from osgeo import ogr, osr
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(tmp)); sr = osr.SpatialReference(); sr.ImportFromEPSG(4326)
    L = ds.CreateLayer("legs", sr, ogr.wkbLineString25D)
    for nm, t in (("from", ogr.OFTString), ("to", ogr.OFTString), ("distance_m", ogr.OFTInteger),
                  ("climb_m", ogr.OFTInteger), ("descent_m", ogr.OFTInteger), ("naismith_h", ogr.OFTReal),
                  ("tobler_h", ogr.OFTReal), ("max_slope_deg", ogr.OFTReal)):
        L.CreateField(ogr.FieldDefn(nm, t))
    for (i0, i1), lg in zip(zip(named[:-1], named[1:]), legs):
        s0, s1 = p["wp"][i0], p["wp"][i1]; g = ogr.Geometry(ogr.wkbLineString25D)
        for j in range(s0, s1, max(1, (s1 - s0) // 2000)):
            g.AddPoint(float(p["lon"][j]), float(p["lat"][j]), float(p["z"][j]))
        g.AddPoint(float(p["lon"][s1]), float(p["lat"][s1]), float(p["z"][s1]))
        f = ogr.Feature(L.GetLayerDefn()); f.SetGeometry(g)
        for k in ("from", "to", "distance_m", "climb_m", "descent_m", "naismith_h", "tobler_h", "max_slope_deg"):
            f[k] = lg[k]
        L.CreateFeature(f)
    P = ds.CreateLayer("waypoints", sr, ogr.wkbPoint)
    P.CreateField(ogr.FieldDefn("name", ogr.OFTString)); P.CreateField(ogr.FieldDefn("mgrs", ogr.OFTString))
    for i in named:
        q = pts[i]; f = ogr.Feature(P.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(q["pos"][1], q["pos"][0])
        f.SetGeometry(g); f["name"] = q["name"] or f"#{i + 1}"; f["mgrs"] = to_mgrs(*q["pos"]); P.CreateFeature(f)
    del L, P, f
    ds = None
