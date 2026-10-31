# lmap: terrain_los. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- terrain: line of sight / intervisibility
# The paper method (profile along a pencil line, straight edge eye -> target) done on the DEM: ground sampled
# every 2 m (LiDAR) / 10 m (30 m DEM) along the GRID line in the observer's UTM zone, earth curvature +
# refraction applied as a drop d^2 (1-k) / 2R. LiDAR is bare earth; Copernicus 30/90 m is a SURFACE model (radar:
# partly includes tree canopy and buildings, smeared over 30-90 m). Neither knows the trees between you: treat
# "visible" as "terrain doesn't block it", and check on the ground.
EARTH_R = 6371000.0
TERRAIN_NOTE = ("terrain model only (LiDAR = bare earth; Copernicus 30/90 m = surface, partly includes trees and "
                "buildings, coarsely): vegetation and buildings can block what this calls visible - check on the ground")
REFRACTION = {"light": 0.143, "radio": 0.25}      # 1/7 (= GDAL/GRASS default, area viewshed); 4/3 earth for radio
MGRS_RE = re.compile(r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])([A-HJ-NP-V])\s*(\d+)\s*(\d*)\s*$", re.I)


def _utm_tr(zone, south, inverse=False):
    from osgeo import osr
    osr.UseExceptions()
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(4326); b.ImportFromEPSG((32700 if south else 32600) + zone)
    for x in (a, b):
        x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(b, a) if inverse else osr.CoordinateTransformation(a, b)


def utm_zone(lon):
    return min(60, max(1, int((lon + 180) // 6) + 1))


def to_mgrs(lat, lon, digits=5):
    """WGS84 lat/lon -> '55K DU 81084 70615' (digits per axis: 5 = 1 m)."""
    z = utm_zone(lon); e, n, _ = _utm_tr(z, lat < 0).TransformPoint(lon, lat)
    f = 10 ** (5 - digits)
    return (f"{z}{MGRS_BANDS[int((lat + 80) // 8)]} {mgrs_square(z, e, n)} "
            f"{int(e % 100000) // f:0{digits}d} {int(n % 100000) // f:0{digits}d}")


def parse_pos(text):
    """'55K DU 81084 70615' / '55KDU8108470615' / '55K DU 810 706' / '-19.258,146.82' -> (lat, lon)."""
    t = text.strip()
    m = MGRS_RE.match(t.upper())
    if m:
        zone, band, col, row, d1, d2 = int(m[1]), m[2].upper(), m[3].upper(), m[4].upper(), m[5], m[6]
        if not d2:
            if len(d1) % 2:
                raise CliError(E_USAGE, f"bad MGRS digits in {text!r}", "even number of digits: 55K DU 81084 70615")
            d1, d2 = d1[:len(d1) // 2], d1[len(d1) // 2:]
        if len(d1) != len(d2) or not 1 <= len(d1) <= 5 or not 1 <= zone <= 60:
            raise CliError(E_USAGE, f"bad MGRS {text!r}", "e.g. 55K DU 81084 70615 (1 m) or 55K DU 810 706 (100 m)")
        cols = MGRS_COLS[(zone - 1) % 3]
        if col not in cols:
            raise CliError(E_USAGE, f"bad MGRS square {col}{row} for zone {zone}")
        p = len(d1); f = 10 ** (5 - p)
        e = (cols.index(col) + 1) * 100000 + int(d1) * f + f / 2          # centre of the referenced cell
        nmod = ((MGRS_ROWS.index(row) - (5 if zone % 2 == 0 else 0)) % 20) * 100000 + int(d2) * f + f / 2
        bi = MGRS_BANDS.index(band); lat0 = -80 + 8 * bi; south = lat0 < 0
        lon0 = (zone - 1) * 6 - 180 + 3
        _, nmin, _ = _utm_tr(zone, south).TransformPoint(lon0, lat0)   # band's southern edge (bands never cross 0)
        n = nmod
        while n < nmin - 150000:                         # the 2000 km row cycle: pick the one in this band
            n += 2_000_000
        lon, lat, _ = _utm_tr(zone, south, inverse=True).TransformPoint(e, n)
        if abs(((lon - lon0 + 180) % 360) - 180) > 4:
            raise CliError(E_USAGE, f"MGRS {text!r} is not in zone {zone}")
        if not lat0 - 1 <= lat <= (84 if band == "X" else lat0 + 8) + 1:
            raise CliError(E_USAGE, f"MGRS {text!r} is not in latitude band {band}")
        return lat, lon
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", t)
    if m:
        lat, lon = float(m[1]), float(m[2])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    raise CliError(E_USAGE, f"can't read position {text!r}",
                   "MGRS '55K DU 81084 70615' (quote it) or lat,lon '-19.258,146.820'")


class _Dem:
    """Bilinear samples from one DEM (any CRS), read in small windows along the line."""
    def __init__(self, path):
        from osgeo import gdal, osr
        gdal.UseExceptions(); osr.UseExceptions()
        self.path = Path(path); self.ds = gdal_open(path); self.b = self.ds.GetRasterBand(1)
        self.gt = self.ds.GetGeoTransform(); self.nd = self.b.GetNoDataValue()
        self.w, self.h = self.ds.RasterXSize, self.ds.RasterYSize
        srs = osr.SpatialReference(); srs.ImportFromWkt(self.ds.GetProjection())
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); self.srs = srs
        g = osr.SpatialReference(); g.ImportFromEPSG(4326); g.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.tr = osr.CoordinateTransformation(g, srs)
        self.res_m = abs(self.gt[1]) * (111320 if srs.IsGeographic() else 1)

    def sample(self, lons, lats):
        import numpy as np
        pts = np.array(self.tr.TransformPoints(list(zip(lons, lats))))[:, :2]
        px = (pts[:, 0] - self.gt[0]) / self.gt[1] - 0.5; py = (pts[:, 1] - self.gt[3]) / self.gt[5] - 0.5
        out = np.full(len(px), np.nan)
        ok = (px >= -0.5) & (py >= -0.5) & (px < self.w - 0.5) & (py < self.h - 0.5)   # outer half pixel: clamp
        px = np.clip(px, 0, self.w - 1 - 1e-9); py = np.clip(py, 0, self.h - 1 - 1e-9)
        idx = np.nonzero(ok)[0]
        for c in range(0, len(idx), 256):
            i = idx[c:c + 256]
            x0, y0 = int(px[i].min()), int(py[i].min()); x1, y1 = int(px[i].max()) + 2, int(py[i].max()) + 2
            a = self.b.ReadAsArray(x0, y0, x1 - x0, y1 - y0).astype("float64")
            if self.nd is not None:
                a[a == self.nd] = np.nan
            fx, fy = px[i] - x0, py[i] - y0; ix, iy = fx.astype(int), fy.astype(int); dx, dy = fx - ix, fy - iy
            out[i] = (a[iy, ix] * (1 - dx) * (1 - dy) + a[iy, ix + 1] * dx * (1 - dy)
                      + a[iy + 1, ix] * (1 - dx) * dy + a[iy + 1, ix + 1] * dx * dy)
        return out


def terrain_dems(ctx):
    """Finest first: the 3D-view LiDAR patches (2 m, 10 m) then the 30 m all-Australia DEM."""
    A = ctx.path("data/australia")
    fs = sorted(A.glob("dem-3d-*m-3dview.cog.tif"), key=lambda p: float(re.search(r"-(\d+(?:\.\d+)?)m-", p.name)[1]))
    fs += [p for p in (A / "dem-3d-30m.cog.tif", A / "dem-30m.cog.tif", ctx.path("data/australia/queensland") / "dem-30m.cog.tif",
                       A / "dem-90m.cog.tif") if p.is_file()]
    if not fs:
        raise CliError(E_NOTFOUND, "no DEM found", "lmap au project --yes builds data/australia/dem-3d-*.cog.tif")
    return [_Dem(f) for f in fs]


def los_profile(dems, a, b, step=None, max_km=300):
    """Ground profile along the UTM grid line a -> b (each (lat, lon)). Returns dict of numpy arrays."""
    import numpy as np
    zone = utm_zone(a[1]); south = a[0] < 0
    tr, inv = _utm_tr(zone, south), _utm_tr(zone, south, inverse=True)
    ea, na, _ = tr.TransformPoint(a[1], a[0]); eb, nb, _ = tr.TransformPoint(b[1], b[0])
    D = math.hypot(eb - ea, nb - na)
    if D < 1:
        raise CliError(E_USAGE, "the two positions are the same")
    if D > max_km * 1000:
        raise CliError(E_USAGE, f"{D / 1000:.0f} km is too far for one line (max {max_km})")
    fine = dems[0]
    if step is None:                                    # finest DEM's cell where it touches the line (9 probes), else 10 m
        tt = np.linspace(0, 1, 9)
        hit = fine.sample(list(a[1] + (b[1] - a[1]) * tt), list(a[0] + (b[0] - a[0]) * tt))
        step = max(1.0, fine.res_m) if fine.res_m < 20 and not np.all(np.isnan(hit)) else 10.0
    n = max(2, int(math.ceil(D / step)) + 1)
    if n > 400_000:
        n = 400_000
    t = np.linspace(0, 1, n); E = ea + (eb - ea) * t; N = na + (nb - na) * t
    ll = np.array(inv.TransformPoints(list(zip(E, N))))[:, :2]
    z = np.full(n, np.nan); src = np.full(n, -1)
    for k, d in enumerate(dems):
        m = np.isnan(z)
        if not m.any():
            break
        v = d.sample(ll[m, 0], ll[m, 1]); z[np.nonzero(m)[0]] = v; src[np.nonzero(m)[0][~np.isnan(v)]] = k
    if np.isnan(z).any():
        raise CliError(E_NOTFOUND, f"no terrain data for {int(np.isnan(z).sum())} of {n} points on the line",
                       "is it outside Australia / over the sea?")
    return {"d": t * D, "z": z, "lon": ll[:, 0], "lat": ll[:, 1], "src": src, "D": D, "step": D / (n - 1),
            "zone": zone, "south": south, "E": (ea, eb), "N": (na, nb)}


def los_solve(p, eye_h, tgt_h, k, mhz=None):
    """Line-of-sight maths on a profile: visibility, blocking point, dead ground, Fresnel clearance."""
    import numpy as np
    d, D = p["d"], p["D"]
    ge = p["z"] - d * d * (1 - k) / (2 * EARTH_R)          # ground lowered by the earth's curve (seen from a)
    Ho = ge[0] + eye_h; Ht = ge[-1] + tgt_h
    line = Ho + (Ht - Ho) * d / D
    inner = slice(1, len(d) - 1)
    clr = (line - ge)[inner]
    res = {"visible": True, "clearance_m": None, "blocked_at_i": None, "target_seen_m": tgt_h}
    if clr.size:
        i = int(np.argmin(clr)) + 1
        need = Ho + (ge[inner] - Ho) * D / d[inner]           # target-top height needed to clear each point
        top_need = float(need.max())
        res.update(visible=bool(Ht >= top_need), clearance_m=float(clr.min()),
                   target_seen_m=float(min(tgt_h, max(0.0, Ht - top_need))))
        if not res["visible"]:
            res["blocked_at_i"] = int(np.argmax(need)) + 1
        res["worst_i"] = i
    # dead ground: a target of tgt_h standing at each point, seen from the eye?
    ang = (ge - Ho) / np.maximum(d, 1e-9); ang[0] = -np.inf
    horizon = np.maximum.accumulate(np.concatenate([[-np.inf], ang[:-1]]))
    seen = (ge + tgt_h - Ho) / np.maximum(d, 1e-9) >= horizon; seen[0] = True
    res["seen"] = seen; res["ge"] = ge; res["line"] = line
    if mhz:
        lam = 299.792458 / mhz
        r1 = np.sqrt(lam * d * (D - d) / D); res["r1"] = r1
        marg = (line - ge - 0.6 * r1)[inner]
        if marg.size:
            j = int(np.argmin(marg)) + 1
            res["fresnel"] = {"worst_i": j, "margin_m": float(marg.min()), "r1_m": float(r1[j]),
                              "clear_pct": float(100 * (line[j] - ge[j]) / r1[j]) if r1[j] > 0 else 100.0}
    return res


def _bearings(p, lat, lon):
    zone, south = p["zone"], p["south"]; tr = _utm_tr(zone, south)
    (ea, eb), (na, nb) = p["E"], p["N"]
    grid = math.degrees(math.atan2(eb - ea, nb - na)) % 360
    cx, cy, _ = tr.TransformPoint(lon, lat); nx, ny, _ = tr.TransformPoint(lon, lat + 0.01)
    conv = -math.degrees(math.atan2(nx - cx, ny - cy))
    yr = time.localtime().tm_year + time.localtime().tm_yday / 365.25
    try:
        decl = wmm_declination(lat, lon, yr, WMM_COF)
    except (OSError, ModelExpired) as e:                # no magnetic bearing beats a wrong one
        decl = None; ctx_note = str(e)
    else:
        ctx_note = None
    true = (grid + conv) % 360
    mil = lambda x: round(x * 6400 / 360) % 6400
    out = {"grid_deg": round(grid, 1), "grid_mils": mil(grid), "true_deg": round(true, 1)}
    if decl is not None:
        mag = (true - decl) % 360
        out.update(mag_deg=round(mag, 1), mag_mils=mil(mag), declination_deg=round(decl, 1))
    elif ctx_note:
        out["magnetic"] = f"not given: {ctx_note}"
    return out


def _dead_stretches(p, seen, min_len):
    out, i, n = [], 1, len(seen)
    while i < n:
        if not seen[i]:
            j = i
            while j + 1 < n and not seen[j + 1]:
                j += 1
            if p["d"][j] - p["d"][i] >= min_len:
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def los_one(ctx, dems, A, B, eye_h, tgt_h, k, mhz, detail=True):
    p = los_profile(dems, A["pos"], B["pos"])
    r = los_solve(p, eye_h, tgt_h, k, mhz)
    pt = lambda i: {"mgrs": to_mgrs(p["lat"][i], p["lon"][i]), "distance_m": round(float(p["d"][i])),
                    "ground_m": round(float(p["z"][i]), 1)}
    o = {"from": A["name"], "to": B["name"], "distance_m": round(p["D"]), "visible": r["visible"],
         "clearance_m": None if r["clearance_m"] is None else round(r["clearance_m"], 1),
         "target_seen_m": round(r["target_seen_m"], 2)}
    if r["blocked_at_i"] is not None:
        i = r["blocked_at_i"]
        o["blocked_by"] = dict(pt(i), above_sightline_m=round(float(r["ge"][i] - r["line"][i]), 1))
    if mhz and "fresnel" in r:
        f = r["fresnel"]
        o["radio"] = {"mhz": mhz, "verdict": "blocked" if not r["visible"] else ("clear" if f["margin_m"] >= 0 else "marginal"),
                      "fresnel_clear_pct_worst": round(f["clear_pct"]), "fresnel_r1_m_there": round(f["r1_m"], 1),
                      "short_of_60pct_m": round(max(0.0, -f["margin_m"]), 1), "worst_at": pt(f["worst_i"])}
    if detail:
        dead = _dead_stretches(p, r["seen"], 2.5 * p["step"])
        o["dead_ground"] = [{"from_m": round(float(p["d"][i])), "to_m": round(float(p["d"][j])),
                             "from_mgrs": to_mgrs(p["lat"][i], p["lon"][i]), "to_mgrs": to_mgrs(p["lat"][j], p["lon"][j])}
                            for i, j in dead[:60]]
        o["dead_ground_pct"] = round(100 * float((~r["seen"]).sum()) / len(r["seen"]), 1)
        o["bearing"] = _bearings(p, A["pos"][0], A["pos"][1])
        o["ends"] = {"from_ground_m": round(float(p["z"][0]), 1), "to_ground_m": round(float(p["z"][-1]), 1)}
        o["terrain"] = {dems[k].path.name: round(100 * float((p["src"] == k).sum()) / len(p["src"]))
                        for k in sorted(set(p["src"].tolist()))}
        o["step_m"] = round(p["step"], 2)
    return o, p, r


def los_png(path, p, r, o, eye_h, tgt_h, mhz):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt, numpy as np
    d = p["d"] / 1000; ge, line = r["ge"], r["line"]
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=110)
    ax.fill_between(d, ge.min() - 5, ge, color="#c8b48c", lw=0)
    ax.plot(d, ge, color="#6b5a3a", lw=0.8)
    dead = ~r["seen"]
    ax.fill_between(d, ge.min() - 5, ge, where=dead, color="#555", alpha=0.45, lw=0, label="dead ground (target not seen)")
    ax.plot(d, line, color="green" if r["visible"] else "red", lw=1.2,
            label=f"sight line ({eye_h:g} m eye -> {tgt_h:g} m target)")
    if mhz and "r1" in r:
        ax.fill_between(d, line - 0.6 * r["r1"], line + 0.6 * r["r1"], color="tab:blue", alpha=0.12, lw=0,
                        label=f"60% Fresnel zone {mhz:g} MHz")
    if r["blocked_at_i"] is not None:
        i = r["blocked_at_i"]; ax.plot(d[i], ge[i], "rv", ms=9, label="blocks the view")
    ax.plot([d[0]], [line[0]], "ko", ms=5); ax.plot([d[-1]], [line[-1]], "k^", ms=6)
    ax.set_xlabel("distance (km)"); ax.set_ylabel("height (m, curvature applied)")
    ax.set_xlim(0, d[-1]); ax.set_ylim(ge.min() - 5, max(ge.max(), line.max()) + (ge.max() - ge.min()) * 0.12 + 3)
    v = "VISIBLE" if r["visible"] else "NOT VISIBLE"
    ax.set_title(f"{o['from']} -> {o['to']}   {o['distance_m'] / 1000:.2f} km   {v}"
                 + (f"   radio {o['radio']['verdict']}" if o.get("radio") else ""), fontsize=10)
    ax.legend(fontsize=7, loc="best"); ax.grid(alpha=0.25)
    fig.text(0.01, 0.01, "terrain model only: vegetation and buildings can block what this shows as visible - check on the ground",
             fontsize=7, color="#a00")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)


def read_points(text_or_file):
    """Points: a file (CSV-ish 'name, position[, 5m]' per line; or .gpx/.kml/.gpkg/.geojson with names)."""
    f = Path(text_or_file)
    pts = []
    if f.suffix.lower() in (".gpx", ".kml", ".kmz", ".gpkg", ".geojson", ".json", ".shp"):
        from osgeo import ogr
        ogr.UseExceptions(); ds = ro_ogr(f)
        for li in range(ds.GetLayerCount()):
            L = ds.GetLayer(li)
            if f.suffix.lower() == ".gpx" and L.GetName() != "waypoints":   # not track/route fixes
                continue
            if L.GetGeomType() not in (ogr.wkbPoint, ogr.wkbPoint25D, ogr.wkbPointM, ogr.wkbPointZM, ogr.wkbUnknown):
                continue
            from osgeo import osr
            g4 = osr.SpatialReference(); g4.ImportFromEPSG(4326); g4.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            sr = L.GetSpatialRef(); tr = None
            if sr:
                sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER); tr = osr.CoordinateTransformation(sr, g4)
            for ft in L:
                g = ft.GetGeometryRef()
                if not g or ogr.GT_Flatten(g.GetGeometryType()) != ogr.wkbPoint:
                    continue
                x, y = g.GetX(), g.GetY()
                if tr:
                    x, y, _ = tr.TransformPoint(x, y)
                ix = next((i for i in range(ft.GetFieldCount()) if ft.GetFieldDefnRef(i).GetName().lower() == "name"), -1)
                nm = ft.GetField(ix) if ix >= 0 else None          # KML calls it "Name"
                pts.append({"name": str(nm or f"P{len(pts) + 1}"), "pos": (y, x), "height": None})
        return pts
    for n, line in enumerate(f.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split(",")]
        h = None
        if len(parts) > 2 and re.fullmatch(r"\d+(\.\d+)?\s*m", parts[-1]):
            h = float(parts[-1].rstrip("m ")); parts = parts[:-1]
            if h > 500:
                raise CliError(E_USAGE, f"{f}:{n}: height {h} m: metres above ground, 0-500")
        if len(parts) < 2:
            raise CliError(E_USAGE, f"{f}:{n}: need 'name, position'", "OP1, 55K DU 81084 70615, 2m")
        try:
            pos = parse_pos(",".join(parts[1:]))
        except CliError as e:
            raise CliError(E_USAGE, f"{f}:{n}: {e.msg}", "one point per line: name, position[, 5m] (no commas in names)")
        pts.append({"name": parts[0], "pos": pos, "height": h, "line": n})
    return pts


def los_gpkg(path, rows, pts):
    from osgeo import ogr, osr
    ogr.UseExceptions(); osr.UseExceptions()
    path = Path(path); tmp = path.with_name(f".{path.stem}.{os.getpid()}.tmp.gpkg"); tmp.unlink(missing_ok=True)
    drv = ogr.GetDriverByName("GPKG"); ds = drv.CreateDataSource(str(tmp))
    sr = osr.SpatialReference(); sr.ImportFromEPSG(4326)
    L = ds.CreateLayer("sightlines", sr, ogr.wkbLineString)
    for nm, t in (("from", ogr.OFTString), ("to", ogr.OFTString), ("visible", ogr.OFTInteger), ("distance_m", ogr.OFTInteger),
                  ("clearance_m", ogr.OFTReal), ("target_seen_m", ogr.OFTReal), ("blocked_mgrs", ogr.OFTString),
                  ("radio", ogr.OFTString)):
        L.CreateField(ogr.FieldDefn(nm, t))
    byname = {p["name"]: p for p in pts}
    for r in rows:
        if r.get("visible") is None:
            continue
        f = ogr.Feature(L.GetLayerDefn()); a, b = byname[r["from"]]["pos"], byname[r["to"]]["pos"]
        g = ogr.Geometry(ogr.wkbLineString); g.AddPoint_2D(a[1], a[0]); g.AddPoint_2D(b[1], b[0]); f.SetGeometry(g)
        f["from"], f["to"], f["visible"], f["distance_m"] = r["from"], r["to"], int(r["visible"]), r["distance_m"]
        if r.get("clearance_m") is not None:
            f["clearance_m"] = r["clearance_m"]
        f["target_seen_m"] = r["target_seen_m"]
        if r.get("blocked_by"):
            f["blocked_mgrs"] = r["blocked_by"]["mgrs"]
        if r.get("radio"):
            f["radio"] = r["radio"]["verdict"]
        L.CreateFeature(f)
    P = ds.CreateLayer("points", sr, ogr.wkbPoint)
    P.CreateField(ogr.FieldDefn("name", ogr.OFTString)); P.CreateField(ogr.FieldDefn("mgrs", ogr.OFTString))
    for p in pts:
        f = ogr.Feature(P.GetLayerDefn()); g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(p["pos"][1], p["pos"][0])
        f.SetGeometry(g); f["name"] = p["name"]; f["mgrs"] = to_mgrs(*p["pos"]); P.CreateFeature(f)
    del L, P, f
    ds = None                                            # closes + flushes
    replace_retry(tmp, path)


def cmd_terrain_los(ctx):
    """Line of sight: can A see B (and B see A) over the terrain? Pairs, blocking point, dead ground, radio."""
    need_root(ctx)
    a = ctx.a
    if a.radio is not None and not 1 <= a.radio <= 100000:
        raise CliError(E_USAGE, f"--radio {a.radio}: MHz between 1 and 100000", "UHF CB 477, 2 m ham 146, 70 cm 438")
    k = a.refraction if a.refraction is not None else REFRACTION["radio" if a.radio else "light"]
    if not -1 <= k <= 1:
        raise CliError(E_USAGE, f"--refraction {k}: between -1 and 1", "0.143 light (default), 0.25 radio, 0 none")
    for nm, v in (("--height", a.height), ("--target-height", a.target_height)):
        if not 0 <= v <= 500:
            raise CliError(E_USAGE, f"{nm} {v}: metres above ground, 0-500")
    if a.points:
        if a.from_ or a.to:
            raise CliError(E_USAGE, "use --points OR --from/--to, not both")
        if not Path(a.points).is_file():
            raise CliError(E_NOTFOUND, f"points file {a.points} not found")
        pts = read_points(a.points)
        if len(pts) < 2:
            raise CliError(E_USAGE, f"{a.points}: need at least 2 points")
        if len(pts) > 60:
            raise CliError(E_USAGE, f"{len(pts)} points = {len(pts) * (len(pts) - 1) // 2} lines; max 60 points")
        names = [p["name"] for p in pts]
        if len(set(names)) != len(names):
            raise CliError(E_USAGE, "point names must be unique")
    else:
        if not (a.from_ and a.to):
            raise CliError(E_USAGE, "need --from and --to (or --points FILE)",
                           "lmap terrain los --from '55K DU 81084 70615' --to '55K DU 83500 72100'")
        pts = [{"name": a.from_name or "A", "pos": parse_pos(a.from_), "height": None},
               {"name": a.to_name or "B", "pos": parse_pos(a.to), "height": None}]
    if a.png and a.points:
        raise CliError(E_USAGE, "--png is for one line (--from/--to); for --points use --gpkg")
    if a.gpkg and not a.gpkg.endswith(".gpkg"):
        raise CliError(E_USAGE, "--gpkg must end in .gpkg")
    outs = [x for x in (a.png, a.gpkg) if x]
    for x in outs:
        if Path(x).exists() and not a.yes:
            raise CliError(E_REFUSED, f"refusing to overwrite {x} without --yes")
        if not Path(x).resolve().parent.is_dir():
            raise CliError(E_NOTFOUND, f"folder for {x} does not exist")
    base = {"refraction_k": k, "eye_height_m": a.height, "target_height_m": a.target_height,
            "radio_mhz": a.radio, "note": TERRAIN_NOTE}
    if a.dry_run:
        return dict(ok=True, dry_run=True, action="terrain.los", points=[{"name": p["name"], "mgrs": to_mgrs(*p["pos"])}
                                                                       for p in pts], writes=outs, **base)
    dems = terrain_dems(ctx)
    hh = lambda p, d: p["height"] if p["height"] is not None else d
    if not a.points:
        A, B = pts
        o, p, r = los_one(ctx, dems, A, B, hh(A, a.height), hh(B, a.target_height), k, a.radio)
        o["from_mgrs"], o["to_mgrs"] = to_mgrs(*A["pos"]), to_mgrs(*B["pos"])
        if a.png:
            los_png(a.png, p, r, o, hh(A, a.height), hh(B, a.target_height), a.radio); o["png"] = str(Path(a.png).resolve())
        if a.gpkg:
            los_gpkg(Path(a.gpkg), [o], pts); o["gpkg"] = str(Path(a.gpkg).resolve())
        return dict(ok=True, action="terrain.los", **o, **base)
    rows = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            try:
                o, _, _ = los_one(ctx, dems, pts[i], pts[j], hh(pts[i], a.height), hh(pts[j], a.height), k, a.radio,
                                  detail=False)
            except CliError as e:                       # one bad pair (no terrain, too far) doesn't sink the rest
                o = {"from": pts[i]["name"], "to": pts[j]["name"], "visible": None, "error": e.msg}
            rows.append(o)
    res = dict(ok=True, action="terrain.los", mode="network", points=[{"name": p["name"], "mgrs": to_mgrs(*p["pos"]),
               "height_m": hh(p, a.height)} for p in pts], lines=rows,
               visible_pairs=sum(bool(r["visible"]) for r in rows), pairs=len(rows),
               failed_pairs=sum(r["visible"] is None for r in rows), **base)
    res.pop("target_height_m")
    res["note"] += "; network: each point is both eye and target at its own height (--height or ', 5m' in the file)"
    if a.gpkg:
        p = Path(a.gpkg)
        los_gpkg(p, rows, pts); res["gpkg"] = str(p.resolve())
    return res
