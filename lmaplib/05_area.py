# lmap: area. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- area
# One command: a box + a detail level -> <root>/areas/<name>/ with clipped data + a ready QGIS project.
AREA_LEVELS = {
    #            dem m  sat m  aerial  osm                      qld sources (QLD only)
    "overview": {"dem": 90, "sat": 10, "aerial": False, "osm": "major", "qld": []},
    "regional": {"dem": 30, "sat": 10, "aerial": False, "osm": "main",
                 "qld": ["qld-hazard-tsv", "qld-flood-tsv", "qld-fire-tsv", "qld-parks-tsv", "qld-contours-tsv"]},
    "local":    {"dem": 30, "sat": 10, "aerial": True,  "osm": "full",
                 "qld": ["qld-hazard-tsv", "qld-flood-tsv", "qld-fire-tsv", "qld-water-tsv", "qld-surface-water-tsv",
                         "qld-infra-tsv", "qld-parks-tsv", "qld-contours-tsv"]},
    "street":   {"dem": 30, "sat": 10, "aerial": True,  "osm": "full",
                 "qld": ["qld-hazard-tsv", "qld-flood-tsv", "qld-fire-tsv", "qld-water-tsv", "qld-surface-water-tsv",
                         "qld-infra-tsv", "qld-parks-tsv", "qld-roads-tsv", "qld-contours-tsv", "qld-cadastre-tsv"]},
}
QLD_BOX = (137.9, -29.2, 153.6, -9.0)
NSW_BOX = (140.9, -37.6, 153.7, -28.1)
# NSW official layers, clipped from the statewide files (lmap data fetch nsw-*-state): key -> (source, layers, per-layer
# SQL filter, renames). Contours: only the 10 m lines (the 2 m large-scale mapping has every 2 m), height -> elevation_m
NSW_CLIP = {
    "nsw-roads":    ("nsw-roads-state", ["roads", "fire_trails"], {}),
    "nsw-water":    ("nsw-water-state", ["watercourses", "water_areas"], {}),
    "nsw-contours": ("nsw-contours-state", ["contours"],
                     {"contours": "SELECT geom, objectid, classsubtype, elevation AS elevation_m, capturesourcecode, "
                                  "capturemethodcode, verticalaccuracy FROM contours WHERE abs(elevation - round(elevation / 10.0) * 10) < 0.05"}),
}
NSW_LEVEL = {"overview": [], "regional": ["nsw-contours"], "local": ["nsw-roads", "nsw-water", "nsw-contours"],
             "street": ["nsw-roads", "nsw-water", "nsw-contours"]}
# rough MB per km2, for the dry-run size estimate
AREA_MB_KM2 = {"overview": 0.03, "regional": 0.2, "local": 1.0, "street": 3.0}


def area_bbox(ctx):
    if ctx.a.bbox and ctx.a.center:
        raise CliError(E_USAGE, "give --bbox OR --center (+ --radius-km), not both")
    if ctx.a.bbox:
        return list(parse_bbox(ctx.a.bbox))
    if not ctx.a.center:
        raise CliError(E_USAGE, "need --center lat,lon (+ --radius-km) or --bbox w,s,e,n")
    import math
    try:
        la, lo = (float(x) for x in ctx.a.center.split(","))
    except ValueError:
        raise CliError(E_USAGE, f"--center wants lat,lon in degrees (e.g. -19.26,146.82), got {ctx.a.center!r}")
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise CliError(E_USAGE, f"--center out of range: {ctx.a.center} (lat first)")
    r = float(ctx.a.radius_km)
    if not 0 < r <= 500:
        raise CliError(E_USAGE, f"--radius-km must be >0 and <=500, got {r}")
    if abs(la) >= 85:
        raise CliError(E_USAGE, f"--center latitude {la} too close to a pole")
    dla = r / 110.574; dlo = r / (111.320 * math.cos(math.radians(la)))
    return [round(lo - dlo, 5), round(la - dla, 5), round(lo + dlo, 5), round(la + dla, 5)]


def overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def raster_ll_box(p):
    """lon/lat box of a raster (gdalinfo wgs84Extent)."""
    d = json.loads(subprocess.run(["gdalinfo", "-json", str(p)], capture_output=True, text=True).stdout or "{}")
    c = (d.get("wgs84Extent") or {}).get("coordinates", [[]])[0]
    if not c:
        return None
    xs, ys = [x for x, _ in c], [y for _, y in c]
    return [min(xs), min(ys), max(xs), max(ys)]


def best_raster(ctx, sid):
    """Prefer the source's .cog.tif (lmap pyramid create) over its .vrt."""
    s = ctx.sources.get(sid)
    if not s or not (s.get("vrt") or s.get("out")):
        return None
    if s.get("out") and ctx.path(s["out"]).is_file():
        return ctx.path(s["out"])
    if not s.get("vrt"):
        return None
    v = ctx.path(s["vrt"]); cog = v.with_name(v.stem + ".cog.tif")
    return cog if cog.is_file() else (v if v.is_file() else None)


# newest first: projects and areas use the first of these that is on disk (older ones stay as fallback)
NEWEST = {"aerial": ("aerial-tsv-latest", "aerial-tsv-2019"), "sat-qld": ("sat-qld-s2-2025", "sat-qld-s2")}
AERIAL_OTHER = ("aerial-nsw-bm",)        # other states' aerials: areas use the one that overlaps (QLD projects don't)
IMAGERY_LABEL = {"aerial-tsv-latest": "aerial 2019-22 (QLD Govt, 50 cm)", "aerial-tsv-2019": "aerial 2019 (TCC, 50 cm)",
                 "aerial-nsw-bm": "aerial (NSW Spatial Services, 50 cm)",
                 "sat-qld-s2-2025": "satellite 2025 (Sentinel-2 cloudless, 10 m)",
                 "au-s2-10m": "satellite 2025 (Sentinel-2 cloudless, 10 m)",
                 "sat-qld-s2": "satellite 2021 (Sentinel-2, 10 m)", "sat-au-30m": "satellite 2025 (Landsat, 30 m)"}
LAYER_NAME = {"sat-qld-s2-2025": "Satellite 2025 (Sentinel-2)", "sat-qld-s2": "Satellite 2021 (Sentinel-2)",
              "au-s2-10m": "Satellite 2025 (Sentinel-2 10m)",
              "sat-au-30m": "Satellite 2025 (Landsat 30m)", "aerial-tsv-latest": "Aerial 50cm (2019-22)",
              "aerial-tsv-2019": "Aerial 50cm (2019)", "aerial-nsw-bm": "Aerial 50cm (NSW)", "sat-qld-2m4": "QLD imagery 2.4m (State aerial + Planet)"}


def imagery_text(items, d=None):
    """'aerial 2019-22 (QLD Govt, 50 cm), satellite 2025 (...)' from an area's items (area.json `items`).
    Only layers whose file is in the area folder (when d is given); unknown sources named as-is."""
    out = []
    for k in ("aerial", "sat"):
        it = next((i for i in items if i.get("key") == k), None)
        if it and (d is None or (Path(d) / f"{k}.tif").is_file()):
            out.append(IMAGERY_LABEL.get(it.get("from"), f"{'aerial' if k == 'aerial' else 'satellite'} ({it.get('from')})"))
    return ", ".join(out) or "none"


def area_layer_names(d):
    """{file: layer name} for an area's QGIS project, from area.json (which source each file came from)."""
    try:
        items = json.loads((Path(d) / "area.json").read_text()).get("items", [])
    except (OSError, ValueError):
        return {}
    return {f"{i['key']}.tif": LAYER_NAME[i["from"]] for i in items if i.get("from") in LAYER_NAME}


def newest_raster(ctx, kind):
    """(source id, path) of the newest built raster of this kind, else (None, None)."""
    for sid in NEWEST[kind]:
        r = best_raster(ctx, sid)
        if r:
            return sid, r
    return None, None


def state_template(ctx, role, bbox):
    """The area template (sources.toml template=true, role=sat|topo) for this box: the state whose bbox holds
    the whole box, finest first; else the national fallback (au-s2-10m sat / ga-topo, which cover every box, so a
    box crossing a state border gets national not part-state). None if nothing (or its xml is missing)."""
    def cover(tb):                               # fraction of the area box inside the template's box
        if not tb:
            return 0
        w, h = min(bbox[2], tb[2]) - max(bbox[0], tb[0]), min(bbox[3], tb[3]) - max(bbox[1], tb[1])
        a = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        return max(0, w) * max(0, h) / a if a > 0 else 0
    def cover_union(bs, n=40):                  # several boxes: sampled union (overlaps not double-counted)
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return 0
        pts = [(bbox[0] + (i + .5) * (bbox[2] - bbox[0]) / n, bbox[1] + (j + .5) * (bbox[3] - bbox[1]) / n)
               for i in range(n) for j in range(n)]
        return sum(any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in bs) for x, y in pts) / len(pts)
    c = [(cover(t.get("bbox")) if t.get("bbox") or not t.get("bboxes") else cover_union(src_boxes(t)),
          t.get("max_res", 99), k, t) for k, t in ctx.sources.items()
         if t.get("template") and t.get("role") == role and t.get("xml") and ctx.path(t["xml"]).is_file()]
    full = [x for x in c if x[0] >= 0.999]       # a state that holds the whole box: finest wins
    if full:
        _, _, k, t = min(full, key=lambda x: x[1])
        return k, t
    part = [x for x in c if x[0] > 0]            # box crosses a border: most coverage, then finest
    if not part:
        return None
    _, _, k, t = max(part, key=lambda x: (x[0], -x[1]))
    return k, t


def area_plan(ctx, bbox, lvl):
    """Decide which inputs to use. Returns list of (key, kind, src, extra)."""
    L = AREA_LEVELS[lvl]; items = []
    # elevation: finest that fully covers the box, else 90m
    dem = None
    if L["dem"] == 30:
        for sid in DEM30:                              # national 30 m first; old state files only as fallback
            r = best_raster(ctx, sid)
            if r and (b := raster_ll_box(r)) and contains(b, bbox):
                dem = (sid, r, 30); break
    if not dem:
        r = best_raster(ctx, "dem-au-90m")
        if r:
            dem = ("dem-au-90m", r, 90)
    if dem:
        items.append(("dem", "dem", dem[1], {"res": dem[2], "from": dem[0]}))
    # satellite: offline statewide QLD file when it covers the box, else live tiles. 10m, but 40m for
    # boxes over ~500 km across (the area copy would be GBs)
    res = L["sat"] if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < 25 else 40
    sat_sid, r = newest_raster(ctx, "sat-qld")
    sat = ctx.sources.get("sat-tsv-s2")
    r2 = best_raster(ctx, "sat-qld-2m4")                 # optional sharper statewide copy (lmap data fetch sat-qld-2m4)
    img = Path(ctx.sources.get("sat-qld-2m4", {}).get("xml", "/nonexistent"))
    sres = getattr(ctx.a, "sat_res", None) or (2.4 if getattr(ctx.a, "sat_2m4", False) else None)
    if sres and lvl != "overview" and img.is_file() and overlaps(QLD_BOX, bbox):
        # --sat-res/--sat-2m4: QLD state imagery (aerial down to 10cm in towns) downloaded once for the area's
        # extent into areas/<name>/sat.tif on a network share, offline after
        items.append(("sat", "sat", img, {"res": sres, "from": f"qld-imagery {sres} m (downloaded for this area)"}))
    elif r2 and (b := raster_ll_box(r2)) and contains(b, bbox):
        items.append(("sat", "sat", r2, {"res": res if lvl == "overview" else 2.4, "from": "sat-qld-2m4"}))
    elif r and (b := raster_ll_box(r)) and contains(b, bbox):
        items.append(("sat", "sat", r, {"res": res, "from": sat_sid}))
    elif sat and ctx.path(sat["xml"]).is_file():
        items.append(("sat", "sat", ctx.path(sat["xml"]), {"res": res, "from": "sat-tsv-s2 (live)"}))
    elif (st := state_template(ctx, "sat", bbox)) and lvl != "overview":
        # outside QLD: that state's imagery, downloaded once for the area (default 2.4m; --sat-res finer)
        sid, t = st
        items.append(("sat", "sat", ctx.path(t["xml"]), {"res": max(sres or (2.4 if res < 40 else res), t.get("max_res", 0)),
                                                      "from": sid}))
    elif (r := best_raster(ctx, "sat-au-30m")) and (b := raster_ll_box(r)) and overlaps(b, bbox):
        items.append(("sat", "sat", r, {"res": max(res, 30), "from": "sat-au-30m"}))
    topo = ctx.sources.get("qld-topo")
    r = best_raster(ctx, "topo-qld-20m")                 # offline statewide copy: overview areas (live is finer)
    if lvl == "overview" and r and (b := raster_ll_box(r)) and overlaps(b, bbox):
        items.append(("topo", "topo", r, {"res": res, "from": "topo-qld-20m"}))
    if topo and lvl != "overview" and overlaps(QLD_BOX, bbox) and ctx.path(topo["xml"]).is_file():
        # QLD Government topographic basemap, saved into the area (no internet needed after)
        items.append(("topo", "topo", ctx.path(topo["xml"]), {"res": {"regional": 10, "local": 4, "street": 2.5}[lvl],
                                                              "from": "qld-topo"}))
    if not any(k == "topo" for k, *_ in items) and (st := state_template(ctx, "topo", bbox)):
        sid, t = st
        r0 = {"overview": 40, "regional": 10, "local": 4, "street": 2.5}[lvl]
        items.append(("topo", "topo", ctx.path(t["xml"]), {"res": max(r0, t.get("max_res", 0)), "from": sid}))
    if L["aerial"]:
        # newest aerial that overlaps the box (the latest one covers Paluma-Dalbeg; 2019 is Townsville only; NSW Blue Mountains)
        for sid in NEWEST["aerial"] + AERIAL_OTHER:
            r = best_raster(ctx, sid)
            if r and (b := raster_ll_box(r)) and overlaps(b, bbox):
                items.append(("aerial", "aerial", r, {"res": 0.5, "from": sid})); break
    if lvl != "overview":                                # tree/scrub tint for topo prints (ESA WorldCover 10 m, box only)
        items.append(("vegetation", "veg", None, {"res": 20 if lvl == "regional" else 10, "from": "esa-worldcover",
                                                  "sieve_px": VEG_SIEVE_PX}))
    if L["osm"] in ("major", "main"):
        g = ctx.path(ctx.sources["osm-australia"]["dest"]) if "osm-australia" in ctx.sources else None
        if g and g.is_file():
            items.append(("osm", "osm-gpkg", g, {"mode": L["osm"], "from": "osm-australia"}))
    else:
        pbf = ctx.path(ctx.sources["osm-australia"]["from"]) if "osm-australia" in ctx.sources else Path("/nonexistent")
        if pbf.is_file():
            items.append(("osm", "osm-pbf", pbf, {"mode": "full", "from": "osm-au"}))
    if dem:
        iv = 10 if dem[2] <= 30 else 20                  # 30m DEM -> 10m (index 50); 90m -> 20m (index 100)
        items.append(("contours-dem", "contours", dem[1], {"from": dem[0], "interval": iv}))
    if L["osm"] == "full":
        items.append(("field", "field", None, {"from": "osm (area clip)"}))
    items.append(("grid", "grid", None, {"from": "lmap (computed)"}))
    if dem:
        items.append(("spot-heights", "peaks", None, {"from": dem[0]}))
        items.append(("slope", "slope", None, {"from": dem[0]}))
        items.append(("dem3d", "dem3d", None, {"from": dem[0]}))
        if lvl != "overview":                           # 90m cells are too coarse to judge a landing site
            items.append(("hls", "hls", None, {"from": dem[0]}))
    if overlaps(QLD_BOX, bbox):
        for sid in L["qld"]:
            if sid not in ctx.sources:
                continue
            key = sid.replace("-tsv", "")
            # statewide offline copy (lmap qld fetch) covering every layer we need -> clip locally
            want = set(ctx.sources[sid]["layers"])
            parts = []                                  # (file, [layers]) from <key>-state / <key>-all-state
            for k in (key + "-state", key + "-all-state"):
                v = ctx.sources.get(k)
                if v and v.get("tier") and ctx.path(v["dest"]).is_file():
                    got = sorted(want & set(v["layers"]) - {l for _, ls in parts for l in ls})
                    if got:
                        parts.append((str(ctx.path(v["dest"])), got))
            have = {l for _, ls in parts for l in ls}
            cs = None if not overlaps(NSW_BOX, bbox) or state_holds(bbox, "queensland") else "queensland"
            if parts:
                items.append((key, "gpkg-clip", parts, {"from": key + "-state", "layers": sorted(have),
                                                        "missing": sorted(want - have), **({"clip_state": cs} if cs else {})}))
            else:
                items.append((key, "arcgis", sid, {"from": sid}))
    if overlaps(NSW_BOX, bbox) and state_touch(bbox, "new-south-wales"):
        cs = None if state_holds(bbox, "new-south-wales") else "new-south-wales"   # border box: cut to NSW itself
        for key in NSW_LEVEL[lvl]:
            sid, lays, sql = NSW_CLIP[key]; v = ctx.sources.get(sid)
            if v and ctx.path(v["dest"]).is_file():
                items.append((key, "gpkg-clip", [(str(ctx.path(v["dest"])), lays)],
                              {"from": sid, "layers": lays, "missing": [], "sql": sql, **({"clip_state": cs} if cs else {})}))
    v = ctx.sources.get("osm-water")                   # sea fill (topo): OSM water polygons, one merged shape per box
    if v and ctx.path(v["dest"]).is_file() and overlaps(v.get("bbox") or src_extent(v) or bbox, bbox):
        items.append(("sea", "gpkg-clip", [(str(ctx.path(v["dest"])), ["sea"])],
                      {"from": "osm-water", "layers": ["sea"], "missing": [], "sql": {"sea": SEA_SQL},
                       "dialect": "SQLITE", "box_sql": True}))
    n5 = ctx.sources.get("nsw-dem-5m")
    if n5 and lvl != "overview" and overlaps(NSW_BOX, bbox) and state_touch(bbox, "new-south-wales"):
        # NSW 5 m DEM -> one seamless contour set (NSW's mapped lines were captured per tile and never joined)
        items.append(("contours-nsw5", "contours-nsw5", n5["url"], {"from": "nsw-dem-5m"}))
    A_ = ctx.path("data/australia"); ld = A_ / "dem-3d-2m-3dview.cog.tif"; lc = A_ / "lidar" / "lidar-coverage.gpkg"
    if dem and lvl != "overview" and ld.is_file() and lc.is_file() and (b := raster_ll_box(ld)) and overlaps(b, bbox):
        # contours from the LiDAR DEM where QLD has no LiDAR contour lines (and outside QLD)
        items.append(("contours-lidar", "contours-lidar", ld, {"from": "dem-3d-2m-3dview (LiDAR)", "cov": str(lc)}))
    if lvl != "overview":                              # GA AUSTopo 1:250k (2025) overview layer. The old 1:100k/1:50k
        items.append(("sheet-250k", "sheet", "250k", {"from": "ga-sheets-250k"}))   # scans: `sheets get` only
    have = {k for k, *_ in items}
    if {"sat", "vegetation"} <= have:                   # photo sheets: water evened (no date-patch quilt)
        items.append(("sat-even", "sat-even", None, {"from": "sat + vegetation (water)"}))
    if "contours-dem" in have and have & {"qld-contours", "contours-lidar", "nsw-contours", "contours-nsw5"}:
        # one contour set for prints, every 10 m: QLD LiDAR lines, then LiDAR-DEM lines, then NSW Govt mapped lines,
        # then approx 30 m elsewhere
        items.append(("contours-best", "contours-best", None,
                      {"from": " + ".join(k for k in ("qld-contours", "contours-lidar", "contours-nsw5", "nsw-contours", "contours-dem") if k in have),
                       "provider": getattr(ctx.a, "contours_from", None) or "auto"}))     # in the signature: switch -> rebuild
    return items


PEAKS_PY = r"""
# Summits from the area DEM: highest cell of each block, kept only if it's the top of its
# neighbourhood (3x3 blocks) and stands >= 30 m above that neighbourhood's low point.
# rank 1 = the highest few (shown zoomed out), rank 2 = the rest (shown zoomed in).
import sys, json
from osgeo import gdal, osr
import numpy as np
gdal.UseExceptions()
ds = gdal.Open(sys.argv[1]); b = ds.GetRasterBand(1); a = b.ReadAsArray().astype('float64')
nd = b.GetNoDataValue(); a[a == nd] = np.nan if nd is not None else a[a == nd]
gt = ds.GetGeoTransform(); px = abs(gt[1])
blk = max(4, int(round(1000 / px)))                  # ~1 km blocks
H, W = a.shape; nby, nbx = -(-H // blk), -(-W // blk)
pad = np.full((nby * blk, nbx * blk), np.nan); pad[:H, :W] = a
v = pad.reshape(nby, blk, nbx, blk).transpose(0, 2, 1, 3).reshape(nby, nbx, -1)
with np.errstate(all='ignore'):
    bmax = np.nanmax(v, axis=2); bmin = np.nanmin(v, axis=2)
    arg = np.nanargmax(np.where(np.isnan(v), -1e9, v), axis=2)
pk = []
for by in range(nby):
    for bx in range(nbx):
        z = bmax[by, bx]
        if not np.isfinite(z) or z <= 5:
            continue
        sl = (slice(max(by - 1, 0), by + 2), slice(max(bx - 1, 0), bx + 2))
        with np.errstate(all='ignore'):
            if z < np.nanmax(bmax[sl]) or z - np.nanmin(bmin[sl]) < 30:
                continue
        iy, ix = divmod(int(arg[by, bx]), blk); y, x = by * blk + iy, bx * blk + ix
        pk.append((float(z), gt[0] + (x + .5) * gt[1], gt[3] + (y + .5) * gt[5]))
pk.sort(reverse=True)
top = max(3, len(pk) // 8)
feats = [{"type": "Feature", "properties": {"elevation_m": round(z), "rank": 1 if i < top else 2},
          "geometry": {"type": "Point", "coordinates": [x, y]}} for i, (z, x, y) in enumerate(pk)]
srs = osr.SpatialReference(wkt=ds.GetProjection()); srs.AutoIdentifyEPSG()
crs = f"urn:ogc:def:crs:EPSG::{srs.GetAuthorityCode(None)}"
json.dump({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": crs}}, "features": feats},
          open(sys.argv[2], "w"))
print(len(feats))
"""


# Helicopter landing site CANDIDATES from terrain only: patches where slope stays under 7 degrees (most
# utility helicopters' limit is 7-10) and that are at least ~90 m across (a single-ship site with approach
# room), best one per 1 km block. Terrain can't see trees, wires, rocks or water - every site must be checked on imagery + the ground.
HLS_PY = r"""
import sys, json
from osgeo import gdal, osr
import numpy as np
src, out = sys.argv[1:3]
ds = gdal.Open(src); s = ds.GetRasterBand(1).ReadAsArray().astype('float32'); nd = ds.GetRasterBand(1).GetNoDataValue()
gt = ds.GetGeoTransform(); px = abs(gt[1])
ok = (s < 7.0) & np.isfinite(s) & ((s != nd) if nd is not None else True)
need = max(2, int(round(90 / px)))                  # cells across a 90 m square (30m DEM: 3x3)
# a cell qualifies if the whole need x need window starting there is flat (summed-area table)
ii = np.pad(ok.astype('int32'), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
H, W = ok.shape
if H < need or W < need:
    json.dump({"type": "FeatureCollection", "features": []}, open(out, "w")); print(0); sys.exit()
win = ii[need:, need:] - ii[:-need, need:] - ii[need:, :-need] + ii[:-need, :-need]
full = win == need * need
# thin: one site per ~500 m block, the flattest (lowest mean slope) window in it
blk = max(need, int(round(1000 / px)))
sp = np.pad(np.where(ok, s, 0).astype('float64'), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
mean = (sp[need:, need:] - sp[:-need, need:] - sp[need:, :-need] + sp[:-need, :-need]) / (need * need)
srs = osr.SpatialReference(); srs.ImportFromWkt(ds.GetProjection()); srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326); wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
tr = osr.CoordinateTransformation(srs, wgs)
feats = []
for by in range(0, full.shape[0], blk):
    for bx in range(0, full.shape[1], blk):
        f = full[by:by + blk, bx:bx + blk]
        if not f.any():
            continue
        m = np.where(f, mean[by:by + blk, bx:bx + blk], np.inf)
        y, x = np.unravel_index(np.argmin(m), m.shape); y += by; x += bx
        # how far does the flat ground extend? grow the square while it stays flat (cap 300 m)
        size = need
        while y + size < H and x + size < W and size * px < 300:
            if ok[y:y + size + 1, x:x + size + 1].all():
                size += 1
            else:
                break
        cx = gt[0] + (x + size / 2) * gt[1]; cy = gt[3] + (y + size / 2) * gt[5]
        lon, lat, _ = tr.TransformPoint(cx, cy)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                      "properties": {"size_m": int(size * px), "mean_slope_deg": round(float(m[y - by, x - bx]), 1),
                                     "check": "terrain only: check trees, wires, obstacles, surface"}})
json.dump({"type": "FeatureCollection", "features": feats}, open(out, "w"))
print(len(feats))
"""


MGRS_COLS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")        # 100 km column letters, by zone % 3
MGRS_ROWS = "ABCDEFGHJKLMNPQRSTUV"                     # 100 km row letters (20, no I/O)
MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"                    # 8 deg latitude bands from 80S


def mgrs_square(zone, e, n):
    """100 km square ID for a UTM easting/northing (southern hemisphere northing incl. 10,000 km false)."""
    col = MGRS_COLS[(zone - 1) % 3][int(e // 100000) - 1]
    off = 5 if zone % 2 == 0 else 0                    # even zones: rows start at F
    row = MGRS_ROWS[(int(n // 100000) + off) % 20]
    return col + row


def mgrs_grid(bbox, crs, out, ctx):
    """MGRS 1 km grid for the box as a GeoPackage: lines (1 km, 10 km bold, 100 km), and label points
    for each 100 km square ('55K DU'). Built in WGS84 / UTM (MGRS is defined on WGS84; vs GDA2020
    the offset is ~1.7 m, under a line width at 1:25k). Lines are written in the area's MGA CRS."""
    import math
    w, so, e, n = bbox
    z = int((((w + e) / 2) + 180) // 6) + 1
    utm = f"EPSG:{32700 + z}"
    band = MGRS_BANDS[int((((so + n) / 2) + 80) // 8)]
    # bbox corners -> UTM (via gdaltransform, no Python GDAL needed)
    pts = "\n".join(f"{x} {y}" for x, y in ((w, so), (w, n), (e, so), (e, n)))
    r = subprocess.run(["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", utm, "-output_xy"],
                       input=pts, capture_output=True, text=True)
    xy = [tuple(map(float, l.split()[:2])) for l in r.stdout.strip().splitlines()]
    if len(xy) != 4:
        raise CliError(E_FAIL, f"grid: gdaltransform failed: {r.stderr.strip()}")
    x0 = math.floor(min(x for x, _ in xy) / 1000) * 1000; x1 = math.ceil(max(x for x, _ in xy) / 1000) * 1000
    y0 = math.floor(min(y for _, y in xy) / 1000) * 1000; y1 = math.ceil(max(y for _, y in xy) / 1000) * 1000
    feats = []
    def line(a, b, kind, val, axis):
        lbl = f"{int(val % 100000 // 1000):02d}"       # 2-digit grid number, as printed on the sheet margin
        feats.append({"type": "Feature", "properties": {"kind": kind, "axis": axis, "label": lbl},
                      "geometry": {"type": "LineString", "coordinates": [a, b]}})
    for x in range(int(x0), int(x1) + 1, 1000):
        k = "100km" if x % 100000 == 0 else ("10km" if x % 10000 == 0 else "1km")
        line([x, y0], [x, y1], k, x, "E")
    for y in range(int(y0), int(y1) + 1, 1000):
        k = "100km" if y % 100000 == 0 else ("10km" if y % 10000 == 0 else "1km")
        line([x0, y], [x1, y], k, y, "N")
    for sx in range(int(x0 // 100000) * 100000, int(x1) + 1, 100000):
        for sy in range(int(y0 // 100000) * 100000, int(y1) + 1, 100000):
            cx, cy = min(max(sx + 50000, x0 + 500), x1 - 500), min(max(sy + 50000, y0 + 500), y1 - 500)
            feats.append({"type": "Feature", "properties": {"kind": "square", "axis": "",
                          "label": f"{z}{band} {mgrs_square(z, sx + 1, sy + 1)}"},
                          "geometry": {"type": "Point", "coordinates": [cx, cy]}})
    gj = out.with_suffix(".geojson")
    gj.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    try:
        # densify (-segmentize 100) so straight UTM lines stay correct after reprojecting to MGA
        for i, (geom, nm) in enumerate((("LINESTRING", "grid_lines"), ("POINT", "grid_squares"))):
            run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + ["-s_srs", utm, "-t_srs", crs,
                 "-segmentize", "100", "-where", f"kind {'<>' if geom == 'LINESTRING' else '='} 'square'",
                 "-nln", nm, out, gj], ctx)
    finally:
        gj.unlink(missing_ok=True)


# National MGRS grid for australia.qgz: per UTM zone 49-56 (WGS84), 10 km cells over land + AU_GRID_SEA_KM of sea,
# each cell = its 1 km lines as 10 km pieces (a spatial index then reads only what's on screen), cut at the zone
# edges. Written in EPSG:3857 (the project's CRS). Bump AU_GRID_VER when the layout changes (forces a rebuild).
AU_GRID_SEA_KM = 20
AU_GRID_VER = "mgrs-au v4"
AU_GRID_ZONES = (49, 50, 51, 52, 53, 54, 55, 56)


def au_grid_key(dem_p):
    st = Path(dem_p).stat()
    return f"{AU_GRID_VER} sea{AU_GRID_SEA_KM}km {Path(dem_p).name} {st.st_size} {int(st.st_mtime)}"


def au_grid_landmask(dem_p):
    """(mask, geotransform) of land + AU_GRID_SEA_KM from a coarse read of the national DEM (0 / nodata = sea)."""
    from osgeo import gdal
    import numpy as np
    from scipy import ndimage
    gdal.UseExceptions()
    ds = gdal_open(dem_p); b = ds.GetRasterBand(1); gt = list(ds.GetGeoTransform()); nd = b.GetNoDataValue()
    f = max(1.0, ds.RasterXSize / 2000); bx, by = round(ds.RasterXSize / f), round(ds.RasterYSize / f)
    a = b.ReadAsArray(buf_xsize=bx, buf_ysize=by)       # uses an overview if there is one; never the full raster
    gt[1] *= ds.RasterXSize / bx; gt[5] *= ds.RasterYSize / by
    land = np.isfinite(a) & (a != 0)
    if nd is not None:
        land &= a != nd
    km = abs(gt[5]) * 111.32                            # px size in km (north-south)
    land = ndimage.binary_dilation(land, iterations=max(1, math.ceil(AU_GRID_SEA_KM / km)))
    b = None; ds = None
    return land, gt


def _lines_only(g):
    """The line parts of a GEOS result (an Intersection can give a GeometryCollection of lines + touch points)."""
    from osgeo import ogr
    if g is None or g.IsEmpty():
        return []
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbLineString:
        return [g]
    if t in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
        return [p for j in range(g.GetGeometryCount()) for p in _lines_only(g.GetGeometryRef(j))]
    return []


def au_grid_build(ctx, dem_p, out):
    """Build the national MGRS grid GeoPackage at out (in scratch). Returns feature counts."""
    from osgeo import ogr, osr
    import numpy as np
    mask, gt = au_grid_landmask(dem_p)
    def on_land(lon, lat):
        c = np.floor((np.asarray(lon) - gt[0]) / gt[1]).astype(int); r = np.floor((np.asarray(lat) - gt[3]) / gt[5]).astype(int)
        ok = (c >= 0) & (r >= 0) & (c < mask.shape[1]) & (r < mask.shape[0])
        return bool(np.any(mask[np.clip(r, 0, mask.shape[0] - 1), np.clip(c, 0, mask.shape[1] - 1)] & ok))
    srs = lambda e: (lambda s: (s.ImportFromEPSG(e), s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER), s)[2])(osr.SpatialReference())
    merc, ll = srs(3857), srs(4326)
    lat0, lat1 = AU_BOX[1] - 1, AU_BOX[3] + 1
    zones = [int(v) for v in os.environ.get("LMAP_GRID_ZONES", ",".join(map(str, AU_GRID_ZONES))).split(",")]  # tests: fewer
    n_lines = n_sq = 0
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out)); LS = {}; S = L = f = g = None; tx = False
    try:
        for nm_ in ("grid_1km", "grid_10km", "grid_100km"):  # by kind: zoomed out never reads the 1 km lines
            LS[nm_] = ds.CreateLayer(nm_, merc, ogr.wkbLineString, ["SPATIAL_INDEX=YES"])
            for n_, t_ in (("kind", ogr.OFTString), ("axis", ogr.OFTString), ("label", ogr.OFTString), ("zone", ogr.OFTInteger)):
                LS[nm_].CreateField(ogr.FieldDefn(n_, t_))
        tbl = {"1km": "grid_1km", "10km": "grid_10km", "100km": "grid_100km", "zone": "grid_100km"}
        S = ds.CreateLayer("grid_squares", merc, ogr.wkbPoint, ["SPATIAL_INDEX=YES"])
        S.CreateField(ogr.FieldDefn("label", ogr.OFTString))
        def put(geom, kind, axis, label, z):
            nonlocal f, L, n_lines
            L = LS[tbl[kind]]; f = ogr.Feature(L.GetLayerDefn()); f.SetGeometry(geom)
            f.SetField("kind", kind); f.SetField("axis", axis); f.SetField("label", label); f.SetField("zone", z)
            L.CreateFeature(f); f = None; n_lines += 1
        def meridian(lon):
            m_ = ogr.Geometry(ogr.wkbLineString)
            for i in range(201):
                m_.AddPoint_2D(lon, lat0 + (lat1 - lat0) * i / 200)
            m_.Transform(osr.CoordinateTransformation(ll, merc)); return m_
        for z in zones:
            if ABORT.is_set() or DRAIN.is_set():
                raise Aborted()
            lw, le = -180 + 6 * (z - 1), -180 + 6 * z
            utm = srs(32700 + z); to_m = osr.CoordinateTransformation(utm, merc)
            to_utm = osr.CoordinateTransformation(ll, utm); to_ll = osr.CoordinateTransformation(utm, ll)
            def lonlat_poly(a0, a1, b0, b1):             # lon a0..a1, lat b0..b1 as a densified polygon in UTM
                r_ = ogr.Geometry(ogr.wkbLinearRing)
                for x, y in ([(a0 + (a1 - a0) * i / 60, b0) for i in range(61)] + [(a1, b0 + (b1 - b0) * i / 200) for i in range(201)]
                             + [(a1 - (a1 - a0) * i / 60, b1) for i in range(61)] + [(a0, b1 - (b1 - b0) * i / 200) for i in range(201)]):
                    r_.AddPoint_2D(*to_utm.TransformPoint(x, y)[:2])
                p_ = ogr.Geometry(ogr.wkbPolygon); p_.AddGeometry(r_); p_.CloseRings(); return p_
            zp = lonlat_poly(lw, le, lat0, lat1)
            ex = zp.GetEnvelope(); X0, X1 = int(ex[0] // 10000) * 10000, int(-(-ex[1] // 10000)) * 10000
            Y0, Y1 = int(ex[2] // 10000) * 10000, int(-(-ex[3] // 10000)) * 10000
            built = set()                                 # pass 1: which 10 km cells get lines
            for cy in range(Y0, Y1, 10000):
                for cx in range(X0, X1, 10000):
                    c = ogr.CreateGeometryFromWkt(f"POLYGON(({cx} {cy},{cx + 10000} {cy},{cx + 10000} {cy + 10000},{cx} {cy + 10000},{cx} {cy}))")
                    if not zp.Intersects(c):
                        continue
                    cl = [to_ll.TransformPoint(cx + dx, cy + dy)[:2] for dx in (0, 5000, 10000) for dy in (0, 5000, 10000)]
                    if on_land([p[0] for p in cl], [p[1] for p in cl]):
                        built.add((cx, cy))
            ds.StartTransaction(); tx = True
            for cy in range(Y0, Y1, 10000):
                if ABORT.is_set() or DRAIN.is_set():
                    raise Aborted()
                for cx in range(X0, X1, 10000):
                    if (cx, cy) not in built:
                        continue
                    c = ogr.CreateGeometryFromWkt(f"POLYGON(({cx} {cy},{cx + 10000} {cy},{cx + 10000} {cy + 10000},{cx} {cy + 10000},{cx} {cy}))")
                    cut = not zp.Contains(c)
                    # lines at i = 0..9; the east / north closing line (i = 10) only where the next cell is not built
                    for axis, last in (("E", (cx + 10000, cy) not in built), ("N", (cx, cy + 10000) not in built)):
                        for i in range(11 if last else 10):
                            v = (cx if axis == "E" else cy) + i * 1000
                            a_, b_ = ((v, cy), (v, cy + 10000)) if axis == "E" else ((cx, v), (cx + 10000, v))
                            g = ogr.Geometry(ogr.wkbLineString)
                            for t in (0, 0.25, 0.5, 0.75, 1):
                                g.AddPoint_2D(a_[0] + (b_[0] - a_[0]) * t, a_[1] + (b_[1] - a_[1]) * t)
                            parts = _lines_only(g.Intersection(zp)) if cut else [g]
                            k = "100km" if v % 100000 == 0 else ("10km" if v % 10000 == 0 else "1km")
                            for part in parts:
                                part = part.Clone(); part.FlattenTo2D(); part.Transform(to_m)
                                put(part, k, axis, f"{v % 100000 // 1000:02d}", z)
                            g = None
            # 100 km square IDs ('55K DU'): one label per part of the square in each 8-degree band (the GZD changes)
            bands = [(b0, b0 + 8) for b0 in range(-80, 84, 8) if b0 + 8 > lat0 and b0 < lat1]
            bpolys = [(MGRS_BANDS[(b0 + 80) // 8], lonlat_poly(lw, le, max(b0, lat0), min(b1, lat1))) for b0, b1 in bands]
            for sy in range(Y0 // 100000 * 100000, Y1, 100000):
                for sx in range(X0 // 100000 * 100000, X1, 100000):
                    sq = ogr.CreateGeometryFromWkt(f"POLYGON(({sx} {sy},{sx + 100000} {sy},{sx + 100000} {sy + 100000},{sx} {sy + 100000},{sx} {sy}))")
                    for band, bp in bpolys:
                        part = sq.Intersection(bp)
                        if part is None or part.IsEmpty() or part.GetArea() < 1e8:
                            continue
                        pt = part.PointOnSurface(); lon, lat = to_ll.TransformPoint(pt.GetX(), pt.GetY())[:2]
                        if not on_land([lon], [lat]):
                            continue
                        f = ogr.Feature(S.GetLayerDefn()); g = pt.Clone(); g.Transform(to_m); f.SetGeometry(g)
                        f.SetField("label", f"{z}{band} {mgrs_square(z, sx + 1, sy + 1)}")
                        S.CreateFeature(f); f = None; g = None; n_sq += 1
            # zone edges (the meridians where the numbers jump): only between two built zones, never a lone line out at sea
            if z - 1 in zones:
                put(meridian(lw), "zone", "", f"zone {z - 1} | {z}", z)
            ds.CommitTransaction(); tx = False
            ctx.log(f"  zone {z}: {n_lines} lines, {n_sq} squares so far")
        ds.SetMetadataItem("lmap_key", au_grid_key(dem_p))
    finally:
        if tx:
            ds.RollbackTransaction()
        f = None; g = None; L = None; S = None; LS.clear(); ds = None      # layers before the dataset
    return {"lines": n_lines, "squares": n_sq}


def cmd_au_grid(ctx):
    """data/australia/mgrs-grid.gpkg: the MGRS grid for australia.qgz (1 km / 10 km / 100 km lines, 2-digit numbers,
    square IDs, zone edges) over land + 20 km of sea. One-off (~3 min); `lmap au project --yes` adds it."""
    need_root(ctx)
    A = ctx.path("data/australia"); out = A / "mgrs-grid.gpkg"
    dem = best_raster(ctx, "dem-au-90m")
    if not dem:
        raise CliError(E_NOTFOUND, "no Australia 90m elevation (used for the land mask)", "lmap data fetch dem-au-90m --yes")
    have = gpkg_meta(out, "lmap_key") if out.is_file() else ""
    build = have != au_grid_key(dem) or bool(ctx.a.refresh)
    plan = {"action": "au.grid", "file": str(out), "exists": out.is_file(), "build": build,
            "zones": "49-56", "sea_km": AU_GRID_SEA_KM, "land_from": str(dem)}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not build:
        return {"ok": True, **plan, "unchanged": True, "hint": "already built; --refresh rebuilds"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to build the grid without --yes", "preview: lmap au grid --dry-run")
    install_signals(); t0 = time.time()
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx):
        part = ctx.scratch() / f"mgrs-grid.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        try:
            n = au_grid_build(ctx, dem, part)
            install_file(part, out)
        except Aborted:
            raise CliError(E_ABORTED, "stopped; old grid (if any) kept")
        finally:
            for x in part.parent.glob(part.name + "*"):
                x.unlink(missing_ok=True)
    return {"ok": True, **plan, **n, "mb": round(out.stat().st_size / 1e6), "seconds": round(time.time() - t0),
            "next": "lmap au project --yes  (adds the 'MGRS grid' group to australia.qgz, off by default)"}


AU_CONT_VER = "contours-au v3"
AU_CONT_IV = 20                  # metres; every 100 m is the index (heavier, labelled) - 1:50k sheet spacing
AU_CONT_SIMPLIFY = 0.00004       # degrees (~4 m): 1/4 of a smoothed cell, invisible at 1:25k, halves the file
AU_CONT_MINLEN = 0.0006          # degrees (~60 m): 30 m pits/bumps


def au_contours_key(dem_p):
    st = Path(dem_p).stat()
    return f"{AU_CONT_VER} i{AU_CONT_IV} s{AU_CONT_SIMPLIFY} m{AU_CONT_MINLEN} {Path(dem_p).name} {st.st_size} {int(st.st_mtime)}"


def au_contours_cells(dem_p):
    """1-degree cells (lon, lat of the SW corner) that hold land in the national DEM (coarse read)."""
    land, gt = au_grid_landmask(dem_p)                 # land + a 20 km rim: cells that are sea only are skipped
    import numpy as np
    from scipy import ndimage
    land = ndimage.binary_dilation(land, iterations=2)   # +~4 km: an islet averaged away at the coarse read
    out = set()
    ys, xs = np.nonzero(land)
    for y, x in zip(ys, xs):
        lon = gt[0] + (x + 0.5) * gt[1]; lat = gt[3] + (y + 0.5) * gt[5]
        out.add((math.floor(lon), math.floor(lat)))
    return sorted(out)


def au_contours_cell(ctx, dem_p, lon, lat, out):
    """One cell: smooth to half the DEM cell (15 m, cubic; every 2nd centre on a DEM sample), 20 m lines, drop sea/<=0 m and short bits,
    simplify. Written to out (a finished cell file is kept: a re-run resumes)."""
    sc = ctx.scratch(); tag = f"auc.{lon}.{lat}.{os.getpid()}"
    tif, raw, part = sc / f"{tag}.tif", sc / f"{tag}.raw.gpkg", sc / f"{tag}.gpkg"
    try:
        h = 1 / 7200 / 2                                # half a 15 m pixel: pixel centres fall ON the seam, so
        te = (lon - h, lat - h, lon + 1 + h, lat + 1 + h)  # neighbouring cells draw lines that end on the same points
        run(["gdalwarp", "-q", "-overwrite", "-te", *map(repr, te), "-ts", "7201", "7201",
             "-r", DEM_RESAMPLE, "-ot", "Float32", "-co", "TILED=YES", str(dem_p), tif], ctx)
        run(["gdal_contour", "-q", "-a", "elevation_m", "-i", str(AU_CONT_IV), "-f", "GPKG", "-nln", "contours",
             tif, raw], ctx)
        # gdal_contour runs lines out to the raster edge (half a pixel past the seam): cut back to the cell exactly,
        # so both cells end their lines on the same seam points (same grid + same source values in the overlap)
        run(["ogr2ogr", "-q", "-f", "GPKG", "-nln", "contours", "-nlt", "LINESTRING", "-explodecollections",
             "-clipsrc", str(lon), str(lat), str(lon + 1), str(lat + 1), "-simplify", str(AU_CONT_SIMPLIFY),
             "-dialect", "sqlite", "-sql",
             f"SELECT geom, CAST(round(elevation_m) AS INTEGER) AS elevation_m FROM contours "
             f"WHERE elevation_m > 0 AND ST_Length(geom) > {AU_CONT_MINLEN}", part, raw], ctx)
        replace_retry(part, out)
    finally:
        for x in (tif, raw, part):
            x.unlink(missing_ok=True)


def cmd_au_contours(ctx):
    """data/australia/contours-30m.gpkg: 20 m contours (index 100 m) for all of Australia from the national 30 m
    DEM, cell by cell in parallel (resumable), then one file. `lmap au project --yes` adds it (zoomed in only,
    under the QLD LiDAR contours). Area contours (10 m, LiDAR) stay the detail where you build them."""
    need_root(ctx)
    A = ctx.path("data/australia"); out = A / "contours-30m.gpkg"
    dem = best_raster(ctx, "dem-au-30m")
    if not dem:
        raise CliError(E_NOTFOUND, "no Australia 30m elevation", "lmap data fetch dem-au-30m --yes")
    key = au_contours_key(dem)
    have = gpkg_meta(out, "lmap_key") if out.is_file() else ""
    build = have != key or bool(ctx.a.refresh)
    cd = ctx.scratch() / "au-contours" / hashlib.sha1(key.encode()).hexdigest()[:10]   # cells of THIS key only
    only = os.environ.get("LMAP_AUCONT_CELLS")         # tests: "146,-20 147,-20"
    cells = [tuple(map(int, c.split(","))) for c in only.split()] if only else au_contours_cells(dem)
    done = [c for c in cells if (cd / f"{c[0]}_{c[1]}.gpkg").is_file()]
    plan = {"action": "au.contours", "file": str(out), "exists": out.is_file(), "build": build, "from": str(dem),
            "interval_m": AU_CONT_IV, "index_m": AU_CONT_IV * 5, "cells": len(cells), "cells_done": len(done),
            "est_gb": round(len(cells) * 0.022, 1)}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not build:
        return {"ok": True, **plan, "unchanged": True, "hint": "already built; --refresh rebuilds"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to build contours without --yes", "preview: lmap au contours --dry-run")
    need = (len(cells) - len(done)) * 30e6 + len(cells) * 22e6     # cells + the merged copy, a generous guess
    install_signals(); t0 = time.time()
    import concurrent.futures as cf
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), \
            Reservation(ctx, "AU contours", need, ctx.scratch(), hint=f"free space in {ctx.scratch()} or set [paths] tmp ="):
        for old in cd.parent.glob("*"):                 # cells of an older DEM/version: up to ~20 GB each
            if old != cd and old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        cd.mkdir(parents=True, exist_ok=True)
        todo = [c for c in cells if not (cd / f"{c[0]}_{c[1]}.gpkg").is_file()]
        random.shuffle(todo)
        prog = Progress({"id": "au-contours"}, len(cells), len(cells) - len(todo)); failed = []
        def one(c):
            err = None
            for k in range(3):                            # a cell failing once (OOM kill / network-share read error
                if DRAIN.is_set():                        # under load) is retried, not the whole run failed
                    return c, None
                try:
                    au_contours_cell(ctx, dem, c[0], c[1], cd / f"{c[0]}_{c[1]}.gpkg"); return c, None
                except Aborted as e:
                    return c, e
                except CliError as e:
                    err = e
                except Exception as e:                    # disk full etc.: a failed cell, not a hang
                    err = CliError(E_FAIL, f"{type(e).__name__}: {e}")
                if k < 2:
                    ctx.log(f"  cell {c}: {str(getattr(err, 'msg', None) or err)[-160:]}; retry {k + 1}/2")
                    if DRAIN.wait(RetryPolicy(3, 5, 30).delay(k)):     # `lmap stop` / abort end the wait
                        return c, None if not ABORT.is_set() else Aborted()
            return c, err
        with cf.ThreadPoolExecutor(max(1, ctx.shrink)) as ex:
            for i, (c, err) in enumerate(ex.map(one, todo), 1):
                say_stop(ctx)
                if err:
                    failed.append(c); prog.tick(False)
                    if not isinstance(err, Aborted):
                        ctx.log(f"[{i}/{len(todo)}] FAIL cell {c}: {getattr(err, 'msg', err)}")
                else:
                    prog.tick(True)
                    if i % 25 == 0 or i == len(todo):
                        ctx.log(f"  contours {len(cells) - len(todo) + i}/{len(cells)} cells, {round(time.time() - t0)} s")
        if DRAIN.is_set() or ABORT.is_set():
            prog.write(final="stopped")
            raise CliError(E_DRAINED if not ABORT.is_set() else E_ABORTED,
                           "stopped; finished cells are kept", "re-run the same command to resume")
        if failed:
            prog.write(final="failed")
            raise CliError(E_FAIL, f"{len(failed)} cell(s) failed: {failed[:5]}", "re-run the same command; finished cells are kept")
        ctx.log(f"merging {len(cells)} cells into {out.name}")
        part = ctx.scratch() / f"contours-30m.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        vrt = part.with_suffix(".vrt")
        try:
            from xml.sax.saxutils import escape
            vrt.write_text('<OGRVRTDataSource><OGRVRTUnionLayer name="contours">' + "".join(
                f'<OGRVRTLayer name="c{j}"><SrcDataSource>{escape(str(cd / f"{c[0]}_{c[1]}.gpkg"))}</SrcDataSource>'
                f'<SrcLayer>contours</SrcLayer></OGRVRTLayer>' for j, c in enumerate(cells))
                + '</OGRVRTUnionLayer></OGRVRTDataSource>')
            run(["ogr2ogr", "-q", "-f", "GPKG", "-nln", "contours", "-nlt", "LINESTRING", "-lco", "SPATIAL_INDEX=NO",
                 "-gt", "65536", part, vrt], ctx)             # one process for all cells (not 967 reopenings)
            if ABORT.is_set() or DRAIN.is_set():
                raise CliError(E_ABORTED if ABORT.is_set() else E_DRAINED, "stopped while merging; cells are kept",
                               "re-run to resume (the merge starts again)")
            run(["ogrinfo", "-q", "-sql", "SELECT CreateSpatialIndex('contours', 'geom')", part], ctx)
            run(["ogrinfo", "-q", "-sql", "CREATE INDEX contours_elev ON contours(elevation_m)", part], ctx)
            from osgeo import gdal
            d_ = gdal_open(part, update=True, vector=True); d_.SetMetadataItem("lmap_key", key); d_ = None
            install_file(part, out)
        finally:
            vrt.unlink(missing_ok=True)
            for x in part.parent.glob(part.name + "*"):
                x.unlink(missing_ok=True)
        prog.write(final="done")
        shutil.rmtree(cd, ignore_errors=True)
    return {"ok": True, **plan, "cells_done": len(cells), "gb": round(out.stat().st_size / 1e9, 2),
            "seconds": round(time.time() - t0),
            "next": "lmap au project --yes  (adds 'Contours 20m' to australia.qgz, zoomed in only)"}


def mga_epsg(lon):
    """GDA2020 MGA zone EPSG (7849-7856) for a longitude; Albers (9473) outside Australia's zones."""
    z = int((lon + 180) // 6) + 1
    return f"EPSG:{7800 + z}" if 49 <= z <= 56 else "EPSG:9473"


def readable(p):
    """is_file() lies on CIFS for files deleted while another client holds them open (delete-pending)."""
    try:
        with open(p, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


# Field features from the area's OSM clip (full OSM levels): one GeoPackage, one layer per kind, with a plain
# `kind` column so QGIS / KML / GPX can style and filter without hstore parsing.
OSM_COLS = {"man_made", "barrier", "highway"}   # own columns in GDAL's OSM points AND lines (osmconf), not in
#                                                 other_tags (waterway is a column on lines only: don't add it here)


def _t(k, v=None):
    if k in OSM_COLS:                              # 2026-10-16: was other_tags only -> wells, tanks, gates = 0
        return f"{k} = '{v}'" if v else f"{k} IS NOT NULL"
    # coalesce: other_tags is NULL on untagged features; NOT (NULL LIKE ..) is NULL, so plain tracks fell out
    return (f"coalesce(other_tags, '') LIKE '%\"{k}\"=>\"{v}\"%'" if v
            else f"coalesce(other_tags, '') LIKE '%\"{k}\"=>%'")


FIELD_POINTS = [   # (kind, SQL where on OSM points)
    ("drinking water", f"{_t('amenity', 'drinking_water')} OR {_t('drinking_water', 'yes')}"),
    ("water point", f"{_t('amenity', 'water_point')} OR {_t('man_made', 'water_tap')} OR {_t('waterway', 'water_point')}"),
    ("well / bore", f"{_t('man_made', 'water_well')} OR {_t('man_made', 'borehole')}"),
    ("spring", _t('natural', 'spring')),
    ("water tank / tower", f"{_t('man_made', 'water_tower')} OR {_t('man_made', 'storage_tank')} OR {_t('man_made', 'reservoir_covered')}"),
    ("waterfall", _t('waterway', 'waterfall')),
    ("shelter / hut", f"{_t('amenity', 'shelter')} OR {_t('tourism', 'alpine_hut')} OR {_t('tourism', 'wilderness_hut')} OR {_t('building', 'hut')}"),
    ("campsite", f"{_t('tourism', 'camp_site')} OR {_t('tourism', 'caravan_site')}"),
    ("toilets", _t('amenity', 'toilets')),
    ("ford", f"{_t('ford', 'yes')} OR {_t('highway', 'ford')}"),
    ("gate", f"{_t('barrier', 'gate')} OR {_t('barrier', 'lift_gate')} OR {_t('barrier', 'swing_gate')}"),
    ("cattle grid", _t('barrier', 'cattle_grid')),
    ("power tower", _t('power', 'tower')),           # steel transmission towers: few, landmarks (bold X)
    ("power pole", _t('power', 'pole')),             # street / rural poles: thousands (small light X)
    ("substation", _t('power', 'substation')),
    ("comms tower", f"{_t('man_made', 'mast')} OR {_t('man_made', 'communications_tower')} OR {_t('tower:type', 'communication')}"),
    ("helipad", f"{_t('aeroway', 'helipad')} OR {_t('aeroway', 'heliport')}"),
    ("airstrip", f"{_t('aeroway', 'aerodrome')} OR {_t('aeroway', 'airstrip')}"),
    ("fuel", _t('amenity', 'fuel')),
    ("hospital / clinic", f"{_t('amenity', 'hospital')} OR {_t('amenity', 'clinic')} OR {_t('amenity', 'doctors')}"),
    ("pharmacy", _t('amenity', 'pharmacy')),
    ("police", _t('amenity', 'police')),
    ("fire station", _t('amenity', 'fire_station')),
    ("lookout", f"{_t('tourism', 'viewpoint')} OR {_t('tower:type', 'observation')}"),
]
FIELD_SCHEMA = "2"   # field.gpkg kinds version: 2 = 'power pole' split from 'power tower'. Areas rebuild it
FIELD_LINES = [    # (kind, SQL where on OSM lines)
    ("power line", f"{_t('power', 'line')} OR {_t('power', 'minor_line')}"),
    ("4WD track", f"highway = 'track' AND ({_t('4wd_only', 'yes')} OR {_t('tracktype', 'grade4')} OR {_t('tracktype', 'grade5')})"),
    ("track", f"highway = 'track' AND NOT ({_t('4wd_only', 'yes')} OR {_t('tracktype', 'grade4')} OR {_t('tracktype', 'grade5')})"),
    ("ford (line)", f"{_t('ford', 'yes')}"),
    ("fence", f"{_t('barrier', 'fence')}"),
    ("pipeline", f"{_t('man_made', 'pipeline')}"),
]


def build_field(ctx, osm, out_part, skip=()):
    """field.gpkg from an area osm.gpkg or a raw .osm.pbf (full OSM: points/lines layers). skip: kinds left out
    (whole AU: towers + poles are 645k of 760k points). Returns {kind: count}."""
    counts = {}
    for i, (lay, src, rules, gtype) in enumerate((("field_points", "points", FIELD_POINTS, "POINT"),
                                                  ("field_lines", "lines", FIELD_LINES, "LINESTRING"))):
        rules = [(k, w) for k, w in rules if k not in skip]
        case = " ".join(f"WHEN {w} THEN '{k}'" for k, w in rules)
        anyw = " OR ".join(f"({w})" for _, w in rules)
        g = "geom" if str(osm).endswith(".gpkg") else "geometry"   # area osm.gpkg vs raw .osm/.osm.pbf (whole AU)
        sql = (f"SELECT CASE {case} END AS kind, name, other_tags AS osm_tags, {g} AS geom FROM {src} WHERE {anyw}")
        run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + ["-nln", lay, "-nlt", gtype,
             "-dialect", "SQLITE", "-sql", sql, out_part, osm], ctx,
            env={**os.environ, "CPL_TMPDIR": str(ctx.scratch())})   # .pbf reads
    for lay in ("field_points", "field_lines"):
        r = run(["ogrinfo", "-q", "-dialect", "SQLITE", "-sql", f"SELECT kind, COUNT(*) AS n FROM {lay} GROUP BY kind",
                 out_part], ctx)
        for k, n in re.findall(r"kind \(String\) = (.*)\n\s+n \(Integer\) = (\d+)", r):
            counts[k] = int(n)
    return counts


BUILD_SIG_VER = "1"
LIVE_DAYS = 30          # (unused: live-server files are re-made only when asked: --refresh-live / refresh --live)


def _vrt_members(x):
    """Files a raster or vector (OGR) VRT reads, from its XML (gdal.Open sees only raster VRTs: an OGR VRT came back
    unreadable and its source gpkg went untracked). None = unreadable."""
    import xml.etree.ElementTree as ET                # (local VRTs lmap/GDAL wrote; expat >= 2.4 caps entity expansion)
    try:
        if Path(x).stat().st_size > 64 << 20:          # a VRT is a few KB-MB: anything huge is not one we wrote
            return None
        root = ET.parse(x).getroot()
    except (OSError, ET.ParseError):
        return None
    out = []
    for el in root.iter():                       # GDAL writes these tags un-namespaced, as named
        if el.tag in ("SourceFilename", "SrcDataSource") and (el.text or "").strip():
            orig = el.text.strip()
            t = _member_file(orig)
            rel = (el.get("relativeToVRT") or "0").strip().lower() in ("1", "yes", "true", "on")
            p = Path(x).parent / t if rel and not orig.startswith("/vsi") and not t.startswith("/") else Path(t)
            out += _with_sidecars(str(p))
    return out


def _member_file(t):
    """The local file behind a member string: NETCDF:"f.nc":var / GPKG:f.gpkg:tbl -> f; /vsizip|gzip|tar/x.zip/a.tif
    -> x.zip. /vsicurl and connection strings stay as text (can't be stat'ed)."""
    m = re.match(r'^(?:NETCDF|HDF5|HDF4_SDS|HDF4|GPKG|SQLITE|GTIFF_DIR|NITF_IM):(.*)$', t)
    if m and not t.startswith("/vsi"):
        r = m.group(1)
        if r.startswith('"'):                    # NETCDF:"path":var
            r = r[1:].split('"', 1)[0]
        elif not os.path.exists(r):              # GPKG:path:table (path may hold ':'): longest existing prefix
            while ":" in r and not os.path.exists(r):
                r = r.rsplit(":", 1)[0]
        t = r
    m = re.match(r'^/vsi(?:zip|gzip|tar|7z|rar)/(/?.+?\.(?:zip|gz|tgz|tar|7z|rar))(?=/|$)', t, re.I)
    if m:
        g = m.group(1)
        return g if g.startswith("/") else "/" + g   # GDAL form /vsizip/home/u/x.zip/a.tif -> /home/u/x.zip
    return t


def _with_sidecars(p):
    """p plus the files whose change also changes what p reads: a shapefile's .dbf/.shx/.prj/.cpg, an SQLite or
    GeoPackage -wal, a folder's files (a folder of shapefiles)."""
    out = [p]
    q = Path(p)
    try:
        if q.suffix.lower() == ".shp":
            out += [str(q.with_suffix(e)) for e in (".dbf", ".shx", ".prj", ".cpg") if q.with_suffix(e).exists()]
        elif q.suffix.lower() in (".gpkg", ".sqlite", ".db") and Path(p + "-wal").exists():
            out.append(p + "-wal")
        elif q.is_dir():                         # every file below it (a change anywhere counts)
            out += sorted(str(f) for f in q.rglob("*") if f.is_file())
    except OSError:
        pass
    return out


def _sig_input(x, _seen=None):
    """One input's identity: path name + size + whole-second mtime (CIFS rounds sub-seconds). A VRT: its CONTENT
    hash (projects rewrite some VRTs each run: an mtime would churn) + its member files (a changed tile or gpkg
    behind an unchanged .vrt must rebuild); a non-file -> its text/name."""
    x = str(x)
    if x.startswith("/vsi") or not os.path.exists(x):
        return x
    try:
        st = os.stat(x)
    except OSError:
        return x                                 # vanished since the exists check
    if x.endswith(".xml") and st.st_size < 1 << 20:  # a tile-server definition shipped with lmap: its CONTENT, not
        import hashlib                           # mtime (a 2nd checkout / reinstall must not re-download a basemap)
        try:
            return f"{Path(x).name}:sha1:{hashlib.sha1(Path(x).read_bytes()).hexdigest()[:16]}"
        except OSError:
            return x
    if not x.endswith(".vrt"):
        return f"{Path(x).name}:{st.st_size}:{int(st.st_mtime)}"
    seen = (_seen or set()) | {os.path.realpath(x)}
    try:
        h = f"{st.st_size}:{int(st.st_mtime)}" if st.st_size > 64e6 else \
            hashlib.sha1(vrt_norm(Path(x).read_bytes())).hexdigest()[:12]   # huge: no hash, members still listed
    except OSError:
        return f"{Path(x).name}:unreadable"
    mem = _vrt_members(x)
    if mem is None:
        return f"{Path(x).name}:{h}:unreadable"
    return f"{Path(x).name}:{h}[" + ",".join(sorted({_sig_input(f, seen) for f in mem
                                                     if os.path.realpath(f) not in seen})) + "]"


def live_age():
    """Age bucket for live-sourced items: changes every LIVE_DAYS, so they are re-fetched that often."""
    return int(time.time() // (LIVE_DAYS * 86400))


def script_hash(src):
    """Hash of a Python script's tokens without comments or blank lines: editing a comment must not rebuild
    everything that script made."""
    import io, tokenize
    try:
        toks = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.COMMENT, tokenize.NL)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        toks = [src]
    return hashlib.sha1("\x00".join(toks).encode()).hexdigest()[:12]


def code_hash(*objs):
    """Short hash of the code that makes a file (an embedded script string or a function's source): a changed
    build step rebuilds what it made."""
    import inspect
    h = hashlib.sha1()
    for o in objs:
        if not (isinstance(o, str) or inspect.isfunction(o)):   # getsource on a class follows its module's
            raise TypeError(f"code_hash: functions or script text only, got {o!r}")   # file: bin/lmap, not lmaplib
        try:
            h.update((o if isinstance(o, str) else inspect.getsource(o)).encode())
        except (OSError, TypeError):
            h.update(getattr(getattr(o, "__code__", None), "co_code", b"") or str(o).encode())
    return h.hexdigest()[:12]


def build_sig(inputs, params):
    """Signature of a derived file: its inputs' size + mtime and the build parameters. Same signature = the file
    was built from exactly these inputs this way -> skip; anything else (or no record) -> rebuild."""
    flat = []
    for i in inputs:
        if isinstance(i, (list, tuple)):
            flat += [_sig_input(j) for j in i]
        elif i is not None:
            flat.append(_sig_input(i))
    return json.dumps({"v": BUILD_SIG_VER, "in": sorted(flat), "p": params}, sort_keys=True, default=str)


def sig_path(out):
    return Path(out).with_name(Path(out).name + ".lmap-sig")


def sig_current(out, sig):
    """out exists, reads, and was built with this signature."""
    try:
        return readable(out) and sig_path(out).read_text() == sig
    except OSError:
        return False


def sig_write(out, sig):
    try:
        atomic_write(sig_path(out), sig)
    except OSError:
        pass                                     # no record = rebuilt next time (safe direction)


def sig_stale(out):
    """Mark out as NOT current before rebuilding it: if the rebuild fails or is skipped (offline), the old file
    stays but is never trusted as current."""
    if Path(out).exists() or sig_path(out).exists():
        try:
            atomic_write(sig_path(out), "STALE")
        except OSError:
            sig_path(out).unlink(missing_ok=True)


def built_mark(out):
    """Local (not network-share clock) note that out was just built: a run that waited on the lock compares it with its
    own start time."""
    p = STATE / "locks" / f"built-{hashlib.sha1(str(out).encode()).hexdigest()[:16]}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True); p.touch()
    except OSError:
        pass


def built_since(out, t):
    try:
        return (STATE / "locks" / f"built-{hashlib.sha1(str(out).encode()).hexdigest()[:16]}").stat().st_mtime > t
    except OSError:
        return False


AREA_OUT = {"veg": "vegetation.tif", "osm-gpkg": "osm.gpkg", "osm-pbf": "osm.gpkg", "contours": "contours-dem.gpkg",
            "contours-lidar": "contours-lidar.gpkg", "contours-nsw5": "contours-nsw5.gpkg", "contours-best": "contours-best.gpkg", "peaks": "spot-heights.gpkg",
            "field": "field.gpkg", "dem3d": "dem-3d.tif", "slope": "slope.tif", "hls": "hls.gpkg", "grid": "grid.gpkg", "sat-even": "sat-even.tif"}


DEM_RESAMPLE = "cubic"     # heights: an interpolating kernel. Never cubicspline (smooths peaks/gullies off)


def area_raster_co(ctx, kind, src):
    """COG -co args of an area raster: 512 blocks + explicit overview resampling (GDAL's default for photos is
    nearest: jagged zoomed out). dem: DEFLATE + predictor (exact heights); topo: photo q90 (crisp text/lines);
    veg: DEFLATE, nearest overviews (class codes, never averaged); else photo. The area build and map_reuse
    both use it, so a reused copy is the same format as a built one."""
    co = (["COMPRESS=DEFLATE", "PREDICTOR=" + ("FLOATING_POINT" if is_float(src) else "2"), "OVERVIEW_RESAMPLING=BILINEAR"]
          if kind == "dem" else ["COMPRESS=DEFLATE", "OVERVIEW_RESAMPLING=NEAREST"] if kind == "veg"
          else [*photo_co(ctx, 90)[1::2], "OVERVIEW_RESAMPLING=AVERAGE"] if kind == "topo"
          else [*photo_co(ctx)[1::2], "OVERVIEW_RESAMPLING=AVERAGE"])
    return sum((["-co", c] for c in co + ["BLOCKSIZE=512"]), [])


def smooth_grid(info):
    """(-te values, -tr value) as strings for resampling a north-up raster (gdalinfo -json) to half its cell size
    so that every 2nd output pixel centre lands exactly on a source pixel centre: the extent grows by half a new cell
    on each side. With an interpolating kernel (cubic, bilinear) the measured heights then survive smoothing."""
    g = info["geoTransform"]; w, hgt = info["size"]; c = abs(g[1]); h = c / 2
    x0, y1 = g[0], g[3]; x1, y0 = x0 + w * c, y1 - hgt * abs(g[5])
    return [repr(v) for v in (x0 - h / 2, y0 - h / 2, x1 + h / 2, y1 + h / 2)], repr(h)


def _item_live(kind, src):
    return kind in ("arcgis", "sheet", "veg") or str(src).endswith(".xml") or str(src).startswith("/vsi")


def _item_code(kind):
    return {"peaks": lambda: code_hash(PEAKS_PY), "hls": lambda: code_hash(HLS_PY),
            "field": lambda: code_hash(build_field),
            "contours-lidar": lambda: code_hash(contours_lidar, _contours_lidar_fill),
            "contours-best": lambda: code_hash(contours_best),
            "contours-nsw5": lambda: code_hash(contours_nsw5),
            "sat-even": lambda: code_hash(sat_even, f"{SEA_EVEN_SIGMA_M}/{SEA_MIN_WIDTH_M}/{WATER_CLASS}"),
            # DEM resampled with cubic (passes through the samples) instead of cubicspline (B-spline:
            # lowered summits up to ~2 m on 30 m data) -> contours / 3D DEMs made with it rebuild once
            "contours": lambda: DEM_RESAMPLE + "/grid2", "dem3d": lambda: DEM_RESAMPLE}.get(kind, lambda: None)()


def item_sig(d, key, kind, src, ex, bbox, crs):
    """(out, inputs, signature) of one area item."""
    out = d / (AREA_OUT.get(kind) or f"{key}.{'gpkg' if kind in ('gpkg-clip', 'arcgis') else 'tif'}")
    ins = _item_inputs(d, key, kind, src, ex)
    p = {"kind": kind, "key": key, "crs": crs, "bbox": [round(float(v), 6) for v in bbox],
         "ex": {k: v for k, v in ex.items() if k != "cov"}, "fs": FIELD_SCHEMA if kind == "field" else None,
         "code": _item_code(kind) if key != "nsw-contours" else (_item_code(kind) or "") + (code_hash(nsw_contours_dedupe, nc_acc, has_field)
             + repr((NC_CELL_M, NC_CLOSE_M, NC_MIN_LEN_M, NC_DEM_MAX, NC_DEM_WIN, NC_CODE_ACC, NC_MAX_CELLS))), "live": None}   # no age bucket (offline; refresh on request)
    return out, ins, build_sig(ins, p)


def area_build_item(ctx, d, key, kind, src, ex, bbox, crs):
    """One area item, rebuilt only when what it is made from changed: a signature of its inputs (size + mtime) +
    parameters + build code sits beside it (<file>.lmap-sig); no signature = rebuild. Live sources (tile servers, ArcGIS, WorldCover over the net, GA sheets) can't be
    stat'ed: kept until asked (--refresh-live re-makes only those). --rebuild: rebuild regardless."""
    out, ins, sig = item_sig(d, key, kind, src, ex, bbox, crs)
    force = bool(getattr(ctx.a, "rebuild", False)) or (bool(getattr(ctx.a, "refresh_live", False)) and _item_live(kind, src))
    if not force:
        if readable(out) and sig_current(out, sig):
            return {"file": out.name, "unchanged": True}
        if kind == "arcgis" and not out.exists() and _sig_read(out) == "EMPTY " + sig:
            return {"file": None, "from": src, "empty": True, "unchanged": True}
    had = readable(out)
    c2 = copy.copy(ctx); c2.force_item = True    # inner: build it
    sig_stale(out)                               # a failed/skipped rebuild leaves the old file marked STALE
    r = _area_build_item(c2, d, key, kind, src, ex, bbox, crs)
    if isinstance(r, dict) and r.get("file") and "error" not in r and "skipped" not in r and readable(out):
        sig_write(out, sig)                      # inputs as read BEFORE the build: one replaced mid-build -> rebuilt next run
    elif isinstance(r, dict) and r.get("empty") and "error" not in r:
        out.unlink(missing_ok=True)              # nothing in the box now: the old features must not stay drawn
        sig_write(out, "EMPTY " + sig)           # not re-queried until the sources change (or --refresh-live)
    elif isinstance(r, dict) and had and readable(out):
        r = {**r, "stale_kept": f"{out.name} is from older inputs (rebuild {'skipped' if 'skipped' in r else 'failed'})"}
    return r


def _sig_read(out):
    try:
        return sig_path(out).read_text()
    except OSError:
        return None


def _built_after(out, ins):
    """out is newer than every input file (same file-server clock: all under the data root)."""
    try:
        mt = out.stat().st_mtime
        for i in ins:
            xs = list(i) if isinstance(i, (list, tuple)) else [i]
            k = 0
            while k < len(xs):                   # members behind a VRT count too (raster or OGR; nested followed)
                x = xs[k]; k += 1
                if x is not None and str(x).endswith(".vrt") and os.path.exists(str(x)) and len(xs) < 100000:
                    mem = _vrt_members(str(x))
                    if mem is None:
                        return False
                    xs += [f for f in mem if f not in xs]
            for x in xs:
                if x is not None and not str(x).startswith("/vsi") and not str(x).endswith(".xml") \
                        and os.path.exists(str(x)) \
                        and os.stat(str(x)).st_mtime > mt:
                    return False
        return True
    except OSError:
        return False


def _item_inputs(d, key, kind, src, ex):
    """What an area item is built from (files whose change must rebuild it)."""
    if kind in ("dem", "sat", "aerial", "topo", "contours-lidar"):
        return [src, ex.get("cov")]
    if kind == "osm-gpkg" or kind == "osm-pbf":
        return [src]
    if kind == "gpkg-clip":                        # NSW contours: the dedupe reads the DEM (lines far off it dropped)
        return [f for f, _ in src] + ([d / "dem.tif"] if key == "nsw-contours" else [])
    if kind in ("contours", "peaks", "slope", "dem3d"):
        return [d / "dem.tif"]
    if kind == "hls":
        return [d / "slope.tif"]
    if kind == "sat-even":
        return [d / "sat.tif", d / "vegetation.tif"]
    if kind == "field":
        return [d / "osm.gpkg"]
    if kind == "contours-best":
        return [d / n_ for n_ in ("qld-contours.gpkg", "contours-lidar.gpkg", "contours-nsw5.gpkg", "nsw-contours.gpkg", "contours-dem.gpkg")]
    return []                                    # veg / grid / sheet / arcgis: bbox + params only


def _area_build_item(ctx, d, key, kind, src, ex, bbox, crs):
    w, so, e, n = bbox
    if getattr(ctx, "force_item", False) and not ctx.a.refresh:   # area_build_item decided: build (locals check refresh)
        ctx = copy.copy(ctx); ctx.a = argparse.Namespace(**{**vars(ctx.a), "refresh": True}); ctx.force_item = True
    if kind == "sheet":                                   # GA map sheets (shared cache) cut to the box
        out = d / f"{key}.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        return area_sheet(ctx, d, out, src, bbox, crs)
    if kind in ("dem", "sat", "aerial", "topo"):
        out = d / f"{key}.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        # built in local scratch, then copied: the COG driver writes a full uncompressed temp + overviews next to
        # its output (20 GB for townsville aerial) and reads them back - over SMB that hit an I/O error (2026-10-15)
        if str(src).endswith(".xml"):                     # live tile server: no internet -> skip, not fail
            import re as _re
            m = _re.search(r"https?://([^/:\"'<>${}]+)", Path(src).read_text(errors="ignore"))
            if m and not any(os.environ.get(v) for v in ("https_proxy", "HTTPS_PROXY")) and not net_ok(m.group(1)):
                return {"skipped": f"no internet ({m.group(1)}); re-run online to add {key}"}
        part = ctx.scratch() / f"area-{d.name}-{key}.{os.getpid()}.part.tif"
        co = ["-r", "bilinear" if kind in ("dem", "topo") else "average", *area_raster_co(ctx, kind, src)]
        # outside the source -> transparent. DEM: nodata value. Imagery: alpha -> COG turns it into a
        # mask (a nodata of 0 also blanked real black pixels: dark sea became white speckle)
        co += (["-dstnodata", "-32768"] if kind == "dem"
               else ["-srcnodata", "0 0 0", "-dstalpha"] if kind == "aerial"   # TCC aerial: 0,0,0 = no coverage
               else ["-srcnodata", "None", "-dstalpha"])
        try:
            with BuildSlot(ctx, f"{d.name}/{out.name}"):          # all-cores warp: shares the machine-wide slots
                run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, n, "-t_srs", crs,
                     "-tr", ex["res"], ex["res"], "-of", "COG", "-co", "BIGTIFF=IF_SAFER", "-multi",
                     "-wo", "NUM_THREADS=ALL_CPUS"] + co + [src if kind == "dem" else photo_src(ctx, src, part), part], ctx,
                    env={**os.environ, "GDAL_CACHEMAX": "1024"})
            if not gdal_ok(part):
                raise CliError(E_FAIL, f"{key}: gdalwarp output failed gdalinfo")
            install_file(part, out)
        finally:
            for x in part.parent.glob(part.name + "*"):
                x.unlink(missing_ok=True)
        return {"file": out.name, "res_m": ex["res"], "from": ex["from"]}
    if kind == "veg":
        # ESA WorldCover 2021 v200: 3x3 degree COGs on S3 named by SW corner; only the box's pixels are read
        out = d / "vegetation.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        import math
        base = "/vsicurl/https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_"
        tiles = [f"{base}{'S' if la < 0 else 'N'}{abs(la):02d}{'W' if lo < 0 else 'E'}{abs(lo):03d}_Map.tif"
                 for la in range(math.floor(float(so) / 3) * 3, math.ceil(float(n) / 3) * 3, 3)
                 for lo in range(math.floor(float(w) / 3) * 3, math.ceil(float(e) / 3) * 3, 3)]
        from osgeo import gdal
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        wc = ctx.sources.get("esa-worldcover", {})
        local = ctx.path(wc["dest"]) if wc.get("dest") else None
        known = {f.split("_Map")[0][-7:] for f in wc.get("files", [])}           # all AU land tiles (sources.toml)
        use = []
        for t in tiles:
            nm = t.rsplit("_", 2)[-2]                                       # e.g. S21E144
            if known and nm not in known and 111 <= int(nm[4:]) <= 153 and int(nm[1:3]) <= 45:
                continue                                                    # AU sea: ESA has no tile (rest: S3 decides)
            f = local / f"{nm}_Map.tif" if local else None
            use.append(str(f) if f and f.is_file() else t)
        use = [t for t in use if not t.startswith("/vsicurl/") or t.rsplit("_", 2)[-2] in known
               or gdal.VSIStatL(t) is not None]                            # outside the AU list: does S3 have it?
        if not use:
            return {"skipped": "box is all sea (no WorldCover land tiles)"}
        if any(t.startswith("/vsicurl/") for t in use) and not any(os.environ.get(v) for v in ("https_proxy", "HTTPS_PROXY")) and not net_ok("esa-worldcover.s3.eu-central-1.amazonaws.com"):
            return {"skipped": "no internet and not stored offline (`lmap data fetch esa-worldcover`, 8 GB all AU)"}
        tiles = use
        part = ctx.scratch() / f"area-{d.name}-veg.{os.getpid()}.part.tif"
        try:
            raw = part.with_name(part.name + ".raw.tif")
            run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, n, "-t_srs", crs,
                 "-tr", ex["res"], ex["res"], "-r", "mode", "-of", "GTiff", "-co", "COMPRESS=DEFLATE",
                 "-dstnodata", "0"] + tiles + [raw], ctx, env={**os.environ, **gdal_env(),
                                                              "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"})
            if not gdal_ok(raw):
                raise CliError(E_UPSTREAM, "vegetation: ESA WorldCover read failed")
            if ex.get("sieve_px"):                     # specks (single 10 m cells of grass on cliff faces) printed as
                from osgeo import gdal                 # grey/white blotches: patches < sieve_px cells take their neighbours'
                gdal.UseExceptions(); r_ = gdal.Open(str(raw), gdal.GA_Update); b_ = r_.GetRasterBand(1)
                gdal.SieveFilter(b_, None, b_, int(ex["sieve_px"]), 8); r_ = b_ = None
            run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", str(raw), str(part)], ctx)
            if not gdal_ok(part):
                raise CliError(E_UPSTREAM, "vegetation: COG write failed")
            install_file(part, out)
        finally:
            for x in part.parent.glob(part.name + "*"):
                x.unlink(missing_ok=True)
        return {"file": out.name, "res_m": ex["res"], "from": ex["from"]}
    if kind in ("osm-gpkg", "osm-pbf"):
        out = d / "osm.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-osm.gpkg"; part.unlink(missing_ok=True)
        spat = ["-spat", w, so, e, n]
        if kind == "osm-gpkg":
            lay = {"places": ("places", "place IN ('city','town','village')" if ex["mode"] == "major" else None),
                   "roads": ("roads", "highway IN ('motorway','trunk','primary','secondary')" if ex["mode"] == "major" else None),
                   "waterways": ("waterways", "waterway = 'river'" if ex["mode"] == "major" else None)}
        else:
            lay = {"points": ("points", None), "lines": ("lines", None), "multipolygons": ("multipolygons", None)}
        env = {**os.environ, "OGR_SQLITE_SYNCHRONOUS": "OFF",
               "CPL_TMPDIR": str(ctx.scratch())}   # OSM driver temp (~1 GB) -> scratch, not cwd
        try:
            if kind == "osm-pbf":                         # all 3 layers in ONE pass over the AU pbf (was 3 passes).
                with hot_copy(ctx, src, "OSM") as pbf:    # the OSM driver over CIFS: 101 s; local: 25 s
                    run(["ogr2ogr", "-f", "GPKG"] + spat + [part, pbf] + list(lay), ctx, env=env)   # measured 2026-10-19:
                lay = {}                                  # 396 MB peak, 116 s (osmium extract: 1.3-2.4 GB, 100-200 s)
            for i, (name, (frm, where)) in enumerate(lay.items()):
                run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + spat + ["-nln", name]
                    + (["-where", where] if where else []) + [part, src, frm], ctx, env=env)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "layers": ["points", "lines", "multipolygons"] if kind == "osm-pbf" else list(lay)}
    if kind == "contours":
        out = d / "contours-dem.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        dem = d / "dem.tif"
        if not readable(dem):
            raise CliError(E_FAIL, "contours need dem.tif (built first)")
        part = ctx.scratch() / f"area-{d.name}-contours.gpkg"; part.unlink(missing_ok=True)
        sm = ctx.scratch() / f"area-{d.name}-dem-smooth.tif"
        try:
            # light smoothing first: raw 30m DEM gives jagged, noisy lines
            # half the DEM's cell size (15m from 30m, 45m from 90m); nodata explicit so the coast doesn't ring.
            # Grid shifted half a new cell: every 2nd new pixel centre sits ON a DEM sample, so the cubic
            # kernel returns the measured height there (on the default grid no centre hit one: summits ~7-20 m low
            # on a sharp synthetic peak)
            te, h = smooth_grid(json.loads(run(["gdalinfo", "-json", dem], ctx)))
            run(["gdalwarp", "-q", "-overwrite", "-r", DEM_RESAMPLE, "-srcnodata", "-32768", "-dstnodata", "-32768",
                 "-te", *te, "-tr", h, h, dem, sm], ctx)
            run(["gdal_contour", "-q", "-a", "elevation_m", "-i", str(ex["interval"]), "-snodata", "-32768",
                 "-f", "GPKG", "-nln", "contours", sm, part], ctx)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True); sm.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "interval_m": ex["interval"]}
    if kind == "contours-lidar":
        out = d / "contours-lidar.gpkg"; cov = Path(ex["cov"])
        newer = max(Path(src).stat().st_mtime, cov.stat().st_mtime)
        if (readable(out) and not ctx.a.refresh and out.stat().st_mtime >= newer
                and gpkg_meta(out, "lmap_key") == contours_key(crs, bbox)):       # same box + CRS
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-contours-lidar.{os.getpid()}.gpkg"; part.unlink(missing_ok=True)
        try:
            st = contours_lidar(ctx, src, cov, part, crs, bbox)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "interval_m": 10, **st}
    if kind == "contours-nsw5":
        out = d / "contours-nsw5.gpkg"
        if readable(out) and not ctx.a.refresh and gpkg_meta(out, "lmap_key") == contours_key(crs, bbox):
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-contours-nsw5.{os.getpid()}.gpkg"; part.unlink(missing_ok=True)
        try:
            st = contours_nsw5(ctx, src, part, crs, bbox)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "interval_m": 10, **st}
    if kind == "contours-best":
        out = d / "contours-best.gpkg"
        if not readable(d / "contours-dem.gpkg"):
            raise CliError(E_FAIL, "contours-best needs contours-dem.gpkg (built first)")
        prov = getattr(ctx.a, "contours_from", None) or "auto"
        pick = CONTOUR_PROVIDERS[prov]                  # ONE provider (+ the 30 m DEM only where it has no data)
        if prov == "auto" and readable(d / "contours-nsw5.gpkg"):
            pick = CONTOUR_PROVIDERS["nsw-5m"] | {"qld-contours.gpkg"}   # border box: QLD LiDAR stays on its side
        ins = [d / n_ for n_ in ("qld-contours.gpkg", "contours-lidar.gpkg", "contours-nsw5.gpkg", "nsw-contours.gpkg", "contours-dem.gpkg")
               if readable(d / n_) and n_ in pick | {"contours-dem.gpkg"}]
        if prov != "auto" and prov != "dem30" and len(ins) < 2:
            out.unlink(missing_ok=True); sig_path(out).unlink(missing_ok=True)   # never print the old set instead
            raise CliError(E_USAGE, f"--contours-from {prov}: this area has no {', '.join(sorted(pick))}",
                           "build it into the area first (lmap area create), or pick another --contours")
        if len(ins) < 2 and prov != "dem30":
            raise CliError(E_FAIL, "contours-best needs qld-contours.gpkg, contours-lidar.gpkg or nsw-contours.gpkg (built first)")
        newer = max(p_.stat().st_mtime for p_ in ins)
        key_ = json.dumps({"in": [p_.name for p_ in ins], "area": contours_key(crs, bbox), "prov": prov}, sort_keys=True)
        if (readable(out) and not ctx.a.refresh and out.stat().st_mtime >= newer      # inputs rebuilt -> rebuild
                and gpkg_meta(out, "lmap_key") == key_):     # a source gone / box changed -> rebuild
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-contours-best.{os.getpid()}.gpkg"; part.unlink(missing_ok=True)
        try:
            use = lambda n_: (d / n_) if (d / n_) in ins else None
            st = contours_best(use("qld-contours.gpkg"), d / "contours-dem.gpkg", part, crs, bbox,
                               use("contours-lidar.gpkg"), use("nsw-contours.gpkg"), use("contours-nsw5.gpkg"))
            from osgeo import ogr
            d_ = ogr.Open(str(part), 1); d_.SetMetadataItem("lmap_key", key_); d_.FlushCache(); d_ = None
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": " + ".join(p_.stem for p_ in ins), "provider": prov, **st}
    if kind == "peaks":
        out = d / "spot-heights.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        dem = d / "dem.tif"
        if not readable(dem):
            raise CliError(E_FAIL, "spot heights need dem.tif (built first)")
        gj = ctx.scratch() / f"area-{d.name}-peaks.geojson"; part = ctx.scratch() / f"area-{d.name}-peaks.gpkg"
        part.unlink(missing_ok=True)
        try:
            with MemTurn(ctx):
                r = subprocess.run(preexec_fn=_pdeath, args=[sys.executable, "-c", PEAKS_PY, str(dem), str(gj)], capture_output=True, text=True, timeout=600)
            if r.returncode:
                raise CliError(E_FAIL, f"spot heights failed: {(r.stderr or r.stdout).strip()[-200:]}")
            run(["ogr2ogr", "-f", "GPKG", "-nln", "spot_heights", part, gj], ctx)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True); gj.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "peaks": int(r.stdout.strip() or 0)}
    if kind == "field":
        out = d / "field.gpkg"
        if readable(out) and not ctx.a.refresh and gpkg_meta(out, "lmap_field") == FIELD_SCHEMA:
            return {"file": out.name, "unchanged": True}         # older schema (poles inside 'power tower'): rebuilt
        osm = d / "osm.gpkg"
        if not readable(osm):
            raise CliError(E_FAIL, "field features need osm.gpkg (built first)")
        part = ctx.scratch() / f"area-{d.name}-field.gpkg"; part.unlink(missing_ok=True)
        try:
            counts = build_field(ctx, osm, part)
            from osgeo import ogr
            d_ = ogr.Open(str(part), 1)
            if d_ is None:
                raise CliError(E_FAIL, f"field features: can't reopen {part.name} to tag it")
            d_.SetMetadataItem("lmap_field", FIELD_SCHEMA); d_.FlushCache(); d_ = None
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "features": counts}
    if kind == "dem3d":
        # 3D terrain only: DEM resampled to 1/3 of its cell size with a smooth (cubic spline) curve, so the 3D view
        # shows rounded ridges instead of 30 m stair-steps. No new detail; hillshade/contours keep using dem.tif.
        out = d / "dem-3d.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        dem = d / "dem.tif"
        if not readable(dem):
            raise CliError(E_FAIL, "dem-3d needs dem.tif (built first)")
        cell = abs(float(json.loads(run(["gdalinfo", "-json", dem], ctx))["geoTransform"][1]))
        r_ = str(max(cell / 3, 5)); part = ctx.scratch() / f"area-{d.name}-dem-3d.{os.getpid()}.part.tif"
        try:                                       # built in local scratch: COG temp + overviews never go over SMB
            run(["gdalwarp", "-q", "-overwrite", "-r", DEM_RESAMPLE, "-tr", r_, r_, "-srcnodata", "-32768",
                 "-dstnodata", "-32768", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=FLOATING_POINT",
                 dem, part], ctx)
            install_file(part, out)
            part.unlink(missing_ok=True)
        except BaseException:
            part.unlink(missing_ok=True); raise
        return {"file": out.name, "res_m": float(r_), "from": ex["from"]}
    if kind == "slope":
        out = d / "slope.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        dem = d / "dem.tif"
        if not readable(dem):
            raise CliError(E_FAIL, "slope needs dem.tif (built first)")
        part = ctx.scratch() / f"area-{d.name}-slope.{os.getpid()}.part.tif"
        try:
            run(["gdaldem", "slope", "-q", "-compute_edges", "-of", "COG", "-co", "COMPRESS=DEFLATE",
                 "-co", "PREDICTOR=FLOATING_POINT", dem, part], ctx)   # degrees; DEM is in metres (MGA)
            install_file(part, out)
            part.unlink(missing_ok=True)
        except BaseException:
            part.unlink(missing_ok=True); raise
        return {"file": out.name, "from": ex["from"]}
    if kind == "sat-even":
        out = d / "sat-even.tif"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        if not (readable(d / "sat.tif") and readable(d / "vegetation.tif")):
            raise CliError(E_FAIL, "sat-even needs sat.tif and vegetation.tif (built first)")
        sat_even(ctx, d / "sat.tif", d / "vegetation.tif", out)
        return {"file": out.name, "from": ex["from"]}
    if kind == "hls":
        out = d / "hls.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        slope = d / "slope.tif"
        if not readable(slope):
            raise CliError(E_FAIL, "HLS needs slope.tif (built first)")
        gj = ctx.scratch() / f"area-{d.name}-hls.geojson"; part = ctx.scratch() / f"area-{d.name}-hls.gpkg"
        part.unlink(missing_ok=True)
        try:
            with MemTurn(ctx):
                r = subprocess.run(preexec_fn=_pdeath, args=[sys.executable, "-c", HLS_PY, str(slope), str(gj)], capture_output=True, text=True, timeout=900)
            if r.returncode:
                raise CliError(E_FAIL, f"HLS failed: {(r.stderr or r.stdout).strip()[-200:]}")
            run(["ogr2ogr", "-f", "GPKG", "-nln", "hls", part, gj], ctx)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True); gj.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "sites": int(r.stdout.strip() or 0)}
    if kind == "grid":
        out = d / "grid.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-grid.gpkg"; part.unlink(missing_ok=True)
        try:
            mgrs_grid(bbox, crs, part, ctx)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"]}
    if kind == "gpkg-clip":
        out = d / f"{key}.gpkg"
        if readable(out) and not ctx.a.refresh:
            return {"file": out.name, "unchanged": True}
        part = ctx.scratch() / f"area-{d.name}-{key}.gpkg"; part.unlink(missing_ok=True); clips = []
        try:
            i = 0; sql = ex.get("sql") or {}               # sql: per layer SELECT (filter + rename, e.g. NSW contours)
            cl = state_clip_file(ctx, ex["clip_state"], bbox) if ex.get("clip_state") else None
            clips.append(cl)
            for f_, lays in src:
                with hot_copy(ctx, f_, key) as fl:     # statewide gpkg (<= 4 GB) over CIFS: a 1-sheet clip 61 s;
                    imm = ["-oo", "IMMUTABLE=YES"] if str(fl) != str(f_) else []   # local 0.1 s. No -wal/-shm
                    _ds = ro_ogr(fl); have = {_ds.GetLayer(j).GetName() for j in range(_ds.GetLayerCount())} if _ds else None
                    _ds = None
                    for lay in lays:
                        if have is not None and lay not in have:   # fetch writes no table for a layer with 0 features
                            continue                               # (seen on small clipped catalogs): none to clip
                        q_ = sql.get(lay)
                        if q_ and "verticalaccuracy" in q_ and not has_field(fl, lay, "verticalaccuracy"):
                            q_ = q_.replace(", verticalaccuracy", "")   # statewide file fetched before 99a
                        if q_ and ex.get("box_sql"):               # box-aware SQL (sea: merged inside the box)
                            px, py = (e - w) * 0.02, (n - so) * 0.02   # cut 2% outside the sheet: the map frame
                            q_ = q_.format(w=w - px, s=so - py, e=e + px, n=n + py)   # hides the cut edges
                        run(["ogr2ogr", *imm, "-f", "GPKG"] + (["-update"] if i else []) + ["-spat", w, so, e, n]
                            + (["-clipsrc", cl] if cl else []) + ["-nln", lay, part, fl]
                            + (["-dialect", ex["dialect"]] if ex.get("dialect") else [])
                            + (["-sql", q_] if q_ else [lay]), ctx); i += 1
            if not i:
                raise RuntimeError(f"{key}: none of its layers are in the fetched file (re-fetch it)")
            ded = nsw_contours_dedupe(part, d / "dem.tif") if key == "nsw-contours" else None
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)
            for p_ in clips:
                if p_:
                    p_.unlink(missing_ok=True)
        return {"file": out.name, "from": ex["from"], "offline": True, **({"dedupe": ded} if ded else {})}
    if kind == "arcgis":
        s = dict(ctx.src(src)); s.update(id=f"area-{d.name}-{src}", bbox=bbox, allow_empty=True,
                                         dest=str((d / f"{key}.gpkg").relative_to(ctx.root)))
        res = arcgis_fetch(ctx, s, False)
        if res.get("empty"):
            return {"file": None, "from": src, "empty": True}
        return {"file": f"{key}.gpkg", "from": src, **({"features": res["features"]} if "features" in res else {})}
    raise CliError(E_FAIL, f"unknown area item kind {kind}")


# sea: the OSM polygons are split into ~1 degree squares; merged into one shape inside the box so the split seams
# never draw as coastline. Cut to the box first (small, fast union); the R-tree keeps it off a full scan.
# The outer WHERE drops the one null row a box with no sea would give (0 features then).
SEA_SQL = ("SELECT geom FROM (SELECT ST_Multi(ST_Union(ST_Intersection(geom, BuildMbr({w}, {s}, {e}, {n}, 4326)))) AS geom "
           "FROM sea WHERE ST_Intersects(geom, BuildMbr({w}, {s}, {e}, {n}, 4326)) AND rowid IN (SELECT id FROM "
           "rtree_sea_geom WHERE maxx >= {w} AND minx <= {e} AND maxy >= {s} AND miny <= {n})) WHERE geom IS NOT NULL")


VEG_SIEVE_PX = 30          # vegetation tint: patches under 30 cells (0.3 ha at 10 m) merge into their surroundings


SEA_EVEN_SIGMA_M = 100     # photo sheets: sea colour evened over this distance (patch seams of mixed-date imagery)
SEA_MIN_WIDTH_M = 600      # only open water at least this wide: rivers, creeks, dams keep their true colour
WATER_CLASS = 80           # ESA WorldCover: permanent water bodies (sea, lakes, rivers)


def sat_even(ctx, sat, veg, out):
    """sat.tif with its water evened out: the coarse statewide imagery is a quilt of different dates, so open water
    shows square patches in different shades (sea along a coastal city). Over WorldCover water only, each pixel's colour
    is shifted by (water median - local water mean over ~SEA_EVEN_SIGMA_M); texture (waves, reef, sand bars) is
    kept, land is never touched. No water in the box -> a plain copy."""
    import numpy as np
    from osgeo import gdal
    from scipy import ndimage
    gdal.UseExceptions()
    s = gdal.Open(str(sat)); W, H, nb = s.RasterXSize, s.RasterYSize, s.RasterCount; gt = s.GetGeoTransform()
    nrgb = min(nb, 3)
    vw = ctx.scratch() / f"{out.stem}.{os.getpid()}.veg.tif"
    part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"
    cog = ctx.scratch() / f"{out.stem}.{os.getpid()}.cog.tif"
    try:
        gdal.Warp(str(vw), str(veg), format="GTiff", outputBounds=(gt[0], gt[3] + H * gt[5], gt[0] + W * gt[1], gt[3]),
                  width=W, height=H, dstSRS=s.GetProjection(), resampleAlg="near", creationOptions=["TILED=YES", "COMPRESS=DEFLATE"])
        vds = gdal.Open(str(vw)); v = vds.GetRasterBand(1)        # keep vds: a band outlives no dataset
        # coarse grid (~40 m) for the local means: cheap at any area size
        k = max(1, int(round(40 / abs(gt[1])))); cw, ch = max(1, W // k), max(1, H // k)
        cs = np.stack([s.GetRasterBand(b + 1).ReadAsArray(buf_xsize=cw, buf_ysize=ch, resample_alg=gdal.GRIORA_Average)
                       .astype("f4") for b in range(nrgb)])
        cv = v.ReadAsArray(buf_xsize=cw, buf_ysize=ch, resample_alg=gdal.GRIORA_NearestNeighbour)
        r_ = max(1, int(round(SEA_MIN_WIDTH_M / 2 / (abs(gt[1]) * W / cw))))
        yy_, xx_ = np.mgrid[-r_:r_ + 1, -r_:r_ + 1]; disk = xx_ ** 2 + yy_ ** 2 <= r_ * r_
        sea = ndimage.binary_opening(cv == WATER_CLASS, disk)      # narrow water (rivers) drops out; the sea stays
        sea = ndimage.binary_dilation(sea, iterations=1) & (cv == WATER_CLASS)
        wm = sea & (cs.sum(0) > 0)
        if wm.sum() < 50:                                   # no real water here: nothing to even
            shutil.copyfile(sat, part)
        else:
            sg = SEA_EVEN_SIGMA_M / (abs(gt[1]) * W / cw)
            m = ndimage.gaussian_filter(wm.astype("f4"), sg)
            off = np.zeros((nrgb, ch, cw), "f4")
            lab, nlab = ndimage.label(wm)                    # each water body evened to ITS OWN median (a dam
            near = ndimage.distance_transform_edt(lab == 0, return_distances=False, return_indices=True)   # stays a dam)
            lab_n = lab[near[0], near[1]]                    # every cell: its nearest body (for the blur fringe)
            for b in range(nrgb):
                loc = ndimage.gaussian_filter(np.where(wm, cs[b], 0), sg) / np.maximum(m, 1e-3)
                med = np.zeros(nlab + 1, "f4")
                med[1:] = ndimage.median(cs[b], lab, index=np.arange(1, nlab + 1))
                off[b] = np.where(m > 1e-3, med[lab_n] - loc, 0)
            drv = gdal.GetDriverByName("GTiff")
            o = drv.Create(str(part), W, H, nb, gdal.GDT_Byte, ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
            o.SetGeoTransform(gt); o.SetProjection(s.GetProjection())
            for b in range(nb):
                o.GetRasterBand(b + 1).SetColorInterpretation(s.GetRasterBand(b + 1).GetColorInterpretation())
                if s.GetRasterBand(b + 1).GetNoDataValue() is not None:
                    o.GetRasterBand(b + 1).SetNoDataValue(s.GetRasterBand(b + 1).GetNoDataValue())
            mf = s.GetRasterBand(1).GetMaskFlags(); dsmask = bool(mf & gdal.GMF_PER_DATASET) and not mf & gdal.GMF_ALPHA
            if dsmask:
                o.CreateMaskBand(gdal.GMF_PER_DATASET)
            step = 1024
            for y0 in range(0, H, step):
                h = min(step, H - y0)
                px = np.stack([s.GetRasterBand(b + 1).ReadAsArray(0, y0, W, h) for b in range(nb)])
                ys_ = np.minimum((np.arange(y0, y0 + h) // k), ch - 1); xs_ = np.minimum(np.arange(W) // k, cw - 1)
                wat = (v.ReadAsArray(0, y0, W, h) == WATER_CLASS) & sea[ys_][:, xs_] & (px[:nrgb].astype("u2").sum(0) > 0)
                if wat.any():
                    yy, xx = np.mgrid[y0:y0 + h, 0:W].astype("f4")
                    for b in range(nrgb):
                        sh = ndimage.map_coordinates(off[b], [yy / k - 0.5, xx / k - 0.5], order=1, mode="nearest")
                        px[b] = np.where(wat, np.clip(px[b] + sh, 1, 255), px[b])    # never 0,0,0 (= no data)
                for b in range(nb):
                    o.GetRasterBand(b + 1).WriteArray(px[b], 0, y0)
                if dsmask:
                    o.GetRasterBand(1).GetMaskBand().WriteArray(s.GetRasterBand(1).GetMaskBand().ReadAsArray(0, y0, W, h), 0, y0)
            o = None
        vds = v = None
        run(["gdal_translate", "-q", "-of", "COG", *area_raster_co(ctx, "sat", part), part, cog], ctx)
        install_file(cog, out)
    finally:
        for f_ in (vw, part, cog):
            f_.unlink(missing_ok=True)
    return out
CB_CELL, CB_GROW, CB_CLOSE = 50.0, 250.0, 500.0     # LiDAR outline: 50 m cells, lines grown 250 m, gaps < 1 km closed


def area_box_geom(crs, bbox):
    """(area SRS, the area box as an OGR polygon in it, edges densified)."""
    from osgeo import ogr, osr
    to = osr.SpatialReference(); to.SetFromUserInput(crs); to.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ll = osr.SpatialReference(); ll.ImportFromEPSG(4326); ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    t = osr.CoordinateTransformation(ll, to); ring = ogr.Geometry(ogr.wkbLinearRing)
    for i in range(41):
        ring.AddPoint_2D(*t.TransformPoint(bbox[0] + (bbox[2] - bbox[0]) * i / 40, bbox[1])[:2])
    for i in range(41):
        ring.AddPoint_2D(*t.TransformPoint(bbox[2], bbox[1] + (bbox[3] - bbox[1]) * i / 40)[:2])
    for i in range(41):
        ring.AddPoint_2D(*t.TransformPoint(bbox[2] - (bbox[2] - bbox[0]) * i / 40, bbox[3])[:2])
    for i in range(41):
        ring.AddPoint_2D(*t.TransformPoint(bbox[0], bbox[3] - (bbox[3] - bbox[1]) * i / 40)[:2])
    ring.CloseRings(); box = ogr.Geometry(ogr.wkbPolygon); box.AddGeometry(ring)
    return to, box


CL_RES, CL_EDGE, CL_MINLEN = 5, 10.0, 60.0   # LiDAR DEM contours: 2 m averaged to 5 m (2 m: banks/rocks = noise),
#                                            # coverage shrunk 10 m (the edge blends into the 10 m fill), loops < 60 m dropped


def contours_lidar(ctx, dem_p, cov_p, out, crs, bbox):
    """contours-lidar.gpkg (area CRS): layer `contours` = every 10 m from the LiDAR DEM (dem-3d-2m-3dview: LiDAR
    inside lidar-coverage, smooth 10 m fill outside -> only the inside is used), layer `area` = where they're valid."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    t0 = time.time()
    to, box = area_box_geom(crs, bbox)
    cs = ro_ogr(cov_p); CL = cs.GetLayer(0)
    sr = CL.GetSpatialRef().Clone(); sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    bs = box.Clone(); bs.Transform(osr.CoordinateTransformation(to, sr)); e_ = bs.GetEnvelope()
    CL.SetSpatialFilterRect(e_[0], e_[2], e_[1], e_[3])
    tr = osr.CoordinateTransformation(sr, to); cov = ogr.Geometry(ogr.wkbMultiPolygon)
    for f in CL:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone(); g.Transform(tr)
        if g.Intersects(box):
            g = g.Intersection(box)
            for k in range(g.GetGeometryCount()) if ogr.GT_Flatten(g.GetGeometryType()) != ogr.wkbPolygon else [None]:
                gg = g if k is None else g.GetGeometryRef(k)
                if ogr.GT_Flatten(gg.GetGeometryType()) == ogr.wkbPolygon:
                    cov.AddGeometry(gg.Clone())
    CL = None; cs = None                                # layer before its dataset (GDAL segfaults otherwise)
    cov = cov.UnionCascaded().Buffer(-CL_EDGE) if cov.GetGeometryCount() else None
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out))
    ds.SetMetadataItem("lmap_key", contours_key(crs, bbox))     # rebuilt when the area box / CRS changes
    C = ds.CreateLayer("contours", to, ogr.wkbLineString); C.CreateField(ogr.FieldDefn("elevation_m", ogr.OFTInteger))
    A = ds.CreateLayer("area", to, ogr.wkbMultiPolygon)
    n = 0
    try:
        n = _contours_lidar_fill(ctx, dem_p, crs, cov, to, out, ds, C, A)
    finally:
        A = C = None; ds = None                          # handles closed even when a GDAL step fails
    return {"lines": n, "area_km2": round(cov.GetArea() / 1e6, 1) if cov is not None else 0.0,
            "seconds": round(time.time() - t0, 1)}


# --contours: which ONE contour provider an area's print set (contours-best) uses; the 30 m DEM fills only where
# that provider has no data. auto = NSW 5 m DEM where built (NSW areas), else QLD LiDAR -> LiDAR DEM -> NSW lines.
CONTOUR_PROVIDERS = {
    "auto": {"qld-contours.gpkg", "contours-lidar.gpkg", "nsw-contours.gpkg"},
    "nsw-5m": {"contours-nsw5.gpkg"},           # NSW 5 m DEM: seamless (default in NSW)
    "nsw-lines": {"nsw-contours.gpkg"},         # NSW mapped lines: per-tile, seams at tile edges
    "lidar": {"qld-contours.gpkg", "contours-lidar.gpkg"},
    "dem30": set(),                             # 30 m Copernicus DEM everywhere
}
NSW5_TILE = 3000                                # px per exportImage request (server max 15000)


def contours_nsw5(ctx, url, out, crs, bbox):
    """contours-nsw5.gpkg (area CRS): layer `contours` = every 10 m from the NSW 5 m DEM (ImageServer exportImage,
    fetched for the area box at 5 m in the area CRS, tile by tile into scratch, deleted after); layer `area` =
    where the DEM has data (50 m cells). Same layout as contours-lidar.gpkg."""
    from osgeo import gdal, ogr, osr
    import numpy as np
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    t0 = time.time()
    to, box = area_box_geom(crs, bbox)
    e = box.GetEnvelope(); R = 5.0
    x0, x1 = math.floor(e[0] / R) * R, math.ceil(e[1] / R) * R
    y0, y1 = math.floor(e[2] / R) * R, math.ceil(e[3] / R) * R
    wk = crs.split(":")[-1]; step = NSW5_TILE * R
    tmp = ctx.scratch() / f"nsw5-{os.getpid()}"; tmp.mkdir(parents=True, exist_ok=True); tiles = []
    try:
        for ty in np.arange(y0, y1, step):
            for tx in np.arange(x0, x1, step):
                bx = (tx, ty, min(tx + step, x1), min(ty + step, y1))
                w, h = int(round((bx[2] - bx[0]) / R)), int(round((bx[3] - bx[1]) / R))
                q = urllib.parse.urlencode({"bbox": ",".join(f"{v:.1f}" for v in bx), "bboxSR": wk, "imageSR": wk,
                                            "size": f"{w},{h}", "format": "tiff", "pixelType": "F32",
                                            "noData": "-9999", "interpolation": "RSP_BilinearInterpolation", "f": "image"})
                tf = tmp / f"t{len(tiles)}.tif"
                def ok(pth):
                    try:
                        return gdal.Open(str(pth)).RasterXSize == w
                    except Exception:
                        return False
                if not http_get(f"{url}/exportImage?{q}", tf, timeout=180, check=ok):
                    raise CliError(E_UPSTREAM, "NSW 5 m elevation: tile not found", url)
                ds_ = gdal.Open(str(tf), gdal.GA_Update); ds_.SetProjection(to.ExportToWkt())
                ds_.SetGeoTransform((bx[0], R, 0, bx[3], 0, -R)); ds_.GetRasterBand(1).SetNoDataValue(-9999); ds_ = None
                tiles.append(str(tf))
        vrt = str(tmp / "dem.vrt"); gdal.BuildVRT(vrt, tiles, srcNodata=-9999, VRTNodata=-9999)
        mem = ogr.GetDriverByName("Memory").CreateDataSource(""); ML = mem.CreateLayer("c", to, ogr.wkbLineString)
        ML.CreateField(ogr.FieldDefn("elev", ogr.OFTReal))
        src = gdal.Open(vrt)
        gdal.ContourGenerateEx(src.GetRasterBand(1), ML, options=["LEVEL_INTERVAL=10", "ELEV_FIELD=elev"])
        # where there's data: 50 m cells
        cw = gdal.Warp("", src, format="MEM", xRes=50, yRes=50, resampleAlg="max", srcNodata=-9999, dstNodata=-9999)
        a = cw.GetRasterBand(1).ReadAsArray() > -9000
        cw.GetRasterBand(1).WriteArray(a.astype("float32"))
        pm = ogr.GetDriverByName("Memory").CreateDataSource(""); PA = pm.CreateLayer("p", to, ogr.wkbPolygon)
        PA.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
        mb = gdal.GetDriverByName("MEM").Create("", cw.RasterXSize, cw.RasterYSize, 1, gdal.GDT_Byte)
        mb.SetGeoTransform(cw.GetGeoTransform()); mb.SetProjection(cw.GetProjection())
        mb.GetRasterBand(1).WriteArray(a.astype("uint8")); cw = None; src = None
        gdal.Polygonize(mb.GetRasterBand(1), mb.GetRasterBand(1), PA, 0); mb = None
        cov = ogr.Geometry(ogr.wkbMultiPolygon)
        for f in PA:
            cov.AddGeometry(f.GetGeometryRef().Clone())
        cov = cov.UnionCascaded().Intersection(box) if cov.GetGeometryCount() else None
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out))
        ds.SetMetadataItem("lmap_key", contours_key(crs, bbox))
        C = ds.CreateLayer("contours", to, ogr.wkbLineString); C.CreateField(ogr.FieldDefn("elevation_m", ogr.OFTInteger))
        A = ds.CreateLayer("area", to, ogr.wkbMultiPolygon); n = 0
        ds.StartTransaction()
        for f in ML:
            g = f.GetGeometryRef()
            if g is None or g.GetPointCount() < 2:
                continue
            if not box.Contains(g):
                g = g.Intersection(box)
            for part in wf_lines(g):
                nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part.Clone()); nf["elevation_m"] = int(round(f["elev"]))
                C.CreateFeature(nf); n += 1
        if cov is not None and not cov.IsEmpty():
            af = ogr.Feature(A.GetLayerDefn()); af.SetGeometry(ogr.ForceToMultiPolygon(cov)); A.CreateFeature(af)
        ds.CommitTransaction(); ds = None; ML = None; mem = None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"lines": n, "area_km2": round(cov.GetArea() / 1e6, 1) if cov is not None else 0.0,
            "tiles": len(tiles), "seconds": round(time.time() - t0, 1)}


def contours_key(crs, bbox):
    return json.dumps({"crs": crs, "bbox": [round(float(v), 6) for v in bbox]}, sort_keys=True)


def gpkg_meta(p, k):
    """A dataset metadata item of a GPKG ('' if missing/unreadable)."""
    from osgeo import gdal
    try:
        d_ = gdal.OpenEx(str(p), gdal.OF_VECTOR, open_options=ro_gpkg_opts(p))
        v = (d_.GetMetadataItem(k) if d_ else None) or ""; d_ = None; return v
    except Exception:
        return ""


def _contours_lidar_fill(ctx, dem_p, crs, cov, to, out, ds, C, A):
    from osgeo import ogr
    n = 0
    if cov is not None and not cov.IsEmpty():
        af = ogr.Feature(A.GetLayerDefn()); af.SetGeometry(ogr.ForceToMultiPolygon(cov)); A.CreateFeature(af); af = None
        clip = cov.Buffer(0.5)                              # lines kept strictly inside the (shrunk) coverage
        e = cov.GetEnvelope(); x0, x1 = math.floor(e[0] / CL_RES) * CL_RES, math.ceil(e[1] / CL_RES) * CL_RES
        y0, y1 = math.floor(e[2] / CL_RES) * CL_RES, math.ceil(e[3] / CL_RES) * CL_RES
        sc = ctx.scratch(); tag = f"{Path(out).stem}.{os.getpid()}"
        tif, cg, lg = sc / f"{tag}.tif", sc / f"{tag}.cov.gpkg", sc / f"{tag}.lines.gpkg"
        try:
            run(["gdalwarp", "-q", "-overwrite", "-t_srs", crs, "-te", *map(str, (x0, y0, x1, y1)), "-tr", str(CL_RES),
                 str(CL_RES), "-r", "average", "-srcnodata", "-32768", "-dstnodata", "-32768", "-ot", "Float32",
                 "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER", str(dem_p), tif], ctx)
            d2 = ogr.GetDriverByName("GPKG").CreateDataSource(str(cg)); l2 = d2.CreateLayer("c", to, ogr.wkbMultiPolygon)
            f2 = ogr.Feature(l2.GetLayerDefn()); f2.SetGeometry(ogr.ForceToMultiPolygon(cov)); l2.CreateFeature(f2); d2 = None
            run(["gdal_rasterize", "-q", "-i", "-burn", "-32768", cg, tif], ctx)        # outside coverage = nodata
            run(["gdal_contour", "-q", "-a", "elevation_m", "-i", "10", "-snodata", "-32768", "-f", "GPKG",
                 "-nln", "contours", tif, lg], ctx)
            ls = ro_ogr(lg); LL = ls.GetLayer(0); ds.StartTransaction()
            for f in LL:
                g = f.GetGeometryRef(); z = f["elevation_m"]
                if g is None or g.Length() < CL_MINLEN:            # pits/bumps from 1 m of LiDAR noise
                    continue
                if z is None or round(z) <= 0:                     # 0 m = tide flats/waterline noise (the
                    continue                                       # coastline is drawn from OSM)
                if not clip.Contains(g):
                    g = g.Intersection(clip)
                for part in wf_lines(g):
                    if part.Length() < CL_MINLEN:
                        continue
                    nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part.Clone()); nf["elevation_m"] = int(round(z))
                    C.CreateFeature(nf); nf = None; n += 1
            ds.CommitTransaction()
        finally:
            f = LL = None; ls = None
            for x in (tif, cg, lg):
                x.unlink(missing_ok=True)
    return n


NC_CELL_M, NC_CLOSE_M, NC_MIN_LEN_M, NC_DEM_MAX, NC_DEM_WIN = 25.0, 600.0, 60.0, 100.0, 300.0   # NSW contour dedupe
#   (99a): footprint cells, gaps closed, shortest cut piece kept; a line whose median height is > NC_DEM_MAX outside
#   the DEM's range within NC_DEM_WIN of it is dropped (junk), so cliff lines on a coarse DEM stay
NC_MAX_CELLS = 25_000_000   # footprint grid cap: cells grow past NC_CELL_M on a huge area
NC_CODE_ACC = {13: 0.5, 14: 1.0, 1: 5.0, 5: 10.0, 6: 20.0}   # capturesourcecode -> vertical m when verticalaccuracy unset


def has_field(path, layer, name):
    """True if `layer` in the OGR file at `path` has the field `name`."""
    ds = ro_ogr(path); L = ds.GetLayerByName(layer) if ds is not None else None
    if L is None:
        return False
    D = L.GetLayerDefn()
    return any(D.GetFieldDefn(i).GetName().lower() == name.lower() for i in range(D.GetFieldCount()))


def nc_acc(code, vacc):
    """Stated vertical accuracy (m) of an NSW contour: verticalaccuracy, else by capturesourcecode, else 50."""
    try:
        if vacc is not None and float(vacc) > 0:
            return float(vacc)
    except (TypeError, ValueError):
        pass
    try:
        return NC_CODE_ACC.get(int(code), 50.0)
    except (TypeError, ValueError):
        return 50.0


def nsw_contours_dedupe(path, dem_p=None):
    """In place on the clipped nsw-contours.gpkg (layer `contours`, lon/lat or metres). NSW overlays capture batches
    (e.g. old 1:25k scans +-5 m under the 2 m programme +-1 m, which comes in tiles): each line is cut to outside
    the footprint of the more accurate lines, so every place keeps exactly one batch and none goes blank.
    Lines whose median height is > NC_DEM_MAX off the DEM (dem_p, if present) are dropped. Returns counts."""
    from osgeo import gdal, ogr, osr
    import math
    from collections import defaultdict
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    ds = ogr.Open(str(path), 1); L = ds.GetLayerByName("contours") if ds is not None else None
    if L is None or L.GetFeatureCount() == 0:
        return None
    sr = L.GetSpatialRef(); geo = sr is None or sr.IsGeographic()
    D = L.GetLayerDefn(); names = {D.GetFieldDefn(i).GetName().lower() for i in range(D.GetFieldCount())}
    hv = "verticalaccuracy" in names
    lat0 = None
    rows = []                                          # (fid, acc, [parts: [(x, y)]], z)
    for f in L:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone(); g.FlattenTo2D()
        parts = []                                     # per part: joining parts would draw a fake segment across gaps
        for k in range(max(g.GetGeometryCount(), 1)):
            p_ = g.GetGeometryRef(k) if g.GetGeometryCount() else g
            pts = [p_.GetPoint_2D(j) for j in range(p_.GetPointCount())]
            if pts:
                parts.append(pts)
        if not parts:
            continue
        if lat0 is None:
            lat0 = parts[0][0][1] if geo else 0.0
        rows.append((f.GetFID(), nc_acc(f["capturesourcecode"] if "capturesourcecode" in names else None,
                                        f["verticalaccuracy"] if hv else None), parts, f["elevation_m"]))
    if not rows:
        return None
    sx, sy = (111320.0 * math.cos(math.radians(lat0)), 110540.0) if geo else (1.0, 1.0)
    import numpy as np
    from scipy import ndimage
    drop_b, drop_d, cut = set(), set(), {}            # cut: fid -> [runs of points outside the better footprint]
    # junk first: a junk line must not make a footprint that cuts good lines and then vanish itself
    if dem_p and Path(dem_p).is_file():
        R = gdal.Open(str(dem_p)); b = R.GetRasterBand(1); a = b.ReadAsArray().astype("float64"); nd = b.GetNoDataValue()
        gt = R.GetGeoTransform()                    # off the DEM = outside the ground's height RANGE within ~NC_DEM_WIN
        k_ = max(3, int(NC_DEM_WIN / max(abs(gt[1]), 1e-9) / (111320.0 if abs(gt[1]) < 1 else 1)) | 1)   # (a 30 m DEM at a
        valid = np.isfinite(a) if nd is None else (np.isfinite(a) & (a != nd))                       # cliff: +-100s m)
        lo = ndimage.minimum_filter(np.where(valid, a, 1e9), k_); hi = ndimage.maximum_filter(np.where(valid, a, -1e9), k_)
        a = None
        rs = osr.SpatialReference(wkt=R.GetProjection()); rs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ls = sr.Clone() if sr is not None else osr.SpatialReference()
        if sr is None:
            ls.ImportFromEPSG(4326)
        ls.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(ls, rs)
        for fid, _, parts, z in rows:
            if z is None:
                continue
            pts = [q for p_ in parts for q in p_]
            dz = []
            for x, y in pts[::max(1, len(pts) // 20)]:
                X, Y = tr.TransformPoint(x, y)[:2]
                c_, r_ = math.floor((X - gt[0]) / gt[1]), math.floor((Y - gt[3]) / gt[5])
                if 0 <= r_ < lo.shape[0] and 0 <= c_ < lo.shape[1] and valid[r_, c_] and lo[r_, c_] <= hi[r_, c_]:
                    dz.append(max(lo[r_, c_] - z, z - hi[r_, c_], 0.0))
            if dz and sorted(dz)[len(dz) // 2] > NC_DEM_MAX:
                drop_d.add(fid)
        lo = hi = valid = None
    # footprint, not per line (whole-line drops left gaps where an old line crossed the edge of a 2 m-programme
    # tile, Blue Mountains 2026-10-22): per accuracy level, the area covered by more accurate lines (cells holding a
    # line, gaps < NC_CLOSE_M closed, SMALL enclosed holes filled: a missing tile inside others stays open); a less
    # accurate line is CUT to outside it
    xs = [q[0] for _, _, ps, _ in rows for p_ in ps for q in p_]; ys = [q[1] for _, _, ps, _ in rows for p_ in ps for q in p_]
    x0, y0 = min(xs) * sx, min(ys) * sy; w_, h_ = max(xs) * sx - x0, max(ys) * sy - y0; xs = ys = None
    cell = max(NC_CELL_M, math.sqrt(w_ * h_ / NC_MAX_CELLS))   # coarser on a huge area (memory)
    nx, ny = int(w_ // cell) + 1, int(h_ // cell) + 1
    ij = lambda q: (min(ny - 1, int((q[1] * sy - y0) // cell)), min(nx - 1, int((q[0] * sx - x0) // cell)))
    def _dense(pts):
        out = []
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] or pts):
            n = max(1, int(math.hypot((x2 - x1) * sx, (y2 - y1) * sy) // (cell / 2)))
            out += [(x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n) for k in range(n)]
        return out + [pts[-1]]
    r_ = int(NC_CLOSE_M / 2 // cell) + 1
    disk = np.hypot(*np.mgrid[-r_:r_ + 1, -r_:r_ + 1]) <= r_
    hole_max = math.pi * NC_CLOSE_M ** 2 / cell ** 2
    occ = np.zeros((ny, nx), bool)
    for k, acc in enumerate(sorted({acc for _, acc, *_ in rows})):
        mine = [(fid, [_dense(p_) for p_ in parts]) for fid, a_, parts, _ in rows if a_ == acc and fid not in drop_d]
        if k and occ.any():
            m_ = ndimage.binary_closing(np.pad(occ, r_ + 1), disk)[r_ + 1:-r_ - 1, r_ + 1:-r_ - 1]
            holes, n_ = ndimage.label(ndimage.binary_fill_holes(m_) & ~m_)
            if n_:
                sz = ndimage.sum(np.ones_like(holes), holes, range(1, n_ + 1))
                m_ |= np.isin(holes, 1 + np.flatnonzero(np.asarray(sz) <= hole_max))
            for fid, dparts in mine:
                runs, npt = [], 0
                for dp in dparts:
                    npt += len(dp); cur = []
                    for q in dp:
                        if m_[ij(q)]:
                            if len(cur) > 1:
                                runs.append(cur)
                            cur = []
                        else:
                            cur.append(q)
                    if len(cur) > 1:
                        runs.append(cur)
                runs = [r for r in runs if sum(math.hypot((u[0] - v[0]) * sx, (u[1] - v[1]) * sy)
                                               for u, v in zip(r, r[1:])) >= NC_MIN_LEN_M]
                if not runs:
                    drop_b.add(fid)
                elif sum(len(r) for r in runs) < npt:
                    cut[fid] = runs
            m_ = holes = None
        for fid, dparts in mine:
            if fid not in drop_b:
                for q in (cut[fid] if fid in cut else dparts):
                    for p in q:
                        occ[ij(p)] = True
    ds.StartTransaction()
    for fid in drop_b | drop_d:
        L.DeleteFeature(fid)
    for fid, runs in cut.items():
        if fid in drop_d:
            continue
        f = L.GetFeature(fid); L.DeleteFeature(fid)
        for r in runs:
            g = ogr.Geometry(ogr.wkbLineString)
            for x, y in r:
                g.AddPoint_2D(x, y)
            nf = ogr.Feature(L.GetLayerDefn()); nf.SetFrom(f); nf.SetFID(-1)
            nf.SetGeometry(ogr.ForceTo(g, L.GetGeomType()) if L.GetGeomType() not in (ogr.wkbLineString, ogr.wkbUnknown) else g)
            L.CreateFeature(nf)
    ds.CommitTransaction(); ds = None
    kept = [acc for fid, acc, *_ in rows if fid not in drop_b and fid not in drop_d]
    return {"lines": len(rows), "dropped_overlap": len(drop_b), "cut_at_better": len(cut), "dropped_dem": len(drop_d),
            "acc_m": sorted(set(kept))}


def contours_best(lidar_p, dem_p, out, crs, bbox, ldem_p=None, nsw_p=None, nsw5_p=None):
    """contours-best.gpkg (area CRS), every 10 m. Layer `contours`: QLD LiDAR lines (src 'lidar'), then lines from
    the LiDAR DEM (contours-lidar.gpkg, src 'lidar-dem') where QLD has none, then NSW Government mapped lines
    (nsw-contours.gpkg, src 'nsw': 1:25k / large-scale photogrammetry, NOT LiDAR) outside both, then the 30 m DEM
    lines (src 'approx') outside all. Layer `lidar_area` = where either LiDAR source is; `nsw_area` = where the NSW
    lines are used. nsw5_p (contours-nsw5.gpkg, src 'nsw-5m') is used like the LiDAR-DEM lines; the caller passes
    ONE provider (CONTOUR_PROVIDERS). Never two sets in one place. lidar_p / ldem_p / nsw_p / nsw5_p may be None."""
    from osgeo import gdal, ogr, osr
    import numpy as np
    from scipy import ndimage
    gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
    to = osr.SpatialReference(); to.SetFromUserInput(crs); to.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ll = osr.SpatialReference(); ll.ImportFromEPSG(4326); ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr_b = osr.CoordinateTransformation(ll, to)
    _, box = area_box_geom(crs, bbox)
    pts = [tr_b.TransformPoint(x, y)[:2] for x in (bbox[0], (bbox[0] + bbox[2]) / 2, bbox[2])
           for y in (bbox[1], (bbox[1] + bbox[3]) / 2, bbox[3])]
    x0 = math.floor(min(p[0] for p in pts) / CB_CELL) * CB_CELL; x1 = math.ceil(max(p[0] for p in pts) / CB_CELL) * CB_CELL
    y0 = math.floor(min(p[1] for p in pts) / CB_CELL) * CB_CELL; y1 = math.ceil(max(p[1] for p in pts) / CB_CELL) * CB_CELL
    W, H = int((x1 - x0) / CB_CELL), int((y1 - y0) / CB_CELL)
    t0 = time.time()
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(out))
    C = ds.CreateLayer("contours", to, ogr.wkbLineString)
    C.CreateField(ogr.FieldDefn("elevation_m", ogr.OFTInteger)); C.CreateField(ogr.FieldDefn("src", ogr.OFTString))
    # integer metres: QLD heights like 109.99999 must still match the "% 10 = 0" filters in QGIS
    mem = ogr.GetDriverByName("Memory").CreateDataSource(""); ML = mem.CreateLayer("l", to, ogr.wkbLineString)
    n_l = n_d = n_x = n_5 = 0
    src = ro_ogr(lidar_p) if lidar_p else None
    L = None if src is None else next((src.GetLayer(i) for i in range(src.GetLayerCount()) if src.GetLayer(i).GetName().startswith("contour_lidar")),
             src.GetLayer(0))
    if L is not None:
        sr = L.GetSpatialRef()
        if sr is None:
            raise CliError(E_FAIL, f"{Path(lidar_p).name}: layer {L.GetName()} has no CRS")
        sr = sr.Clone(); sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(sr, to)
        bs = box.Clone(); bs.Transform(osr.CoordinateTransformation(to, sr)); e_ = bs.GetEnvelope()
        L.SetSpatialFilterRect(e_[0], e_[2], e_[1], e_[3])  # only lines near the area (GPKG rtree)
    ds.StartTransaction()
    for f in (L if L is not None else []):
        g = f.GetGeometryRef(); z = f["elevation_m"]
        if g is None or z is None:
            continue
        g = g.Clone(); g.Transform(tr)
        if not g.Intersects(box):
            continue
        if not box.Contains(g):
            g = g.Intersection(box)
        for part in wf_lines(g):
            part = part.Clone()
            mf = ogr.Feature(ML.GetLayerDefn()); mf.SetGeometry(part); ML.CreateFeature(mf)   # every line marks coverage
            if abs(z - round(z / 10) * 10) < 0.05:          # the print shows 10 m (QLD heights carry float noise)
                nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part); nf["elevation_m"] = int(round(z / 10) * 10); nf["src"] = "lidar"
                C.CreateFeature(nf); n_l += 1
    ds.CommitTransaction(); L = None; src = None
    r = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    r.SetGeoTransform((x0, CB_CELL, 0, y1, 0, -CB_CELL)); r.SetProjection(to.ExportToWkt())
    gdal.RasterizeLayer(r, [1], ML, burn_values=[1], options=["ALL_TOUCHED=TRUE"]); ML = None; mem = None
    m = r.GetRasterBand(1).ReadAsArray().astype(bool)
    rad = int(round(CB_GROW / CB_CELL)); yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    m = ndimage.binary_dilation(m, structure=(xx * xx + yy * yy) <= rad * rad)
    k = int(round(CB_CLOSE / CB_CELL))
    if m.any():                                         # border 1: the closing must not shave the box edges
        m = ndimage.binary_closing(m, structure=np.ones((2 * k + 1, 2 * k + 1)), border_value=1)
    m = ndimage.binary_fill_holes(m)
    bm = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    bm.SetGeoTransform((x0, CB_CELL, 0, y1, 0, -CB_CELL)); bm.SetProjection(to.ExportToWkt())
    bl = ogr.GetDriverByName("Memory").CreateDataSource(""); bL = bl.CreateLayer("b", to, ogr.wkbPolygon)
    bf = ogr.Feature(bL.GetLayerDefn()); bf.SetGeometry(box); bL.CreateFeature(bf)
    gdal.RasterizeLayer(bm, [1], bL, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    m &= bm.GetRasterBand(1).ReadAsArray().astype(bool); bL = None; bl = None; bm = None
    r.GetRasterBand(1).WriteArray(m.astype("uint8"))
    pm = ogr.GetDriverByName("Memory").CreateDataSource(""); PA = pm.CreateLayer("p", to, ogr.wkbPolygon)
    PA.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(r.GetRasterBand(1), r.GetRasterBand(1), PA, 0); r = None
    cov = ogr.Geometry(ogr.wkbMultiPolygon)
    for f in PA:
        cov.AddGeometry(f.GetGeometryRef().Clone())
    PA = None; pm = None
    cov = cov.UnionCascaded() if cov.GetGeometryCount() else None
    cut = cov.SimplifyPreserveTopology(CB_CELL / 2).Buffer(CB_CELL / 2, 1) if cov is not None else None
    # LiDAR-DEM lines: only where the QLD lines aren't
    for fine_p, fine_src in ((ldem_p, "lidar-dem"), (nsw5_p, "nsw-5m")):
        lsrc = ro_ogr(fine_p) if fine_p and Path(fine_p).is_file() else None
        if lsrc is not None and lsrc.GetLayerByName("contours") is not None and lsrc.GetLayerByName("area") is not None:
            la = None
            for f in lsrc.GetLayerByName("area"):
                g = f.GetGeometryRef()
                if g is not None and not g.IsEmpty():
                    la = g.Clone() if la is None else la.Union(g)
            if la is not None:
                la = la.Intersection(box)
                ds.StartTransaction()
                for f in lsrc.GetLayerByName("contours"):
                    g = f.GetGeometryRef(); z = f["elevation_m"]
                    if g is None or z is None or not g.Intersects(box):
                        continue
                    if not box.Contains(g):
                        g = g.Intersection(box)
                    if cut is not None and g.Intersects(cut):
                        g = g.Difference(cut)
                        if g is None or g.IsEmpty():
                            continue
                    for part in wf_lines(g):
                        nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part.Clone()); nf["elevation_m"] = int(round(z))
                        nf["src"] = fine_src; C.CreateFeature(nf); n_x += fine_src == "lidar-dem"; n_5 += fine_src == "nsw-5m"
                ds.CommitTransaction()
                la = la.SimplifyPreserveTopology(CB_CELL / 2)
                cov = la if cov is None else cov.Union(la)
                cut = cov.Buffer(CB_CELL / 2, 1)
    f = None; lsrc = None
    A = ds.CreateLayer("lidar_area", to, ogr.wkbMultiPolygon); A.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    if cov is not None and not cov.IsEmpty():
        af = ogr.Feature(A.GetLayerDefn()); af.SetGeometry(ogr.ForceToMultiPolygon(cov)); af["v"] = 1; A.CreateFeature(af)
    area_frac = (cov.Intersection(box).GetArea() / box.GetArea()) if cov is not None else 0.0
    # NSW Government lines: outside the LiDAR; their own footprint (lines grown + gaps closed, like the QLD set) is
    # where they replace the approx 30 m lines
    n_n = 0; nsw_frac = 0.0
    nsrc = ro_ogr(nsw_p) if nsw_p and Path(nsw_p).is_file() else None
    NL = nsrc.GetLayerByName("contours") if nsrc is not None else None
    if NL is not None:
        if NL.GetSpatialRef() is None:
            raise CliError(E_FAIL, f"{Path(nsw_p).name}: layer contours has no CRS")
        sr = NL.GetSpatialRef().Clone(); sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(sr, to)
        bs = box.Clone(); bs.Transform(osr.CoordinateTransformation(to, sr)); e_ = bs.GetEnvelope()
        NL.SetSpatialFilterRect(e_[0], e_[2], e_[1], e_[3])
        mem = ogr.GetDriverByName("Memory").CreateDataSource(""); ML = mem.CreateLayer("l", to, ogr.wkbLineString)
        ds.StartTransaction()
        for f in NL:
            g = f.GetGeometryRef(); z = f["elevation_m"]
            if g is None or z is None or abs(z - round(z / 10) * 10) >= 0.05:
                continue
            g = g.Clone(); g.Transform(tr)
            if not g.Intersects(box):
                continue
            if not box.Contains(g):
                g = g.Intersection(box)
            for part in wf_lines(g):
                mf = ogr.Feature(ML.GetLayerDefn()); mf.SetGeometry(part.Clone()); ML.CreateFeature(mf)
            if cut is not None and g.Intersects(cut):
                g = g.Difference(cut)
                if g is None or g.IsEmpty():
                    continue
            for part in wf_lines(g):
                nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part.Clone()); nf["elevation_m"] = int(round(z / 10) * 10)
                nf["src"] = "nsw"; C.CreateFeature(nf); n_n += 1
        ds.CommitTransaction(); f = None
        ncov = _line_cover(ML, to, box, x0, y1, W, H); ML = None; mem = None
        if ncov is not None:
            if cov is not None:
                ncov = ncov.Difference(cov)
            NA = ds.CreateLayer("nsw_area", to, ogr.wkbMultiPolygon); NA.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
            if ncov is not None and not ncov.IsEmpty():
                nf = ogr.Feature(NA.GetLayerDefn()); nf.SetGeometry(ogr.ForceToMultiPolygon(ncov)); nf["v"] = 1; NA.CreateFeature(nf)
                nsw_frac = ncov.Intersection(box).GetArea() / box.GetArea()
                allc = ncov if cov is None else cov.Union(ncov)
                cut = allc.Buffer(CB_CELL / 2, 1)
            NA = None
    NL = None; nsrc = None
    dsrc = ro_ogr(dem_p); DL = dsrc.GetLayer(0)
    ds.StartTransaction()
    for f in DL:
        g = f.GetGeometryRef()
        if g is None or f["elevation_m"] is None:
            continue
        if not g.Intersects(box):                       # same box as the LiDAR side
            continue
        if not box.Contains(g):
            g = g.Intersection(box)
        if cut is not None and g.Intersects(cut):
            g = g.Difference(cut)
            if g is None or g.IsEmpty():
                continue
        for part in wf_lines(g):
            nf = ogr.Feature(C.GetLayerDefn()); nf.SetGeometry(part.Clone()); nf["elevation_m"] = int(round(f["elevation_m"]))
            nf["src"] = "approx"; C.CreateFeature(nf); n_d += 1
    ds.CommitTransaction(); f = DL = None; dsrc = None; A = C = None; ds = None
    return {"lidar_lines": n_l, "lidar_dem_lines": n_x, "nsw_5m_lines": n_5, "nsw_lines": n_n, "approx_lines": n_d,
            "lidar_area_pct": round(100 * area_frac, 1), "nsw_area_pct": round(100 * nsw_frac, 1),
            "seconds": round(time.time() - t0, 1)}


def _line_cover(ML, to, box, x0, y1, W, H):
    """Footprint of a set of contour lines (area CRS): rasterised at CB_CELL, grown CB_GROW, gaps < CB_CLOSE closed,
    holes filled, cut to the box. Same rule as the QLD LiDAR footprint. None if no lines."""
    from osgeo import gdal, ogr
    import numpy as np
    from scipy import ndimage
    r = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    r.SetGeoTransform((x0, CB_CELL, 0, y1, 0, -CB_CELL)); r.SetProjection(to.ExportToWkt())
    gdal.RasterizeLayer(r, [1], ML, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    m = r.GetRasterBand(1).ReadAsArray().astype(bool)
    if not m.any():
        return None
    rad = int(round(CB_GROW / CB_CELL)); yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    m = ndimage.binary_dilation(m, structure=(xx * xx + yy * yy) <= rad * rad)
    k = int(round(CB_CLOSE / CB_CELL))
    m = ndimage.binary_closing(m, structure=np.ones((2 * k + 1, 2 * k + 1)), border_value=1)
    m = ndimage.binary_fill_holes(m)
    bm = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    bm.SetGeoTransform((x0, CB_CELL, 0, y1, 0, -CB_CELL)); bm.SetProjection(to.ExportToWkt())
    bl = ogr.GetDriverByName("Memory").CreateDataSource(""); bL = bl.CreateLayer("b", to, ogr.wkbPolygon)
    bf = ogr.Feature(bL.GetLayerDefn()); bf.SetGeometry(box); bL.CreateFeature(bf)
    gdal.RasterizeLayer(bm, [1], bL, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    m &= bm.GetRasterBand(1).ReadAsArray().astype(bool)
    r.GetRasterBand(1).WriteArray(m.astype("uint8"))
    pm = ogr.GetDriverByName("Memory").CreateDataSource(""); PA = pm.CreateLayer("p", to, ogr.wkbPolygon)
    PA.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(r.GetRasterBand(1), r.GetRasterBand(1), PA, 0)
    cov = ogr.Geometry(ogr.wkbMultiPolygon)
    for f in PA:
        cov.AddGeometry(f.GetGeometryRef().Clone())
    if not cov.GetGeometryCount():
        return None
    return cov.UnionCascaded().SimplifyPreserveTopology(CB_CELL / 2).Intersection(box)


AREA_QGIS = r'''
import sys, json, re, zipfile, os, glob, time
from qgis.core import *
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtCore import Qt
QgsApplication.setPrefixPath('/usr', True); app = QgsApplication([], False); app.initQgis()
_QVL = QgsVectorLayer
class QgsVectorLayer(_QVL):   # read-only while building: QGIS opens a GPKG for update and, for an empty layer,
    def __init__(self, path='', baseName='', providerLib='ogr', options=None):   # tries to write its extent ->
        if options is None and providerLib == 'ogr':          # SQLite lock wait (5 s each over CIFS, ~3 min an area)
            options = _QVL.LayerOptions(QgsProject.instance().transformContext()); options.forceReadOnly = True
        super().__init__(path, baseName, providerLib, *([options] if options is not None else []))
d, name, crs_id, bbox, level, scratch = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5], sys.argv[6]
p = QgsProject.instance(); p.clear(); p.setTitle(f'{name} ({level}) - lmap area')
crs = QgsCoordinateReferenceSystem(crs_id); p.setCrs(crs)
p.writeEntry('Measurement', '/AngleUnits', 'mil'); p.writeEntry('Measurement', '/DistanceUnits', 'meters')
p.writeEntry('Measurement', '/AreaUnits', 'm2')
root = p.layerTreeRoot()
_CW = [str(v) for v in json.loads(os.environ.get('LMAP_CW') or '[0.12, 0.24]')]   # contour, index mm
_FILES = json.loads(os.environ.get('LMAP_FILES') or '{}')   # statewide project: point names at the big shared files
f = lambda n: _FILES.get(n, os.path.join(d, n))
_NAMES = json.loads(os.environ.get('LMAP_NAMES') or '{}')   # layer names that say which imagery (year/source)
layer_name = lambda n, default: _NAMES.get(n) or default   # not `nm`: the template uses nm as a loop variable
def add(l, on=True, parent=None):
    p.addMapLayer(l, False); (parent or root).addLayer(l).setItemVisibilityChecked(on); return l
def track_sym(kind):
    # ONE track symbology on every sheet (topo + photo) and the project: yellow + dark casing;
    # 4WD = dashed, other tracks = dash-dot (map.py's print styles copy these numbers)
    s = QgsLineSymbol.createSimple({'color': '0,0,0,170', 'width': '0.5', 'capstyle': 'flat'})
    y = QgsSimpleLineSymbolLayer(QColor(255, 220, 40), 0.35); y.setPenCapStyle(Qt.FlatCap)
    y.setUseCustomDashPattern(True); y.setCustomDashVector([2.0, 0.8] if kind == '4wd' else [2.0, 0.6, 0.4, 0.6])
    s.symbolLayer(0).setUseCustomDashPattern(True)
    s.symbolLayer(0).setCustomDashVector([2.0, 0.8] if kind == '4wd' else [2.0, 0.6, 0.4, 0.6])
    s.appendSymbolLayer(y); return s
def zoom(l, minscale):
    if minscale: l.setScaleBasedVisibility(True); l.setMinimumScale(minscale)
def vec(path, layer, nm, sym, minscale=0, where=None, parent=None, on=True):
    v = QgsVectorLayer(f'{path}|layername={layer}' + (f'|subset={where}' if where else ''), nm, 'ogr')
    if not v.isValid(): return None
    if where:   # a filtered extent = full table scan at save time (16 GB contours over a network share: 17 min). Use the unfiltered one.
        v.setExtent(QgsVectorLayer(f'{path}|layername={layer}', 'x', 'ogr').extent())
    v.setRenderer(QgsSingleSymbolRenderer(sym)); zoom(v, minscale); return add(v, on, parent)
# the map style table (share/style.toml + config [style]): one symbol size, one line width, line rows by class
STY = json.loads(os.environ.get('LMAP_STYLE') or '{}')
SYM_MM = float(STY.get('symbol_mm', 2.0)); PEAK_MM = SYM_MM * float(STY.get('peak_scale', 1.25))
LINE_MM = float(STY.get('line_mm', 0.2))
TEXT_PT = float(STY.get('text_pt', 6.0)); HALO_MM = float(STY.get('halo_mm', 0.0))
def sty_tf(tf, bold=False):
    """A name's text format from the style: one size, halo only if halo_mm > 0."""
    tf.setSize(TEXT_PT); f_ = tf.font(); f_.setBold(bold); tf.setFont(f_)
    b = tf.buffer(); b.setEnabled(HALO_MM > 0); b.setSize(HALO_MM); tf.setBuffer(b); return tf
def sty_ln(cls):
    """Line symbol for a style.toml [line.<cls>] row: colour, dash (mm), width (row width or line_mm)."""
    r = (STY.get('line') or {})[cls]
    s = QgsLineSymbol.createSimple({'color': r['colour'], 'width': str(r.get('width', LINE_MM)),
                                    'capstyle': 'flat' if r['dash'] else 'round', 'joinstyle': 'round'})
    if r['dash']:
        s.symbolLayer(0).setUseCustomDashPattern(True); s.symbolLayer(0).setCustomDashVector([float(x) for x in r['dash']])
    return s
sty_name = lambda cls: (STY.get('line') or {})[cls]['legend']
def mk(k, peak=False, **kw):
    """Simple symbol. Every point symbol is the style's one size (symbol_mm; peaks x peak_scale), whatever size
    the caller wrote; size '0' = an invisible label anchor, kept."""
    if k == 'pt' and str(kw.get('size', '')) != '0':
        kw['size'] = str(PEAK_MM if peak else SYM_MM)
        kw.pop('outline_color', None) if kw.get('outline_color') in ('white', '255,255,255') else None
        if 'outline_color' not in kw:
            kw['outline_style'] = 'no'          # no white ring round symbols
    return {'pt': QgsMarkerSymbol, 'ln': QgsLineSymbol, 'pg': QgsFillSymbol}[k].createSimple(kw)
def label(v, field):
    s = QgsPalLayerSettings(); s.fieldName = field; s.enabled = True
    s.setFormat(sty_tf(QgsTextFormat()))
    v.setLabeling(QgsVectorLayerSimpleLabeling(s)); v.setLabelsEnabled(True)

def contour_tiles(path, nm, parent):
    """Statewide projects: contours from a vector-tile pyramid (mbtiles), styled as contour_style(10).
    None when absent/unreadable (the caller falls back to the full contours)."""
    if not path or not os.path.isfile(path):
        return None
    c = QgsVectorTileLayer(f'type=mbtiles&url={path}', nm)
    if not c.isValid():
        return None
    _sty = []
    for ex, w, rule in (('"elevation_m" % 50 = 0', _CW[1], 'Index 50m'), ('"elevation_m" % 50 <> 0', _CW[0], '10m')):
        s_ = QgsVectorTileBasicRendererStyle(rule, 'contours', QgsWkbTypes.LineGeometry)
        s_.setSymbol(mk('ln', color='160,100,40,200', width=w)); s_.setFilterExpression(ex); _sty.append(s_)
    r_ = QgsVectorTileBasicRenderer(); r_.setStyles(_sty); c.setRenderer(r_)
    ls = QgsPalLayerSettings(); ls.fieldName = 'format_number("elevation_m", 0)'; ls.isExpression = True
    ls.placement = Qgis.LabelPlacement.Line; ls.enabled = True
    ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
    ls.repeatDistance = 150; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    ls.thinningSettings().setMinimumFeatureSize(15); ls.priority = 2
    tf = QgsTextFormat(); tf.setSize(7); tf.setColor(QColor(130, 80, 30)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.7); tf.setBuffer(b); ls.setFormat(tf)
    lst = QgsVectorTileBasicLabelingStyle(); lst.setLayerName('contours'); lst.setGeometryType(QgsWkbTypes.LineGeometry)
    lst.setFilterExpression('"elevation_m" % 50 = 0'); lst.setLabelSettings(ls)
    lb_ = QgsVectorTileBasicLabeling(); lb_.setStyles([lst]); c.setLabeling(lb_)
    zoom(c, CONTOUR_MIN); return add(c, True, parent)

# --- QLD official layers: one group each, OFF (tick to show)
QLD = {
 'qld-hazard': ('Hazard: storm tide (QLD)', {'storm_tide_high': ('pg', dict(color='220,30,30,110', outline_style='no'), 0),
     'storm_tide_medium': ('pg', dict(color='255,150,0,90', outline_style='no'), 0),
     'erosion_sea_level_rise': ('pg', dict(color='150,90,40,60', outline_color='150,90,40', outline_width='0.2'), 0)}),
 'qld-water': ('Water: bores, gauges (QLD)', {'bores': ('pt', dict(name='circle', color='0,90,200', size='1.6', outline_color='white'), 100000),
     'gauges_open': ('pt', dict(name='triangle', color='0,170,170', size='2.6', outline_color='black'), 0)}),
 'qld-surface-water': ('Water: holes, dams, lakes (QLD)', {'waterholes': ('pt', dict(name='circle', color='0,150,255', size='2.4', outline_color='black'), 0),
     'rockholes': ('pt', dict(name='diamond', color='0,150,255', size='2.6', outline_color='black'), 0),
     'farm_dams': ('pg', dict(color='80,160,255,160', outline_style='no'), 150000),
     'reservoirs': ('pg', dict(color='40,110,220,170', outline_style='no'), 0), 'lakes': ('pg', dict(color='60,130,230,150', outline_style='no'), 0)}),
 'qld-infra': ('Infrastructure (QLD)', {'emergency': ('pt', dict(name='cross_fill', color='230,0,0', size='3.2', outline_color='white'), 0),
     'community': ('pt', dict(name='square', color='120,60,180', size='2.2', outline_color='white'), 150000),
     'storage_tanks': ('pg', dict(color='90,90,90,180', outline_color='black', outline_width='0.2'), 0),
     'water_storage': ('pg', dict(color='0,90,200,180', outline_color='black', outline_width='0.2'), 0)}),
 'qld-roads': ('Roads & bridges (QLD official)', {'bridges': ('ln', dict(color='0,0,0', width='1.2'), 150000),
     'roads_tracks': ('ln', dict(color='120,70,20', width='0.35'), 150000)}),
 'qld-parks': ('Parks, state forests & trails (QLD)', {
     'protected_areas': ('pg', dict(color='40,140,40,35', outline_color='20,110,20,220', outline_width='0.5'), 0),
     'park_roads': ('ln', dict(color='110,70,20', width='0.4', line_style='dash'), 150000),
     'walking_tracks': ('ln', dict(color='200,30,30', width='0.3', line_style='dot'), 100000),
     'horse_trails': ('ln', dict(color='150,60,160', width='0.3', line_style='dot'), 100000),
     'mtb_trails': ('ln', dict(color='230,120,0', width='0.3', line_style='dot'), 100000),
     'trailbike_trails': ('ln', dict(color='90,90,90', width='0.3', line_style='dot'), 100000)}),
 'qld-flood': ('Hazard: flood (QLD FloodCheck)', {
     'flood_extreme': ('pg', dict(color='120,60,200,45', outline_color='120,60,200,160', outline_width='0.2'), 0),
     'flood_1pc_aep': ('pg', dict(color='30,90,230,70', outline_color='30,90,230,200', outline_width='0.25'), 0),
     'flood_rapid_hazard': ('pg', dict(color='0,0,0,0', outline_color='30,90,230', outline_width='0.4', outline_style='dash'), 0),
     'floodline_2011': ('pg', dict(color='0,160,230,60', outline_color='0,120,200', outline_width='0.2'), 0)}),
 'qld-fire': ('Hazard: fire history (QLD)', {
     'fire_scars': ('pg', dict(color='200,60,0,60', outline_style='no'), 0),
     'burnt_areas': ('pg', dict(color='0,0,0,0', outline_color='160,40,0,200', outline_width='0.25'), 0)}),
 'qld-contours': ('Contours 10m, index 50m (QLD LiDAR)', {}),
 'qld-cadastre': ('Property boundaries (QLD)', {'parcels': ('pg', dict(color='0,0,0,0', outline_color='200,0,200,180', outline_width='0.15'), 20000)}),
 'nsw-roads': ('Roads & fire trails (NSW official)', {'roads': ('ln', dict(color='120,70,20', width='0.35'), 150000),
     'fire_trails': ('ln', dict(color='200,60,0', width='0.35', line_style='dash'), 150000)}),
 'nsw-water': ('Water: creeks, water areas (NSW official)', {'watercourses': ('ln', dict(color='40,110,200', width='0.2'), 150000),
     'water_areas': ('pg', dict(color='170,205,240,200', outline_color='40,110,200', outline_width='0.15'), 0)}),
}
# print scales are 1:50k and 1:25k: contours must draw at both (and a bit past, for on-screen zoom-out)
CONTOUR_MIN = 60000
# statewide/national maps: big polygon/point sets with no limit get one (they drew all of QLD at once; the area
# maps are small and keep showing everything). Points 1:500k, polygons 1:1M; 0 (always) only in area maps.
_qc_group = None
STATE_MIN = {'pt': 500000, 'pg': 1000000, 'ln': 500000}
for key in reversed(list(QLD)):
    if not os.path.isfile(f(key + '.gpkg')): continue
    title, lays = QLD[key]; g = root.addGroup(title)
    for t, (k, sym, ms) in lays.items():
        if not ms and os.environ.get('LMAP_STATEWIDE'):
            ms = STATE_MIN[k]
        vec(f(key + '.gpkg'), t, t.replace('_', ' ').capitalize(), mk(k, **sym), ms, parent=g)
    if key == 'qld-contours':
        c = contour_tiles(_FILES.get('qld-contours.mbtiles'), 'Contours 10m (QLD LiDAR, fast view tiles)', g)   # statewide (43a)
        c_full = None if c else vec(f(key + '.gpkg'), 'contour_lidar_5m', 'Contours 10m', mk('ln', color='160,100,40,170', width='0.18'),
                CONTOUR_MIN, '"elevation_m" % 10 = 0', parent=g)
        if c_full:
            c = c_full
            rr = QgsRuleBasedRenderer.Rule(None)
            for ex, w, nm in (('"elevation_m" % 50 = 0', _CW[1], 'Index 50m'), ('"elevation_m" % 50 <> 0', _CW[0], '10m')):
                r = QgsRuleBasedRenderer.Rule(mk('ln', color='160,100,40,200', width=w), 0, 0, ex, nm); rr.appendChild(r)
            c.setRenderer(QgsRuleBasedRenderer(rr))
            ls = QgsPalLayerSettings(); ls.fieldName = 'format_number("elevation_m", 0)'; ls.isExpression = True
            ls.placement = Qgis.LabelPlacement.Line; ls.enabled = True
            ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
            ls.repeatDistance = 150; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
            ls.thinningSettings().setMinimumFeatureSize(15)   # mm: skip short/crowded bits
            ls.priority = 2
            tf = QgsTextFormat(); tf.setSize(7); tf.setColor(QColor(130, 80, 30)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.7); tf.setBuffer(b); ls.setFormat(tf)
            rl = QgsRuleBasedLabeling.Rule(ls); rl.setFilterExpression('"elevation_m" % 50 = 0')
            root_l = QgsRuleBasedLabeling.Rule(None); root_l.appendChild(rl)
            c.setLabeling(QgsRuleBasedLabeling(root_l)); c.setLabelsEnabled(True)
    for nd in g.findLayers():          # name labels where the data has names (infra, waterholes, gauges...)
        lyr = nd.layer()
        if not isinstance(lyr, _QVL):     # contour tiles (QgsVectorTileLayer): no fields, styled already
            continue
        flds = [x.name() for x in lyr.fields()]
        fld = next((x for x in ('estatename', 'name', 'station_name', 'site_name', 'hydroname') if x in flds), None)
        if fld and key not in ('qld-roads', 'qld-cadastre', 'qld-contours', 'nsw-roads'):
            label(lyr, fld)
            ps = lyr.labeling().settings(); ps.scaleVisibility = True; ps.maximumScale = 0
            if lyr.geometryType() == Qgis.GeometryType.Line:
                ps.placement = Qgis.LabelPlacement.Curved; ps.minimumScale = 25000
            elif lyr.geometryType() == Qgis.GeometryType.Polygon:   # park names: visible zoomed further out
                ps.minimumScale = 300000
                tf = ps.format(); tf.setColor(QColor(20, 100, 20)); f_ = tf.font(); f_.setBold(True); tf.setFont(f_); ps.setFormat(tf)
            else:
                ps.minimumScale = 25000
            lyr.setLabeling(QgsVectorLayerSimpleLabeling(ps))
    g.setItemVisibilityChecked(False); g.setExpanded(False)
    if key == 'qld-contours':
        g.setItemVisibilityChecked(True); _qc_group = g     # (the DEM block may turn it off: little LiDAR here)

# --- contours from the DEM: shown when there are no LiDAR contours (or zoomed out past them)
cd = f('contours-dem.gpkg')
if os.path.isfile(cd):
    lidar = os.path.isfile(f('qld-contours.gpkg'))
    _cb = f('contours-best.gpkg')
    if lidar and os.path.isfile(_cb):                   # LiDAR group on only if LiDAR covers most of the area
        _la = QgsVectorLayer(_cb + '|layername=lidar_area', 'probe', 'ogr')
        _ar = sum(ft.geometry().area() for ft in _la.getFeatures()) if _la.isValid() else 0
        _bx = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'), crs, p).transformBoundingBox(
            QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))                    # the area box, in the area CRS
        lidar = _ar > 0.5 * max(_bx.width() * _bx.height(), 1); del _la
    _cl = QgsVectorLayer(cd + '|layername=contours', 'probe', 'ogr')
    _vals = sorted({round(x) for x in _cl.uniqueValues(_cl.fields().indexOf('elevation_m'))}); del _cl
    _d = [b - a for a, b in zip(_vals, _vals[1:]) if b > a]
    iv = min(_d) if _d else 10
    idx = iv * 5
    _src = "30" if iv == 10 else "90"
    g = root.addGroup(f'Contours {iv}m, index {idx}m (APPROX, from {_src}m terrain, +/-{"10" if iv == 10 else "20"}m)')
    c = vec(cd, 'contours', f'Contours {iv}m (approx, {_src}m terrain)', mk('ln', color='160,100,40,170', width='0.1'), 250000, parent=g)
    if c:
        rr = QgsRuleBasedRenderer.Rule(None)
        for ex, w, nm in ((f'"elevation_m" % {idx} = 0', _CW[1], f'Index {idx}m'), (f'"elevation_m" % {idx} <> 0', _CW[0], f'{iv}m')):
            rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color='160,100,40,200', width=w), 0, 0, ex, nm))
        c.setRenderer(QgsRuleBasedRenderer(rr))
        ls = QgsPalLayerSettings(); ls.fieldName = 'format_number("elevation_m", 0)'; ls.isExpression = True
        ls.placement = Qgis.LabelPlacement.Line; ls.enabled = True
        ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
        ls.repeatDistance = 150; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
        ls.thinningSettings().setMinimumFeatureSize(15)   # mm: skip short/crowded bits
        tf = QgsTextFormat(); tf.setSize(7); tf.setColor(QColor(130, 80, 30)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.7); tf.setBuffer(b); ls.setFormat(tf)
        rl = QgsRuleBasedLabeling.Rule(ls); rl.setFilterExpression(f'"elevation_m" % {idx} = 0')
        root_l = QgsRuleBasedLabeling.Rule(None); root_l.appendChild(rl)
        c.setLabeling(QgsRuleBasedLabeling(root_l)); c.setLabelsEnabled(True)
    g.setItemVisibilityChecked(not lidar); g.setExpanded(False)   # one contour set ticked: LiDAR if it covers most
    if not lidar and _qc_group is not None:
        _qc_group.setItemVisibilityChecked(False)

# --- best contours (QLD LiDAR > LiDAR DEM > approx 30 m), stored every 10 m: two views of the same file
def contour_style(c, iv):
    idx = iv * 5; base = f'"elevation_m" % {iv} = 0'
    rr = QgsRuleBasedRenderer.Rule(None)
    for ex, w, nm in ((f'"elevation_m" % {idx} = 0', _CW[1], f'Index {idx}m'), (f'{base} AND "elevation_m" % {idx} <> 0', _CW[0], f'{iv}m')):
        rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color='160,100,40,200', width=w), 0, 0, ex, nm))
    c.setRenderer(QgsRuleBasedRenderer(rr))
    ls = QgsPalLayerSettings(); ls.fieldName = 'format_number("elevation_m", 0)'; ls.isExpression = True
    ls.placement = Qgis.LabelPlacement.Line; ls.enabled = True
    ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
    ls.repeatDistance = 150; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    ls.thinningSettings().setMinimumFeatureSize(15); ls.priority = 2
    tf = QgsTextFormat(); tf.setSize(7); tf.setColor(QColor(130, 80, 30)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.7); tf.setBuffer(b); ls.setFormat(tf)
    rl = QgsRuleBasedLabeling.Rule(ls); rl.setFilterExpression(f'"elevation_m" % {idx} = 0')
    root_l = QgsRuleBasedLabeling.Rule(None); root_l.appendChild(rl)
    c.setLabeling(QgsRuleBasedLabeling(root_l)); c.setLabelsEnabled(True)
# --- NSW Government contours (1:25k / large-scale mapping, NOT LiDAR): their own group, unticked when best is there
_nc = f('nsw-contours.gpkg')
_nt = _FILES.get('nsw-contours.mbtiles')               # statewide project: the vector-tile pyramid (43a), fast over a network share
if os.path.isfile(_nc) or (_nt and os.path.isfile(_nt)):
    _at = next((i for i, n_ in enumerate(root.children()) if n_.name().startswith('Contours')), 0)
    g = root.insertGroup(_at, 'Contours 10m, index 50m (NSW Govt mapping, not LiDAR)')
    c = contour_tiles(_nt, 'Contours 10m (NSW, fast view tiles)', g)
    if c is None and os.path.isfile(_nc):               # no tiles, or unreadable (half-copied/corrupt): the gpkg/VRT
        c = vec(_nc, 'contours', 'Contours 10m (NSW)', mk('ln'), CONTOUR_MIN, parent=g)
        if c:
            contour_style(c, 10)
    if not c:                                           # nothing readable: no empty group, other sets stay ticked
        root.removeChildNode(g)
    else:
        for gr in root.children():                      # one contour set ticked: NSW over the approx 30 m set
            if gr is not g and gr.name().startswith('Contours'):
                gr.setItemVisibilityChecked(False)
        g.setItemVisibilityChecked(not os.path.isfile(f('qld-contours.gpkg'))); g.setExpanded(False)
_cb = f('contours-best.gpkg')
if os.path.isfile(_cb):
    for gr in root.children():                          # the single-source groups stay, unticked
        if gr.name().startswith('Contours'):
            gr.setItemVisibilityChecked(False)
    _at = next((i for i, n_ in enumerate(root.children()) if n_.name().startswith('Contours')), 0)
    _pr = QgsVectorLayer(_cb + '|layername=contours', 'probe', 'ogr')
    _ss = set(_pr.uniqueValues(_pr.fields().indexOf('src'))) if _pr.isValid() and _pr.fields().indexOf('src') >= 0 else set(); del _pr
    _t = [t for k, t in (('lidar', 'LiDAR where flown'), ('lidar-dem', None), ('nsw', 'NSW Govt mapping'),
                         ('approx', 'APPROX +/-10m elsewhere')) if k in _ss and t]
    if 'lidar-dem' in _ss and 'lidar' not in _ss:
        _t.insert(0, 'LiDAR where flown')
    g = root.insertGroup(_at, f"Contours ({', '.join(_t) or 'none'})")   # same draw level as before
    for iv, on in ((10, True), (20, False)):
        c = vec(_cb, 'contours', f'Contours {iv}m, index {iv * 5}m', mk('ln'), CONTOUR_MIN,
                f'"elevation_m" % {iv} = 0', parent=g, on=on)
        if c:
            contour_style(c, iv)
    g.setIsMutuallyExclusive(True, 0); g.setItemVisibilityChecked(True); g.setExpanded(True)   # 10 m OR 20 m

# --- spot heights: DEM summits (rank 1 always, rank 2 zoomed in) + named OSM peaks with their height
sh = f('spot-heights.gpkg')
pk_group = root.insertGroup(0, 'Spot heights') if os.path.isfile(sh) else None
if pk_group:
    v = vec(sh, 'spot_heights', 'Spot heights (terrain)', mk('pt', peak=True, name='triangle', color='60,30,10', size='1.8'), 0, parent=pk_group)
    if v:
        rr = QgsRuleBasedRenderer.Rule(None)
        for ex, sz, ms, nm in (('"rank" = 1', '2.0', 0, 'Highest'), ('"rank" = 2', '1.5', 60000, 'Other summits')):
            rr.appendChild(QgsRuleBasedRenderer.Rule(mk('pt', peak=True, name='triangle', color='60,30,10', size=sz), 0, ms, ex, nm))
        v.setRenderer(QgsRuleBasedRenderer(rr))
        rl_root = QgsRuleBasedLabeling.Rule(None)
        for ex, ms in (('"rank" = 1', 0), ('"rank" = 2', 60000)):
            ls = QgsPalLayerSettings(); ls.fieldName = 'elevation_m'; ls.enabled = True
            ls.placement = Qgis.LabelPlacement.AroundPoint; ls.dist = 0.8
            tf = sty_tf(QgsTextFormat()); tf.setColor(QColor(60, 30, 10)); ls.setFormat(tf)
            r_ = QgsRuleBasedLabeling.Rule(ls, 0, ms); r_.setFilterExpression(ex); rl_root.appendChild(r_)
        v.setLabeling(QgsRuleBasedLabeling(rl_root)); v.setLabelsEnabled(True)
    # OSM often maps one peak twice (same name, metres apart): keep the lowest fid per name within ~2 km
    _PK = "other_tags LIKE '%\"natural\"=>\"peak\"%'"
    _PEAK_DEDUP = (" AND fid NOT IN (SELECT b.fid FROM points a, points b, rtree_points_geom ra, rtree_points_geom rb"
                   " WHERE ra.id = a.fid AND rb.id = b.fid AND a.name = b.name AND a.fid < b.fid AND a." + _PK +
                   " AND b." + _PK + " AND abs(ra.minx - rb.minx) < 0.02 AND abs(ra.miny - rb.miny) < 0.02)")
    # a peak named like a place within ~1 km (e.g. a city hill): the place carries the name, the spot height the
    # triangle + height -> one name, one triangle, nothing printed over the summit contours
    # per peak (correlated, rtree box first): cost = one small lookup per named peak, not a statewide self-join
    _PEAK_PLACE = (" AND NOT EXISTS (SELECT 1 FROM rtree_points_geom ra, rtree_points_geom rb, points b"
                   " WHERE ra.id = points.fid AND rb.minx > ra.minx - 0.01 AND rb.maxx < ra.maxx + 0.01"
                   " AND rb.miny > ra.miny - 0.01 AND rb.maxy < ra.maxy + 0.01 AND b.fid = rb.id"
                   " AND b.fid <> points.fid AND b.place IS NOT NULL AND b.name = points.name)")
    if os.path.isfile(f('osm.gpkg')):
        _pp = QgsVectorLayer(f('osm.gpkg') + '|layername=points', 'probe', 'ogr'); _full = _pp.isValid(); del _pp
        if _full:
            op = vec(f('osm.gpkg'), 'points', 'Named peaks (OSM)', mk('pt', peak=True, name='triangle', color='0,0,0', size='2.2'), 0,
                     "\"other_tags\" LIKE '%\"natural\"=>\"peak\"%' AND \"name\" IS NOT NULL" + _PEAK_DEDUP + _PEAK_PLACE, parent=pk_group)
            if op:
                ls = QgsPalLayerSettings(); ls.isExpression = True; ls.enabled = True
                ls.fieldName = "\"name\" || coalesce('\\n' || regexp_substr(\"other_tags\", '\"ele\"=>\"([0-9.]+)'), '')"
                ls.placement = Qgis.LabelPlacement.AroundPoint; ls.dist = 0.8
                ls.setFormat(sty_tf(QgsTextFormat(), bold=True))
                op.setLabeling(QgsVectorLayerSimpleLabeling(ls)); op.setLabelsEnabled(True)
    pk_group.setExpanded(False)                      # ON by default: summits are core topo info

# --- MGRS grid (top of the stack): 1 km thin, 10 km bold, 100 km heavy; square IDs + 2-digit numbers
gp = f('grid.gpkg')
if os.path.isfile(gp):
    g = root.insertGroup(0, 'MGRS grid (WGS84) - NOT the MGA numbers in the status bar')
    gl = vec(gp, 'grid_lines', 'Grid lines', mk('ln', color='0,0,0'), parent=g)
    if gl:
        rr = QgsRuleBasedRenderer.Rule(None)
        for nm, ex, w, col, ms in (('100 km', "\"kind\"='100km'", '0.6', '0,0,0,230', 0),
                                    ('10 km', "\"kind\"='10km'", '0.35', '0,0,0,200', 0),
                                    ('1 km', "\"kind\"='1km'", '0.12', '0,0,0,170', 100000)):
            rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color=col, width=w), 0, ms, ex, nm))
        gl.setRenderer(QgsRuleBasedRenderer(rr))
        ls = QgsPalLayerSettings(); ls.fieldName = 'label'; ls.enabled = True
        ls.placement = Qgis.LabelPlacement.Line
        ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
        ls.repeatDistance = 80; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
        ls.scaleVisibility = True; ls.maximumScale = 0; ls.minimumScale = 60000
        tf = QgsTextFormat(); tf.setSize(7); b = tf.buffer(); b.setEnabled(True); b.setSize(0.8); tf.setBuffer(b); ls.setFormat(tf)
        gl.setLabeling(QgsVectorLayerSimpleLabeling(ls)); gl.setLabelsEnabled(True)
    sq = vec(gp, 'grid_squares', '100 km square ID', mk('pt', name='circle', size='0'), parent=g)
    if sq:
        ls = QgsPalLayerSettings(); ls.fieldName = 'label'; ls.enabled = True
        tf = QgsTextFormat(); tf.setSize(14); f_ = tf.font(); f_.setBold(True); tf.setFont(f_)
        tf.setColor(QColor(0, 0, 0, 150)); b = tf.buffer(); b.setEnabled(True); b.setSize(1); tf.setBuffer(b); ls.setFormat(tf)
        sq.setLabeling(QgsVectorLayerSimpleLabeling(ls)); sq.setLabelsEnabled(True)
    g.setItemVisibilityChecked(False); g.setExpanded(False)

# --- national contours (australia.qgz): 20 m, index 100 m, from the 30 m DEM; under the QLD LiDAR group, zoomed in only
_ac = _FILES.get('contours-30m.gpkg', '')
if _ac and os.path.isfile(_ac):
    g = root.addGroup('Contours 20m, index 100m (APPROX, from 30m terrain, +/-20m)')
    c = vec(_ac, 'contours', 'Contours 20m (30m terrain)', mk('ln', color='160,100,40,170', width='0.1'), CONTOUR_MIN, parent=g)
    if c:
        rr = QgsRuleBasedRenderer.Rule(None)
        for ex, w, nm in (('"elevation_m" % 100 = 0', _CW[1], 'Index 100m'), ('"elevation_m" % 100 <> 0', _CW[0], '20m')):
            rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color='160,100,40,190', width=w), 0, 0, ex, nm))
        c.setRenderer(QgsRuleBasedRenderer(rr))
        ls = QgsPalLayerSettings(); ls.fieldName = 'format_number("elevation_m", 0)'; ls.isExpression = True
        ls.placement = Qgis.LabelPlacement.Line; ls.enabled = True
        ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
        ls.repeatDistance = 150; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
        ls.thinningSettings().setMinimumFeatureSize(15); ls.priority = 2
        tf = QgsTextFormat(); tf.setSize(7); tf.setColor(QColor(130, 80, 30)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.7)
        tf.setBuffer(b); ls.setFormat(tf)
        rl = QgsRuleBasedLabeling.Rule(ls); rl.setFilterExpression('"elevation_m" % 100 = 0')
        root_l = QgsRuleBasedLabeling.Rule(None); root_l.appendChild(rl)
        c.setLabeling(QgsRuleBasedLabeling(root_l)); c.setLabelsEnabled(True)
    # QLD LiDAR contours (10 m, exact) are ticked where present: these approximate ones start unticked then
    g.setItemVisibilityChecked(not os.path.isfile(f('qld-contours.gpkg'))); g.setExpanded(False)

# --- GA map sheets (australia.qgz): `lmap sheets get` map layers + the sheet index outlines (off). Order in the
# group = drawing order: outlines, then 1:50k, 1:100k, 1:250k (the most detailed sheet on top where they overlap)
_sd = _FILES.get('sheets', '')
if _sd and os.path.isdir(_sd):
    _sl = [(sc, t) for sc in ('50k', '100k', '250k') for t in sorted(glob.glob(os.path.join(_sd, sc, '*.cog.tif')))]
    _six = os.path.join(_sd, 'index.gpkg')
    if _sl or os.path.isfile(_six):
        g = root.insertGroup(0, 'Map sheets (GA)'); g.setItemVisibilityChecked(False); g.setExpanded(False)
        if os.path.isfile(_six):                        # outlines first in the group = drawn above the sheets
            for sc, col in (('250k', '200,0,0,220'), ('100k', '0,90,200,200'), ('50k', '0,140,60,200')):
                v = vec(_six, 's' + sc, f'Sheet index 1:{sc[:-1]} 000', mk('pg', color='0,0,0,0', outline_color=col,
                        outline_width='0.4' if sc == '250k' else '0.25'), 0, parent=g, on=False)
                if v:
                    ls = QgsPalLayerSettings(); ls.fieldName = "concat(\"number\", '  ', \"name\", '  (', coalesce(\"year\", '?'), ')')"
                    ls.isExpression = True; ls.enabled = True; ls.placement = Qgis.LabelPlacement.OverPoint
                    tf = QgsTextFormat(); tf.setSize(8); tf.setColor(QColor(*map(int, col.split(',')[:3])))
                    b = tf.buffer(); b.setEnabled(True); b.setSize(0.8); tf.setBuffer(b); ls.setFormat(tf)
                    v.setLabeling(QgsVectorLayerSimpleLabeling(ls)); v.setLabelsEnabled(True)
        for sc, t in _sl:                               # addLayer appends: below the outlines
            nm = os.path.basename(t)[:-8].replace('_', ' ')
            lab = (f'AUSTopo 1:250k {nm} (2025, GDA2020)' if sc == '250k' else
                   f'HISTORICAL 1:{sc[:-1]} 000 {nm} - ~20 m off, printed grid ~200 m off: DO NOT NAVIGATE BY IT')
            r = QgsRasterLayer(t, lab, 'gdal')
            if r.isValid():
                add(r, on=(sc == '250k'), parent=g)

# --- national MGRS grid (australia.qgz): 3 tables so zoomed out never reads the 1 km lines
gp = _FILES.get('mgrs-grid.gpkg', '')
if gp and os.path.isfile(gp):
    g = root.insertGroup(0, 'MGRS grid (WGS84) - NOT the MGA numbers in the status bar')
    def _gl(tbl, nm, w, col, minscale, lab_scale, lab_size):
        v = vec(gp, tbl, nm, mk('ln', color=col, width=w), minscale, parent=g)
        if v and lab_scale:
            ls = QgsPalLayerSettings(); ls.fieldName = 'label'; ls.enabled = True
            ls.placement = Qgis.LabelPlacement.Line
            ls.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
            ls.repeatDistance = 80; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
            ls.scaleVisibility = True; ls.maximumScale = 0; ls.minimumScale = lab_scale
            tf = QgsTextFormat(); tf.setSize(lab_size); b = tf.buffer(); b.setEnabled(True); b.setSize(0.8); tf.setBuffer(b)
            ls.setFormat(tf); v.setLabeling(QgsVectorLayerSimpleLabeling(ls)); v.setLabelsEnabled(True)
        return v
    z100 = _gl('grid_100km', '100 km lines + zone edges', '0.6', '0,0,0,230', 0, 0, 0)
    if z100:                                         # zone edges dashed + named ('zone 54 | 55')
        rr = QgsRuleBasedRenderer.Rule(None)
        rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color='0,0,0,230', width='0.6'), 0, 0, "\"kind\"='100km'", '100 km'))
        rr.appendChild(QgsRuleBasedRenderer.Rule(mk('ln', color='160,0,0,230', width='0.8', line_style='dash'), 0, 0,
                                                 "\"kind\"='zone'", 'UTM zone edge'))
        z100.setRenderer(QgsRuleBasedRenderer(rr))
        ls = QgsPalLayerSettings(); ls.fieldName = 'label'; ls.enabled = True   # 'zone 54 | 55' along each zone edge
        ls.placement = Qgis.LabelPlacement.Line; ls.repeatDistance = 120; ls.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
        tf = QgsTextFormat(); tf.setSize(8); tf.setColor(QColor(160, 0, 0)); b = tf.buffer(); b.setEnabled(True); b.setSize(0.8)
        tf.setBuffer(b); ls.setFormat(tf)
        lr = QgsRuleBasedLabeling.Rule(None); lr.appendChild(QgsRuleBasedLabeling.Rule(ls, 0, 0, "\"kind\"='zone'", 'zone edge'))
        z100.setLabeling(QgsRuleBasedLabeling(lr)); z100.setLabelsEnabled(True)
    _gl('grid_10km', '10 km lines (below 1:2M)', '0.35', '0,0,0,200', 2000000, 500000, 7)
    _gl('grid_1km', '1 km lines (below 1:100k)', '0.12', '0,0,0,170', 100000, 60000, 7)
    sq = vec(gp, 'grid_squares', '100 km square ID (below 1:5M)', mk('pt', name='circle', size='0'), 5000000, parent=g)
    if sq:
        ls = QgsPalLayerSettings(); ls.fieldName = 'label'; ls.enabled = True
        tf = QgsTextFormat(); tf.setSize(14); f_ = tf.font(); f_.setBold(True); tf.setFont(f_)
        tf.setColor(QColor(0, 0, 0, 150)); b = tf.buffer(); b.setEnabled(True); b.setSize(1); tf.setBuffer(b); ls.setFormat(tf)
        sq.setLabeling(QgsVectorLayerSimpleLabeling(ls)); sq.setLabelsEnabled(True)
    g.setItemVisibilityChecked(False); g.setExpanded(False)

# --- OSM
o = f('osm.gpkg')
if os.path.isfile(o):
    _probe = QgsVectorLayer(o + '|layername=points', 'probe', 'ogr'); full = _probe.isValid(); del _probe
    pts, lns = ('points', 'lines') if full else ('places', 'roads')
    wat = 'lines' if full else 'waterways'
    pl = vec(o, pts, 'Towns/places', mk('pt', name='circle', color='black', size='1.4'), 0,
             "\"place\" IN ('city','town','village','suburb','hamlet','locality')")
    if pl: label(pl, 'name')
    if pl and os.environ.get('LMAP_STATEWIDE'):     # whole state: cities/towns always, small places zoomed in
        # two layers, not one rule-filtered one: rules only hide, so all 21k places were still read and
        # label-tested every redraw (1.8 s at full Australia). Now zoomed out reads only cities/towns.
        pl.setSubsetString("\"place\" IN ('city','town')"); pl.setName('Towns/cities')
        sp = vec(o, pts, 'Villages/localities (below 1:500k)', mk('pt', name='circle', color='black', size='1.2'), 500000,
                 "\"place\" IN ('village','suburb','hamlet','locality')")
        if sp: label(sp, 'name')
    def line_label(v, colour, size, maxscale, italic=False):
        ls = QgsPalLayerSettings(); ls.fieldName = 'name'; ls.enabled = True
        ls.placement = Qgis.LabelPlacement.Curved
        tf = QgsTextFormat(); tf.setSize(size); tf.setColor(QColor(colour))
        if italic:
            fnt = tf.font(); fnt.setItalic(True); tf.setFont(fnt)
        b = tf.buffer(); b.setEnabled(True); b.setSize(0.7); tf.setBuffer(b); ls.setFormat(tf)
        ls.scaleVisibility = True; ls.maximumScale = 0; ls.minimumScale = maxscale
        v.setLabeling(QgsVectorLayerSimpleLabeling(ls)); v.setLabelsEnabled(True)
    # roads styled by class (QTopo-ish): casing + fill, wider for higher classes
    rd = vec(o, lns, 'Roads', mk('ln', color='0,0,0'), 0, "\"highway\" IS NOT NULL")
    if rd:
        rr = QgsRuleBasedRenderer.Rule(None)
        for nm, ex, fill, w, ms in (
            ('Motorway/highway', "\"highway\" IN ('motorway','trunk','motorway_link','trunk_link')", '200,40,30', 0.9, 0),
            ('Main road', "\"highway\" IN ('primary','primary_link')", '235,110,40', 0.7, 0),
            ('Secondary', "\"highway\" IN ('secondary','secondary_link')", '245,190,60', 0.55, 500000),
            ('Minor / street', "\"highway\" IN ('tertiary','tertiary_link','unclassified','residential','living_street','service')", '255,255,255', 0.4, 60000),
            ('4WD track', "\"highway\" = 'track' AND (\"other_tags\" LIKE '%\"4wd_only\"=>\"yes\"%' OR \"other_tags\" LIKE '%\"tracktype\"=>\"grade4\"%' OR \"other_tags\" LIKE '%\"tracktype\"=>\"grade5\"%')", '4wd', 0.35, 100000),
            ('Track', "\"highway\" = 'track' AND NOT (coalesce(\"other_tags\",'') LIKE '%\"4wd_only\"=>\"yes\"%' OR coalesce(\"other_tags\",'') LIKE '%\"tracktype\"=>\"grade4\"%' OR coalesce(\"other_tags\",'') LIKE '%\"tracktype\"=>\"grade5\"%')", 'track', 0.35, 100000),
            ('Path / trail', "\"highway\" IN ('path','footway','cycleway','bridleway','steps')", '90,90,90', 0.2, 25000)):
            if fill in ('4wd', 'track'):
                r = QgsRuleBasedRenderer.Rule(track_sym(fill), 0, ms, ex, nm); rr.appendChild(r); continue
            sym = QgsLineSymbol.createSimple({'color': '90,90,90', 'width': str(w + 0.25),        # casing
                                              'capstyle': 'round', 'joinstyle': 'round'})
            inner = QgsSimpleLineSymbolLayer(QColor(*[int(x) for x in fill.split(',')]), w)
            inner.setPenCapStyle(Qt.RoundCap); inner.setPenJoinStyle(Qt.RoundJoin)
            sym.symbolLayer(0).setRenderingPass(0); inner.setRenderingPass(10 - len(rr.children()))   # fills over ALL casings;
                                                                                                         # higher class on top
            if nm.startswith(('Track', 'Path')):
                sym = QgsLineSymbol.createSimple({'color': fill, 'width': str(w), 'line_style': 'dash'})
            else:
                sym.appendSymbolLayer(inner)
            r = QgsRuleBasedRenderer.Rule(sym, 0, ms, ex, nm); rr.appendChild(r)
        for r in rr.children():                        # tracks/paths: over road casings (no casing of their own)
            if r.symbol() is not None and r.label().startswith(('Track', '4WD', 'Path')):
                for k in range(r.symbol().symbolLayerCount()):
                    r.symbol().symbolLayer(k).setRenderingPass(1)
        rd.setRenderer(QgsRuleBasedRenderer(rr)); rd.renderer().setUsingSymbolLevels(True)   # segment joins: no casing blocks
        line_label(rd, '#333333', 7, 15000)
        if os.environ.get('LMAP_STATEWIDE'):          # 3.5M roads: read them all only when zoomed in
            # (below 1:150k; Sydney 1:370k spent 16 s here). Further out: main-roads.gpkg (motorway..secondary;
            # the Secondary rule stops them past 1:500k, as before)
            zoom(rd, 150000); rd.setName('Roads (all, below 1:150k)')
            mr = vec(f('main-roads.gpkg'), 'main_roads', 'Main roads', mk('ln', color='0,0,0'), 0)
            if mr:
                mr.setRenderer(QgsRuleBasedRenderer(rr.clone())); mr.setScaleBasedVisibility(True); mr.setMaximumScale(150000)
                ms_ = vec(f('main-roads.gpkg'), 'main_roads_small', 'Main roads (overview)', mk('ln', color='0,0,0'), 0)
                if ms_:                                # merged copy for whole-country views (au: 0.4 s vs 1.6-3.7 s)
                    mr.setMinimumScale(3000000); mr.setName('Main roads (1:150k-1:3M)')
                    ms_.setRenderer(QgsRuleBasedRenderer(rr.clone())); ms_.setScaleBasedVisibility(True)
                    ms_.setMaximumScale(3000000); ms_.setMinimumScale(0)
    rv = vec(o, wat, 'Rivers/creeks', mk('ln', color='60,120,210', width='0.35'), 300000,
             "\"waterway\" IN ('river','stream','canal')")
    if rv:                                           # style rows: solid = flows all year, dashed = OSM intermittent
        _rr = QgsRuleBasedRenderer(sty_ln('watercourse')); _rt = _rr.rootRule(); _rt.removeChildAt(0)
        if 'other_tags' in rv.fields().names():
            _rt.appendChild(QgsRuleBasedRenderer.Rule(sty_ln('watercourse_dry'), 0, 0,
                                                      "coalesce(\"other_tags\", '') LIKE '%\"intermittent\"=>\"yes\"%'",
                                                      sty_name('watercourse_dry')))
        _rt.appendChild(QgsRuleBasedRenderer.Rule(sty_ln('watercourse'), 0, 0, 'ELSE', sty_name('watercourse')))
        rv.setRenderer(_rr)
        line_label(rv, '#2a5db0', 7.5, 40000, italic=True)
    if full:
        # field features (OSM): water sources, shelter, crossings, gates, power lines - off by default
        fg = root.insertGroup(0, 'Field features (OSM)')
        T = lambda k, v: f"\"other_tags\" LIKE '%\"{k}\"=>\"{v}\"%'"
        for nm, lay, where, sym, ms in (
            ('Water: drinking water / tap', 'points', f"{T('amenity','drinking_water')} OR \"man_made\" = 'water_tap'",
             mk('pt', name='circle', color='0,120,255', size='2.6', outline_color='white'), 0),
            ('Water: spring', 'points', T('natural', 'spring'), mk('pt', name='circle', color='0,200,255', size='2.6', outline_color='black'), 0),
            ('Water: well / bore / tank', 'points', "\"man_made\" IN ('water_well','water_tower','storage_tank')",
             mk('pt', name='square', color='0,90,200', size='2.2', outline_color='white'), 0),
            ('Shelter / hut', 'points', f"{T('amenity','shelter')} OR {T('tourism','wilderness_hut')} OR {T('tourism','alpine_hut')}",
             mk('pt', name='triangle', color='120,60,20', size='2.8', outline_color='white'), 0),
            ('Ford', 'points', T('ford', 'yes'), mk('pt', name='diamond', color='0,150,200', size='2.6', outline_color='black'), 0),
            ('Gate', 'points', "\"barrier\" IN ('gate','lift_gate','swing_gate','cattle_grid')",
             mk('pt', name='line', color='0,0,0', size='2.4', outline_width='0.5'), 50000),
            ('Ford (on road)', 'lines', T('ford', 'yes'), mk('ln', color='0,150,200', width='1.2'), 0),
            ('Power line', 'lines', f"{T('power','line')} OR {T('power','minor_line')}",
             sty_ln('power'), 150000),
            ('Power tower', 'points', T('power', 'tower'), mk('pt', name='x', color='200,0,0', outline_color='200,0,0', outline_width='0.3'), 40000)):
            v = vec(o, lay, nm, sym, ms, where, parent=fg, on=True)
            if v and v.featureCount() == 0:
                fg.removeLayer(v); QgsProject.instance().removeMapLayer(v.id())
            elif v and lay == 'points' and nm.startswith(('Water', 'Shelter')):
                label(v, 'name')
        fg.setItemVisibilityChecked(False); fg.setExpanded(False)

# --- rasters
def true_colour(r):
    # already 8-bit display colour: no per-band stretch (that tints each area differently)
    rn = QgsMultiBandColorRenderer(r.dataProvider(), 1, 2, 3)
    for b in ('setRedContrastEnhancement', 'setGreenContrastEnhancement', 'setBlueContrastEnhancement'):
        ce = QgsContrastEnhancement(Qgis.DataType.Byte); ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.NoEnhancement)
        getattr(rn, b)(ce)
    r.setRenderer(rn); return r
def black_clear(r):
    # only 0,0,0 (all 3 bands) is "no data". A per-band 0 also hid real dark pixels (2025 Sentinel-2 vegetation
    # and water often have blue=0, e.g. 4,25,0): white specks everywhere (2026-10-15). The files have masks,
    # but PDF export ignores them and QGIS doesn't honour them at every zoom (black boxes) -> keep this.
    tr = QgsRasterTransparency()
    tr.setTransparentThreeValuePixelList([QgsRasterTransparency.TransparentThreeValuePixel(0, 0, 0, 0.0)])   # 4th = remaining opacity
    r.renderer().setRasterTransparency(tr); return r
def hill(path, nm):
    r = QgsRasterLayer(path, nm, 'gdal')
    hs = QgsHillshadeRenderer(r.dataProvider(), 1, 315, 45); hs.setMultiDirectional(True)
    if r.crs().isGeographic():                   # statewide DEM is in degrees: heights in m -> scale to degrees
        hs.setZFactor(1 / 111320.0)               # (else every 1 m bump reads as a cliff: black/white checkerboard)
    r.setRenderer(hs); r.renderer().setOpacity(0.45); r.setBlendMode(QPainter.CompositionMode_Multiply)
    r.resampleFilter().setZoomedInResampler(QgsCubicRasterResampler()); return add(r)
if os.environ.get('LMAP_HILLSHADE') and os.path.isfile(os.environ['LMAP_HILLSHADE']):
    # statewide: pre-made shading (gdaldem). Live shading of the lat/long 30m DEM in Web Mercator drew a stair-step grid
    _h = QgsRasterLayer(os.environ['LMAP_HILLSHADE'], 'Hillshade', 'gdal')
    _h.setRenderer(QgsSingleBandGrayRenderer(_h.dataProvider(), 1)); _h.renderer().setOpacity(0.45)
    _h.setBlendMode(QPainter.CompositionMode_Multiply); _h.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
    _h.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler()); add(_h)
elif os.path.isfile(f('dem.tif')):
    hill(f('dem.tif'), 'Hillshade')
if os.path.isfile(f('aerial.tif')):
    a = QgsRasterLayer(f('aerial.tif'), layer_name('aerial.tif', 'Aerial 50cm'), 'gdal')
    add(a); zoom(a, 60000); black_clear(true_colour(a))        # 0,0,0 = no coverage
for _sc, _lab in (('250k', 'GA 1:250 000 sheet (AUSTopo 2025, overview only: ~50-100 m)'),):
    if os.path.isfile(f(f'sheet-{_sc}.tif')):   # off: tick to show (lmap area create)
        true_colour(add(QgsRasterLayer(f(f'sheet-{_sc}.tif'), _lab, 'gdal'), on=False))
if os.path.isfile(f('topo.tif')):     # off under satellite; on when there's no imagery
    true_colour(add(QgsRasterLayer(f('topo.tif'), 'QLD Topo (Govt basemap)', 'gdal'), on=not os.path.isfile(f('sat.tif'))))
# night lights (NASA Black Marble): off, tick to show; screen blend = lights glow over the map below
if os.environ.get('LMAP_LIGHTS') and os.path.isfile(os.environ['LMAP_LIGHTS']):
    _l = true_colour(add(QgsRasterLayer(os.environ['LMAP_LIGHTS'], 'Night lights 2016 (NASA, 500m)', 'gdal'), on=False))
    _l.setBlendMode(QPainter.CompositionMode_Screen)
# national project: finer imagery stacked above the national layer (state 10m, ...), each from its own zoom
for _d in json.loads(os.environ.get('LMAP_DETAIL') or '[]'):
    if os.path.isfile(_d['file']):
        _r = black_clear(true_colour(add(QgsRasterLayer(_d['file'], _d['name'], 'gdal')))); zoom(_r, _d.get('minscale', 0))
if os.path.isfile(f('sat.tif')):                   # sat-even.tif: same imagery, water colour evened (photo sheets)
    _sf = f('sat-even.tif') if os.path.isfile(f('sat-even.tif')) else f('sat.tif')
    _s = black_clear(true_colour(add(QgsRasterLayer(_sf, layer_name('sat.tif', 'Satellite'), 'gdal'))))
if os.path.isfile(f('dem.tif')):
    r = QgsRasterLayer(f('dem.tif'), 'Elevation (coloured)', 'gdal')
    fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Interpolated)
    if os.environ.get('LMAP_DEM_RANGE'):           # statewide: a full-res stats pass over the whole DEM would take ages
        lo, hi = [float(x) for x in os.environ['LMAP_DEM_RANGE'].split(',')]
    else:
        st = r.dataProvider().bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
        lo, hi = max(st.minimumValue, 0.0), max(st.maximumValue, 10.0)
    land = ['#a9d18e', '#d9e8a8', '#efe3a0', '#d6b46f', '#b07d4b', '#8a5a3b']   # stretched to this area's range
    items = [(-10, '#9cc3e6'), (0.5, '#9cc3e6')] + [
        (lo + max(1.0 - lo, 0) * 0 + (hi - lo) * i / (len(land) - 1) if i else max(lo, 1.0), c) for i, c in enumerate(land)]
    fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), f'{v:.0f} m') for v, c in items])
    sh = QgsRasterShader(); sh.setRasterShaderFunction(fn)
    r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh))
    add(r, on=not os.path.isfile(f('sat.tif')))       # off when there's imagery

# --- field features (OSM): water, shelter, crossings, barriers, power, services. On; small symbols, names zoomed in
if os.path.isfile(f('field.gpkg')):
    fg = root.insertGroup(0, 'Field features (OSM)')
    PT = {'drinking water': ('circle', '0,120,255', 2.6), 'water point': ('circle', '0,120,255', 2.6),
          'well / bore': ('circle', '0,90,200', 2.4), 'spring': ('circle', '0,160,220', 2.4),
          'water tank / tower': ('square', '0,90,200', 2.2), 'waterfall': ('triangle', '0,150,220', 2.2),
          'shelter / hut': ('square', '120,60,20', 2.6), 'campsite': ('triangle', '30,120,30', 2.6),
          'toilets': ('square', '120,120,120', 1.8), 'ford': ('diamond', '0,90,200', 2.6),
          'gate': ('cross_fill', '0,0,0', 2.6), 'cattle grid': ('cross', '0,0,0', 2.4),
          'power tower': ('x', '200,0,0', 1.6), 'power pole': ('x', '235,120,120', 0.8),
          'substation': ('square', '200,0,0', 2.6),
          'comms tower': ('triangle', '160,0,160', 2.6), 'helipad': ('circle', '255,255,255', 3.2),
          'airstrip': ('circle', '255,255,255', 3.2), 'fuel': ('pentagon', '230,120,0', 2.6),
          'hospital / clinic': ('cross_fill', '220,0,0', 3.0), 'pharmacy': ('cross_fill', '0,150,0', 2.4),
          'police': ('star', '0,0,160', 2.8), 'fire station': ('star', '220,60,0', 2.8),
          'lookout': ('star', '100,60,20', 2.6)}
    v = QgsVectorLayer(f('field.gpkg') + '|layername=field_points', 'Field points', 'ogr')
    if v.isValid():
        _so = ('cross', 'cross2', 'x', 'line', 'arrowhead', 'semi_arc', 'third_arc', 'quarter_arc')   # stroke-only shapes
        # one size for every symbol (style symbol_mm, set in mk); stroke-only shapes get a heavier stroke so they
        # read as big as the filled ones
        cats = [QgsRendererCategory(k, mk('pt', name=sh, color=c,
                                           outline_color=c if sh in _so else ('60,60,60' if c == '255,255,255' else '255,255,255'),
                                           outline_width='0.4' if sh in _so else '0.15'), k)
                for k, (sh, c, sz) in PT.items()]
        v.setRenderer(QgsCategorizedSymbolRenderer('kind', cats)); add(v, parent=fg)
        if os.environ.get('LMAP_STATEWIDE'):          # all of AU: 115k points, read only zoomed in
            v.setScaleBasedVisibility(True); v.setMinimumScale(500000)
        ls = QgsPalLayerSettings(); ls.isExpression = True; ls.enabled = True
        # real names only: the legend says what a symbol is. No brand names on fuel/pharmacy
        # ("Shell Aitkenvale") - clutter that changes often; the symbol stays. LMAP_BRAND_NAMES=1 shows them.
        ls.fieldName = ('"name"' if os.environ.get('LMAP_BRAND_NAMES') else
                        "CASE WHEN \"kind\" IN ('fuel', 'pharmacy') THEN NULL ELSE \"name\" END"); ls.dist = 1.0
        ls.setFormat(sty_tf(QgsTextFormat())); ls.scaleVisibility = True; ls.maximumScale = 1; ls.minimumScale = 30000
        v.setLabeling(QgsVectorLayerSimpleLabeling(ls)); v.setLabelsEnabled(True)
    LN = {'power line': 'power', 'fence': 'fence', 'pipeline': 'pipeline'}      # field kind -> style.toml line row
    v = QgsVectorLayer(f('field.gpkg') + '|layername=field_lines', 'Field lines', 'ogr')
    if v.isValid():
        if not (STY.get('line') or {}).get('pipeline', {}).get('underground'):   # buried pipes: not on the ground
            v.setSubsetString("NOT (\"kind\" = 'pipeline' AND (coalesce(\"osm_tags\", '') LIKE '%\"location\"=>\"underground\"%'"
                              " OR coalesce(\"osm_tags\", '') LIKE '%\"layer\"=>\"-%'))")
        cats = [QgsRendererCategory(k, sty_ln(c), sty_name(c)) for k, c in LN.items()]
        cats.insert(1, QgsRendererCategory('ford (line)', mk('ln', color='0,90,200', width='0.8'), 'ford (line)'))
        cats[1:1] = [QgsRendererCategory('4WD track', track_sym('4wd'), '4WD track'), QgsRendererCategory('track', track_sym('track'), 'track')]
        v.setRenderer(QgsCategorizedSymbolRenderer('kind', cats)); add(v, parent=fg)
        if os.environ.get('LMAP_STATEWIDE'):          # all of AU: 350k tracks/power lines (1:200k cold: +12 s)
            v.setScaleBasedVisibility(True); v.setMinimumScale(100000)
    fg.setExpanded(False)

# --- terrain analysis: slope bands (off) + HLS candidates (off) + viewsheds made with `lmap area viewshed`
_SLOPES = json.loads(os.environ.get('LMAP_SLOPES') or '[]')     # statewide: [{"file","name","minscale"}] coarse first
def slope_styles(r):
    """Three named styles on one integer-degree slope layer; 'walk / 4WD' is the default (Layer > Styles)."""
    def disc(items):
        fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Discrete)
        fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lb) for v, c, lb in items])
        sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
        rr = QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_); rr.setOpacity(0.55); return rr
    styles = {
        'walk / 4WD': disc([(9.5, '#00000000', '0-9: walk easy; 4WD ok'), (14.5, '#f6e27f', '10-14: walk hard; 4WD ok'),
                            (19.5, '#f0b44c', '15-19: walk hard; 4WD careful'), (24.5, '#e8822e', '20-24: scramble; 4WD careful'),
                            (29.5, '#d9442b', '25-29: scramble; 4WD straight up/down only'),
                            (90.5, '#6b0f1a', '30+: climb; no vehicle')]),
        'bushfire (AS 3959)': disc([(5.5, '#fff5d6', '0-5'), (10.5, '#fdd49e', '>5-10'), (15.5, '#fc8d59', '>10-15'),
                                    (20.5, '#d7301f', '>15-20'), (90.5, '#7f0000', '>20 (outside Method 1)')]),
    }
    fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Interpolated)
    fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), f'{v} deg') for v, c in (
        (0, '#ffffff'), (10, '#fee08b'), (20, '#fc8d59'), (30, '#d73027'), (45, '#67001f'))])
    sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
    cr = QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_); cr.setOpacity(0.55); styles['degrees (ramp)'] = cr
    sm = r.styleManager()
    for nm_, rr in styles.items():
        if nm_ != 'walk / 4WD':
            r.setRenderer(rr); sm.addStyleFromLayer(nm_)
    r.setRenderer(styles['walk / 4WD']); sm.addStyleFromLayer('walk / 4WD'); sm.setCurrentStyle('walk / 4WD')
    sm.removeStyle('default') if 'default' in sm.styles() else None
    r.dataProvider().setNoDataValue(1, 255)
tg = root.insertGroup(0, 'Terrain analysis') if (_SLOPES or glob.glob(os.path.join(d, 'viewsheds', '*.cog.tif'))
                                                 or glob.glob(os.path.join(d, 'water', '*.gpkg'))
                                                 or any(os.path.isfile(f(x)) for x in ('slope.tif', 'hls.gpkg'))) else None
if tg:
    for _s in _SLOPES[::-1]:                              # finest on top; each from its own zoom
        r = QgsRasterLayer(_s['file'], _s['name'], 'gdal')
        if r.isValid():
            slope_styles(r); zoom(r, _s.get('minscale') or 0); add(r, on=False, parent=tg)
    if os.path.isfile(f('slope.tif')) and not _SLOPES:
        r = QgsRasterLayer(f('slope.tif'), 'Slope (degrees)', 'gdal')
        fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Discrete)
        fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lb) for v, c, lb in (
            (7, '#00000000', '0-7: vehicles, landing'), (15, '#f6e27f', '7-15: 4WD, slow on foot'),
            (25, '#f0a33c', '15-25: foot only, steep'), (35, '#d9442b', '25-35: very steep, hands'),
            (90, '#6b0f1a', '35+: cliff / climbing'))])
        sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
        r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_)); r.renderer().setOpacity(0.55)
        add(r, on=False, parent=tg)
    if os.path.isfile(f('hls.gpkg')):
        v = vec(f('hls.gpkg'), 'hls', 'Landing site candidates (terrain only)',
                mk('pt', name='circle', color='255,255,255,0', outline_color='200,0,0', outline_width='0.6', size='4'),
                0, parent=tg, on=False)
        if v:
            label(v, 'size_m')
    for vs in sorted(glob.glob(os.path.join(d, 'viewsheds', '*.cog.tif'))):   # lmap terrain viewshed: count
        if vs.endswith('-angle.cog.tif'):
            continue
        va = vs[:-8] + '-angle.cog.tif'
        if os.path.isfile(va):                           # --angles: mils from the eye, down = blue, up = red
            r = QgsRasterLayer(va, 'Angle from ' + os.path.basename(vs)[:-8] + ' (mils, + up / - down)', 'gdal')
            if r.isValid():
                fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Discrete)
                fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lb) for v, c, lb in (
                    (-100, '#08519c', 'below -100'), (-30, '#3182bd', '-100 to -30'), (-10, '#9ecae1', '-30 to -10'),
                    (0, '#f7f7f7', '-10 to 0 (near level)'), (10, '#fcbba1', '0 to +10'), (30, '#fb6a4a', '+10 to +30'),
                    (1e9, '#a50f15', 'above +30'))])
                sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
                r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_)); r.renderer().setOpacity(0.6)
                add(r, on=False, parent=tg)
        r = QgsRasterLayer(vs, 'Seen from ' + os.path.basename(vs)[:-8], 'gdal')
        if not r.isValid():
            continue
        fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Discrete)
        fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lb) for v, c, lb in (
            (1.5, '#e31a1c', 'seen by 1'), (2.5, '#fd8d3c', 'seen by 2'), (3.5, '#fecc5c', 'seen by 3'),
            (254.5, '#ffffb2', 'seen by 4+'))])
        sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
        r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_)); r.renderer().setOpacity(0.5)
        add(r, on=False, parent=tg)
    for wv in sorted(glob.glob(os.path.join(d, 'water', '*.gpkg'))):    # lmap terrain water
        nm_ = os.path.basename(wv)[:-5]
        if not QgsVectorLayer(f'{wv}|layername=streams', 'x', 'ogr').isValid():   # not an lmap water file
            continue
        wg = tg.addGroup('Water flow ' + nm_); wg.setItemVisibilityChecked(False)
        wt = os.path.join(d, 'water', nm_ + '-wetness.cog.tif')
        if os.path.isfile(wt):
            r = QgsRasterLayer(wt, 'wet ground', 'gdal')
            if r.isValid():
                fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Interpolated)
                fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(1, QColor(190, 230, 255, 0), 'damp'),
                                         QgsColorRampShader.ColorRampItem(125, QColor(80, 160, 255, 140), 'wet'),
                                         QgsColorRampShader.ColorRampItem(250, QColor(0, 40, 200, 220), 'wettest')])
                sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
                r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_)); add(r, on=True, parent=wg)
        v = QgsVectorLayer(f'{wv}|layername=catchments', 'catchments', 'ogr')
        if v.isValid():
            v.setRenderer(QgsSingleSymbolRenderer(mk('pg', color='0,0,0,0', outline_color='130,60,160',
                                                     outline_width='0.35', outline_style='dash')))
            add(v, on=False, parent=wg)
        v = QgsVectorLayer(f'{wv}|layername=streams', 'flow lines', 'ogr')
        if v.isValid():
            cats = [QgsRendererCategory(c, mk('ln', color=col, width=w), lb) for c, col, w, lb in (
                ('gully', '70,140,230', '0.2', 'gully (<2 km2 upstream)'), ('creek', '30,90,220', '0.45', 'creek (2-50 km2)'),
                ('river', '0,50,180', '0.8', 'river (50+ km2)'))]
            v.setRenderer(QgsCategorizedSymbolRenderer('class', cats)); add(v, on=True, parent=wg)
        v = QgsVectorLayer(f'{wv}|layername=wet_spots', 'wet spots (dig here first)', 'ogr')
        if v.isValid():
            v.setRenderer(QgsSingleSymbolRenderer(mk('pt', name='circle', color='0,200,255', outline_color='0,0,120',
                                                     outline_width='0.4', size='2.6')))
            add(v, on=True, parent=wg); label(v, 'rank')
    for vs in sorted(glob.glob(os.path.join(d, 'viewshed-*.tif'))):
        r = QgsRasterLayer(vs, 'Seen from ' + os.path.basename(vs)[9:-4], 'gdal')
        fn = QgsColorRampShader(); fn.setColorRampType(QgsColorRampShader.Exact)
        fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(255, QColor(255, 0, 0, 110), 'visible')])
        sh_ = QgsRasterShader(); sh_.setRasterShaderFunction(fn)
        r.setRenderer(QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, sh_)); add(r, on=False, parent=tg)
    tg.setExpanded(False)

# --- '3D' map theme: same layers, bolder lines, bigger symbols and labels. The 3D view drapes this theme onto the
# terrain, so contours/creeks/names stay readable when stretched over hills. The 2D map keeps its own style.
def _bold_renderer(lyr, lw, pw):
    r = lyr.renderer().clone(); ctx = QgsRenderContext()
    for sym in r.symbols(ctx):
        for i in range(sym.symbolLayerCount()):
            sl = sym.symbolLayer(i)
            if isinstance(sl, QgsLineSymbolLayer):
                sl.setWidth(max(sl.width() * lw, 0.45))
            elif isinstance(sl, QgsMarkerSymbolLayer):
                sl.setSize(sl.size() * pw)
            elif hasattr(sl, 'strokeWidth') and hasattr(sl, 'setStrokeWidth'):
                sl.setStrokeWidth(max(sl.strokeWidth() * lw, 0.4))
    lyr.setRenderer(r)
def _big_labels(lyr, f=2.5):
    lab = lyr.labeling()
    if not lab or not lyr.labelsEnabled():
        return
    lab = lab.clone()
    def fix(ps):
        ps = QgsPalLayerSettings(ps); tf = ps.format(); tf.setSize(tf.size() * f)
        b = tf.buffer(); b.setEnabled(True); b.setSize(max(b.size() * 1.6, 1.2)); b.setColor(QColor('white')); tf.setBuffer(b)
        ps.setFormat(tf); ps.scaleVisibility = False      # 3D tiles render at their own scales: always label
        return ps
    if isinstance(lab, QgsVectorLayerSimpleLabeling):
        lab.setSettings(fix(lab.settings()))
    elif isinstance(lab, QgsRuleBasedLabeling):
        for ru in lab.rootRule().descendants():
            if ru.settings():
                ru.setSettings(fix(ru.settings()))
    lyr.setLabeling(lab)
try:
    recs = []
    for tl in root.findLayers():
        lyr = tl.layer()
        if not lyr or not tl.isVisible():
            continue
        sm = lyr.styleManager(); base = sm.currentStyle()
        rec = QgsMapThemeCollection.MapThemeLayerRecord(lyr); rec.usingCurrentStyle = True; rec.currentStyle = base
        if isinstance(lyr, _QVL) and lyr.isValid() and lyr.renderer():
            sm.addStyleFromLayer('3D'); sm.setCurrentStyle('3D')
            nm = lyr.name().lower()
            _bold_renderer(lyr, 3.0 if 'contour' in nm else 2.2, 1.8); _big_labels(lyr)
            sm.setCurrentStyle(base)                      # stores the bold look under '3D', restores 2D
            rec.currentStyle = '3D'
        recs.append(rec)
    th = QgsMapThemeCollection.MapThemeRecord(); th.setLayerRecords(recs)
    p.mapThemeCollection().insert('3D', th)
except Exception as e:
    print('3D theme failed:', e, file=sys.stderr)

# --- 3D symbols: real lines draped on the terrain (sharp at any zoom, unlike lines baked into the terrain image)
# and stand-up billboard icons for points. 2D is untouched. Done per layer by name.
try:
    from qgis._3d import (Qgs3D, QgsLine3DSymbol, QgsPoint3DSymbol, QgsVectorLayer3DRenderer,
                          QgsSimpleLineMaterialSettings)
    Qgs3D.initialize()
    KEEP3D = []      # QGIS 3.40: billboard icons are freed early unless Python keeps a ref -> p.write() segfaults
    def line3d(lyr, color, width=2.0, lift=1.0):
        sy = QgsLine3DSymbol(); sy.setRenderAsSimpleLines(True); sy.setWidth(width)
        sy.setAltitudeClamping(Qgis.AltitudeClamping.Terrain); sy.setOffset(lift)
        sy.setAltitudeBinding(Qgis.AltitudeBinding.Vertex)   # drape every vertex (default Centroid floats long lines)
        m = QgsSimpleLineMaterialSettings(); m.setAmbient(QColor(color)); sy.setMaterialSettings(m)
        lyr.setRenderer3D(QgsVectorLayer3DRenderer(sy))
    def billboard(lyr, sym2d, lift=15.0):
        sy = QgsPoint3DSymbol(); sy.setShape(Qgis.Point3DShape.Billboard)
        sy.setAltitudeClamping(Qgis.AltitudeClamping.Terrain)
        if isinstance(sym2d, QgsMarkerSymbol):
            b = sym2d.clone(); b.setSize(max(b.size() * 4, 9)); sy.setBillboardSymbol(b); KEEP3D.append(b)
        r3 = QgsVectorLayer3DRenderer(sy); KEEP3D.extend([sy, r3]); lyr.setRenderer3D(r3)
    L3 = {'contour': ('#9a5b2a', 1.4, 0.5), 'creek': ('#2f7fe0', 2.2, 1.0), 'river': ('#2f7fe0', 3.0, 1.0),
          'watercourse': ('#2f7fe0', 2.0, 1.0), 'road': ('#ffffff', 3.0, 1.5), 'track': ('#8a5a2b', 2.0, 1.5),
          'trail': ('#6b3f1f', 1.8, 1.5), 'path': ('#6b3f1f', 1.8, 1.5), 'power': ('#d00000', 1.6, 12.0),
          'field lines': ('#8a5a2b', 2.0, 1.5), 'park': ('#2e8b2e', 2.0, 1.0), 'boundar': ('#555555', 1.4, 1.0),
          'grid': ('#000000', 1.0, 2.0)}
    n3 = 0
    for tl in root.findLayers():
        lyr = tl.layer()
        if not isinstance(lyr, _QVL) or not lyr.isValid():
            continue
        nm = lyr.name().lower(); gt = lyr.geometryType()
        if gt == Qgis.GeometryType.Line:
            # lines stay painted into the terrain image (continuous, labelled). Real 3D lines broke up on slopes:
            # straight segments between vertices cut through the curved ground. line3d() kept for later use.
            continue
        elif gt == Qgis.GeometryType.Point and False:   # billboards off: points+names are draped (see '3D' theme)
            r2 = lyr.renderer()
            if isinstance(r2, QgsCategorizedSymbolRenderer):     # one 3D rule per category: same icon as 2D
                from qgis._3d import QgsRuleBased3DRenderer
                rr = QgsRuleBased3DRenderer.Rule(None)
                for c in r2.categories():
                    if not isinstance(c.symbol(), QgsMarkerSymbol):
                        continue
                    sy = QgsPoint3DSymbol(); sy.setShape(Qgis.Point3DShape.Billboard)
                    sy.setAltitudeClamping(Qgis.AltitudeClamping.Terrain)
                    b = c.symbol().clone(); b.setSize(max(b.size() * 4, 9)); sy.setBillboardSymbol(b)
                    val = str(c.value()).replace("'", "''")
                    ru = QgsRuleBased3DRenderer.Rule(sy, f'"{r2.classAttribute()}" = \'{val}\''); rr.appendChild(ru)
                    KEEP3D += [b, sy, ru]
                r3 = QgsRuleBased3DRenderer(rr); KEEP3D += [rr, r3]; lyr.setRenderer3D(r3); n3 += 1
                continue
            sym = r2.symbol() if hasattr(r2, 'symbol') and r2.symbol() else None
            if sym is None and hasattr(r2, 'rootRule') and r2.rootRule().children():
                sym = r2.rootRule().children()[0].symbol()
            billboard(lyr, sym); n3 += 1
except ImportError:
    pass

# --- 3D: project terrain = this area's DEM, so View > 3D Map Views > New opens with relief draped.
# Vertical scale from the relief: flat country needs more exaggeration to read (2.5x), mountains less (1.5x).
dem_l = next((l for l in p.mapLayers().values() if l.source().endswith('dem.tif') and l.name() == 'Hillshade'), None)
if dem_l and os.path.isfile(f('dem-3d.tif')):       # smooth 3D-only DEM: in the project but not in the layer tree
    d3 = QgsRasterLayer(f('dem-3d.tif'), 'Terrain (3D)', 'gdal')
    if d3.isValid():
        p.addMapLayer(d3, False); dem_l = d3
if os.environ.get('LMAP_NO3D'):                     # statewide: no 3D terrain/view (whole-state stats + tiles = too slow)
    dem_l = None
if os.environ.get('LMAP_3D_DEM'):                   # big maps: named DEM + fixed scale (no whole-file stats pass)
    dem_l = QgsRasterLayer(os.environ['LMAP_3D_DEM'], 'Terrain (3D)', 'gdal')
    assert dem_l.isValid(), os.environ['LMAP_3D_DEM']
    p.addMapLayer(dem_l, False)
    tp = QgsRasterDemTerrainProvider(); tp.setLayer(dem_l); tp.setScale(float(os.environ.get('LMAP_3D_SCALE', '1.5')))
    p.elevationProperties().setTerrainProvider(tp)
    vscale, dem_id = tp.scale(), dem_l.id()
elif dem_l:
    tp = QgsRasterDemTerrainProvider(); tp.setLayer(dem_l)
    st = dem_l.dataProvider().bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
    relief = max(st.maximumValue - max(st.minimumValue, 0), 1)
    tp.setScale(2.5 if relief < 150 else 2.0 if relief < 500 else 1.5)
    p.elevationProperties().setTerrainProvider(tp)
    vscale, dem_id = tp.scale(), dem_l.id()
else:
    vscale, dem_id = 1.0, None

# --- pin datum transforms (no popup), open on the box
tc = p.transformContext()
for s in sorted({l.crs().authid() for l in p.mapLayers().values()} - {crs.authid(), ''}):
    src = QgsCoordinateReferenceSystem(s)
    ops = [x for x in QgsDatumTransform.operations(src, crs) if x.isAvailable]
    if ops:
        tc.addCoordinateOperation(src, crs, sorted(ops, key=lambda x: x.accuracy if x.accuracy > 0 else 99)[0].proj, True)
p.setTransformContext(tc)
ds = p.displaySettings(); ds.setCoordinateType(Qgis.CoordinateDisplayType.CustomCrs); ds.setCoordinateCustomCrs(crs)
tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:7844'), crs, p)
v = tr.transformBoundingBox(QgsRectangle(*bbox))
p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(v, crs))
# "zoom full" = the area box, saved: else QGIS asks every layer for its extent (41 layers, 38 GB aerial, network-share VRTs:
# hung 75 s+ on australia.qgz)
p.viewSettings().setPresetFullExtent(QgsReferencedRectangle(v, crs))
def _collapse(node):                                # every group + layer legend folded on open, the contour 10/20 m
    for c in node.children():                       # choice too (tidy layer list)
        c.setExpanded(False); _collapse(c)
# --- imported layers (lmap import add): clipped into imp-<id>.gpkg|.tif, on top in their own group. Added after
# the 3D section: the 3D billboard pass over point layers segfaulted p.write() on them (QGIS 3.40)
_imp = sorted(x for x in os.listdir(d) if x.startswith('imp-') and x.endswith(('.gpkg', '.tif')))
if _imp:
    _ig = root.insertGroup(0, 'Imported'); _IMPL = []
    for _x in _imp:
        if _x.endswith('.tif'):
            add(QgsRasterLayer(f(_x), _x[4:-4], 'gdal'), parent=_ig)
            continue
        from osgeo import ogr as _ogr
        _src_ds = _ogr.Open(f(_x))
        if _src_ds is None:
            continue
        _subs = [_src_ds.GetLayer(i).GetName() for i in range(_src_ds.GetLayerCount())]; _src_ds = None
        for _nm in _subs:
            _v = QgsVectorLayer(f'{f(_x)}|layername={_nm}', f'{_x[4:-5]} ({_nm})', 'ogr')
            if _v.isValid() and _v.featureCount() and _nm not in ('track_points', 'route_points'):   # GPX vertices: noise
                _gt = _v.geometryType()                    # one bold magenta look: user overlays stand out on any sheet
                if _gt == QgsWkbTypes.PointGeometry:
                    _v.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name': 'circle', 'color': '230,0,180', 'size': '2.2',
                                                                          'outline_color': 'white', 'outline_width': '0.3'}))
                elif _gt == QgsWkbTypes.LineGeometry:
                    _v.renderer().setSymbol(QgsLineSymbol.createSimple({'color': '230,0,180', 'width': '0.6'}))
                elif _gt == QgsWkbTypes.PolygonGeometry:
                    _v.renderer().setSymbol(QgsFillSymbol.createSimple({'color': '230,0,180,40', 'outline_color': '230,0,180',
                                                                        'outline_width': '0.5'}))
                _fn = next((x for x in ('name', 'Name', 'NAME', 'title') if _v.fields().indexOf(x) >= 0), None)
                if _fn:
                    _ls = QgsPalLayerSettings(); _ls.fieldName = _fn; _tf = QgsTextFormat(); _tf.setSize(8)
                    _tf.setColor(QColor(150, 0, 120)); _bf = QgsTextBufferSettings(); _bf.setEnabled(True); _bf.setSize(0.8)
                    _tf.setBuffer(_bf); _ls.setFormat(_tf)
                    _v.setLabeling(QgsVectorLayerSimpleLabeling(_ls)); _v.setLabelsEnabled(True)
                _IMPL.append(add(_v, parent=_ig))

# contour groups are the lowest line work: move them just above the first raster (under roads, tracks, water, points)
_root = p.layerTreeRoot()
for _cg in [n_ for n_ in _root.children() if QgsLayerTree.isGroup(n_) and n_.name().startswith('Contours')]:
    _kids = _root.children()
    _ri = next((i for i, n_ in enumerate(_kids) if QgsLayerTree.isLayer(n_) and n_.layer() is not None
                and n_.layer().type() == QgsMapLayerType.RasterLayer), len(_kids))
    _cl = _cg.clone(); _root.insertChildNode(_ri, _cl); _root.removeChildNode(_cg)
# phone maps (lmap area phone-map): pre-drawn tiles, fast to pan at any zoom. Bottom of the list, off: tick one
# as a quick base map. Print/phone-map never draw them (PICK_PY drops phone-*.mbtiles).
_pm = sorted(x for x in os.listdir(d) if re.fullmatch(r'phone-(topo|photo)(-[^/]+)?\.mbtiles', x))
if _pm:
    _pg = _root.addGroup('Phone maps (fast base, tick one)')
    for _x in _pm:
        _r = QgsRasterLayer(f(_x), 'Phone ' + _x[len('phone-'):-len('.mbtiles')].replace('-', ' '), 'gdal')
        if _r.isValid():
            add(_r, on=False, parent=_pg)
    _pg.setItemVisibilityChecked(False)
_collapse(p.layerTreeRoot())
# write the .qgs IN the area folder so QGIS stores paths relative to it ('./dem.tif'); writing it in
# scratch made them '../../../../<root>/...' (relative to scratch) -> broken when opened
p.writeEntry('Paths', '/Absolute', False)
qgs = os.path.join(d, f'.{name}.build.qgs')
for _t in (0, 5, 20, 60):                      # network-share write blip: retry, don't fail the area
    time.sleep(_t)
    if p.write(qgs):
        break
else:
    raise SystemExit(f'could not write {qgs} (NAS?) after 4 tries')
s = open(qgs).read()
if '<mapcanvas' not in s:          # headless write drops the canvas: add it so it opens on the area
    mc = (f'<mapcanvas annotationsVisible="1" name="theMapCanvas"><units>meters</units><extent><xmin>{v.xMinimum()}</xmin>'
          f'<ymin>{v.yMinimum()}</ymin><xmax>{v.xMaximum()}</xmax><ymax>{v.yMaximum()}</ymax></extent><rotation>0</rotation>'
          f'<destinationsrs><spatialrefsys nativeFormat="Wkt"><wkt>{crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED)}</wkt>'
          f'<authid>{crs.authid()}</authid></spatialrefsys></destinationsrs>'
          f'<rendermaptile>0</rendermaptile></mapcanvas>\n  ')
    i = s.index('<projectlayers'); s = s[:i] + mc + s[i:]
# saved 3D view "3D terrain": opens ready with sharp tiles (4096 map texture, 64 DEM cells per tile, low screen
# error) on this area's DEM, draped with the layers visible in 2D. Written as XML: the Python API can't attach a
# DEM terrain generator to Qgs3DMapSettings, and its writeXml crashes without one. Format = QgisApp::read3DMapViewSettings.
if dem_id and '<mapViewDocks3D><view' not in s:
    from xml.sax.saxutils import quoteattr as qa
    cx, cy = v.center().x(), v.center().y()
    keep = lambda l: True
    if os.environ.get('LMAP_3D_CENTER'):          # big maps: 3D opens on one region, rasters only (whole AU froze QGIS >10 min)
        import math
        lo, la = [float(x) for x in os.environ['LMAP_3D_CENTER'].split(',')]
        c = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'), crs, p).transform(QgsPointXY(lo, la))
        h = float(os.environ.get('LMAP_3D_KM', '200')) * 1000 / 2 / math.cos(math.radians(la))   # 3857 metres stretch
        v = QgsRectangle(c.x() - h, c.y() - h, c.x() + h, c.y() + h); cx, cy = c.x(), c.y()
        keep = lambda l: l.type() == QgsMapLayerType.RasterLayer and not l.hasScaleBasedVisibility()
    lay = ''.join(f'<layer id={qa(l.layer().id())}/>' for l in root.findLayers() if l.isVisible() and l.layer() and keep(l.layer()))
    crs_xml = (f'<spatialrefsys nativeFormat="Wkt"><wkt>{crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED)}</wkt>'
               f'<authid>{crs.authid()}</authid></spatialrefsys>')
    dist = max(v.width(), v.height()) * 0.9
    view = (f'<mapViewDocks3D><view name="3D terrain" isOpen="0" isDocked="0">'
            f'<qgis3d><origin x="{cx}" y="{cy}" z="0"/>'
            f'<extent xMin="{v.xMinimum()}" yMin="{v.yMinimum()}" xMax="{v.xMaximum()}" yMax="{v.yMaximum()}" showIn2dView="0"/>'
            f'<camera field-of-view="45" projection-type="1" camera-navigation-mode="terrain-based-navigation" '
            f'camera-movement-speed="5"/><color background="#ffffff" selection="#ffff00"/>'
            f'<crs>{crs_xml}</crs>'
            # close view of a coastal city test box, time to fully loaded: 4096 px / err 1 / labels 191 s; 1024 / 3 / no labels
            # 25-30 s, looks the same (the screen shows ~1000 px per tile). Labels draped on hills read back-to-front.
            # Ground error 1 m: 0.5 drew 30 m cliffs on the test box (41 s), 5-10 m blurs the photo; 1 is smooth + sharp.
            f'<terrain terrain-rendering-enabled="1" exaggeration="{vscale}" texture-size="1024" max-terrain-error="3" '
            f'max-ground-error="1" shading-enabled="0" elevation-offset="0" map-theme="3D" show-labels="0">'
            f'<layers>{lay}</layers><generator type="dem" layer={qa(dem_id)} resolution="64" skirt-height="10"/>'
            f'</terrain><skybox skybox-enabled="0"/></qgis3d>'
            f'<camera x="0" y="0" elev="0" dist="{dist:.0f}" pitch="45" yaw="0"/>'
            f'</view></mapViewDocks3D>')
    if '<mapViewDocks3D/>' in s:                  # QGIS writes an empty one: fill it
        s = s.replace('<mapViewDocks3D/>', view, 1)
    else:
        i = s.index('<projectlayers'); s = s[:i] + view + '\n  ' + s[i:]
s = s.replace(d.rstrip('/') + '/', './')      # any absolute leftovers -> relative
# Liberation Sans everywhere (common on Linux desktops; metric-identical to Arial). Headless default "DejaVu LGC Sans" isn't installed.
# (Headless QGIS defaults to "DejaVu LGC Sans", which most desktops don't have -> warning bar on open.)
s = re.sub(r'(fontFamily|family)="DejaVu[^"]*"', r'\1="Liberation Sans"', s)
s = re.sub(r'<font ([^>]*?)family="[^"]*"', r'<font \1family="Liberation Sans"', s)
os.remove(qgs)
for x in os.listdir(d):                           # QGIS writes a sidecar beside the .qgs
    if x.startswith(f'.{name}.build'): os.remove(os.path.join(d, x))
qgs = os.path.join(scratch, 'area.qgs')
open(qgs, 'w').write(s)
out = os.path.join(scratch, 'area.qgz')
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(qgs, f'{name}.qgs')
print(json.dumps({'layers': len(p.mapLayers())})); sys.stdout.flush()
sys.stderr.flush(); os._exit(0)    # project written: skip QGIS teardown (it segfaulted after adding imported GPX layers)
'''


def area_qgis(ctx, d, name, crs, bbox, lvl, env=None):
    try:
        import importlib.util
        if not importlib.util.find_spec("qgis"):
            return {"skipped": "python3-qgis not installed"}
    except (ImportError, ValueError):
        return {"skipped": "python3-qgis not installed"}
    env = dict(env or {})
    env.setdefault("LMAP_CW", json.dumps(contour_widths(getattr(ctx, "a", None))))
    env.setdefault("LMAP_STYLE", json.dumps(map_style()))   # in the build sig: a style change rebuilds
    if "LMAP_NAMES" not in env:                        # area project: names from area.json
        env["LMAP_NAMES"] = json.dumps(area_layer_names(d))
    # rebuilt only when a layer file, the names, the level/box or the project code changed
    out = d / f"{name}.qgz"
    try:
        used = [str(v) for v in json.loads(env.get("LMAP_FILES") or "{}").values() if v and os.path.isfile(str(v))]
    except ValueError:
        used = []                                  # statewide projects: their layers live outside d
    sig = build_sig([x for x in sorted(d.iterdir()) if x.is_file() and x.suffix in (".tif", ".gpkg", ".mbtiles", ".vrt")]
                    + sorted(set(used)),
                    {"crs": crs, "bbox": [round(float(v), 6) for v in bbox], "lvl": lvl, "env": env,
                     "code": script_hash(AREA_QGIS)})
    if not getattr(ctx.a, "rebuild", False) and sig_current(out, sig):
        return {"file": out.name, "unchanged": True}
    sc = ctx.scratch() / f"area-{name}-qgis"; sc.mkdir(exist_ok=True)
    try:
        try:
            with MemTurn(ctx):
                r = subprocess.run(preexec_fn=_pdeath, args=[sys.executable, "-c", AREA_QGIS, str(d), name, crs, json.dumps(bbox), lvl, str(sc)],
                                   capture_output=True, text=True, timeout=900,
                                   env={**os.environ, "QT_QPA_PLATFORM": "offscreen", **env})
        except subprocess.TimeoutExpired:
            raise CliError(E_FAIL, "QGIS project build timed out (15 min)")
        line = next((l for l in r.stdout.splitlines() if l.startswith("{")), None)
        if r.returncode or not line:
            raise CliError(E_FAIL, f"QGIS project build failed: {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
        install_file(sc / "area.qgz", out)
        sig_write(out, sig)
        return {"file": out.name, **json.loads(line)}
    finally:
        shutil.rmtree(sc, ignore_errors=True)


AREA_README = """{name} - lmap area map ({level})

Open {name}.qgz in QGIS. Everything this map needs is in this folder; copy the folder anywhere.

COORDINATES - read this
  Status bar numbers are MGA zone {zone} (GDA2020) eastings/northings in metres.
  They are NOT an MGRS grid reference: no zone/band and no 100 km square letters.
  For MGRS: tick the 'MGRS grid' group (1 km lines, square IDs like '{zone}{band} DU') and read
  the 2-digit numbers on the lines, or use Lat Lon Tools (click -> copies e.g. '{zone}{band} DU 12345 67890').
  MGRS/ATAK/GPS = WGS84. MGA = GDA2020. They differ by ~1.7 m (2026) - fine for navigation,
  NOT for precision work.
  Angles are mils (6400 to a circle). Distances metres. Contours metres.

LAYERS (groups are off by default - tick to show)
  Contours: QLD LiDAR 10 m (index 50 m) where downloaded, else from the terrain DEM.
  Aerial shows when zoomed in past 1:60k; property lines past 1:20k.

Imagery: {imagery}. Things change - verify on the ground.
Box: {bbox}   Built: {made} by lmap {ver}
Licences: see area.json
"""


NAS_BLIP_WAITS = (10, 20, 40, 80, 150)   # ~5 min: growing waits before a step that hit a storage read error retries
NAS_BLIP_RE = re.compile(r"Input/output error|IReadBlock failed|_TIFFPartialRead|Cannot read offset|TIFFReadEncoded|"
                         r"Stale file handle|Connection (reset|timed out)|Host is down|Resource temporarily unavailable|"
                         r"short copy|disk I/O error|could not write .* \(NAS", re.I)


def nas_blip(e):
    """True if exception e looks like a dropped network share / storage read (worth retrying), not a code bug."""
    import errno
    if isinstance(e, OSError) and e.errno in (errno.EIO, errno.ESTALE, errno.ECONNRESET, errno.ETIMEDOUT, errno.EHOSTDOWN, errno.EAGAIN):
        return True
    return bool(NAS_BLIP_RE.search(str(getattr(e, "msg", "") or e)))


class Step:
    """One node of a Dag: fn() runs once every dep is done. Fails: steps that depend on it are skipped."""
    def __init__(self, id, fn, deps=(), label=None):
        self.id, self.fn, self.label = id, fn, label or id   # dep "?x": order only (run after x ends, even if it failed)
        self.deps = tuple(d for d in deps if not d.startswith("?")); self.soft = tuple(d[1:] for d in deps if d.startswith("?"))
        self.state, self.result, self.error, self.t0, self.t1 = "wait", None, None, None, None


class Dag:
    """Run Steps in parallel as soon as their deps are done. At most `width` at once; the real limits on
    servers, CPU and disk stay where they were (host_slot, CpuSlots, BuildSlot), shared by every process.
    DRAIN: start nothing new, let running steps finish. A failed step skips its dependants; others carry on.
    Logs: one line per start/finish with the step id; a status line every `every` s while busy."""
    def __init__(self, ctx, width=4, every=60):
        self.ctx, self.width, self.every, self.steps = ctx, max(1, width), every, {}

    def add(self, id, fn, deps=(), label=None):
        if id in self.steps:
            raise ValueError(f"duplicate step {id}")
        self.steps[id] = Step(id, fn, deps, label); return id

    def _check(self):
        for s in self.steps.values():
            for d in s.deps:
                if d not in self.steps:
                    raise CliError(E_FAIL, f"step {s.id} depends on unknown step {d}")
        seen, stack = set(), set()
        def visit(i):
            if i in stack:
                raise CliError(E_FAIL, f"dependency cycle at {i}")
            if i in seen:
                return
            stack.add(i); [visit(d) for d in self.steps[i].deps]; stack.discard(i); seen.add(i)
        [visit(i) for i in self.steps]

    def run(self):
        import concurrent.futures as cf
        self._check()
        S, log, t_last = self.steps, self.ctx.log, time.time()
        running = {}
        with cf.ThreadPoolExecutor(self.width) as ex:
            while True:
                for s in S.values():
                    if s.state == "wait" and any(S[d].state in ("failed", "skipped") for d in s.deps):
                        bad = [d for d in s.deps if S[d].state in ("failed", "skipped")]
                        s.state, s.error = "skipped", f"needs {', '.join(bad)}"; log(f"{s.id}: skipped ({s.error})")
                if not (DRAIN.is_set() or ABORT.is_set()):
                    for s in S.values():
                        if len(running) >= self.width:
                            break
                        if (s.state == "wait" and all(S[d].state == "done" for d in s.deps)
                                and all(S[d].state not in ("wait", "running") for d in s.soft if d in S)):
                            s.state, s.t0 = "running", time.time(); log(f"{s.id}: start ({s.label})")
                            running[ex.submit(self._call, s)] = s
                self._report()
                if not running:
                    self._report(); break
                done, _ = cf.wait(list(running), timeout=5, return_when=cf.FIRST_COMPLETED)
                for f in done:
                    s = running.pop(f); s.t1 = time.time()
                    try:
                        s.result = f.result(); s.state = "done"
                        log(f"{s.id}: done in {s.t1 - s.t0:.0f}s")
                        if not (isinstance(s.result, dict) and s.result.get("unchanged")):   # ETA history: real work only
                            step_hist_add(f"{(RUN.d or {}).get('cmd', '')}|{s.id}", s.t1 - s.t0)
                    except Aborted:
                        s.state, s.error = "stopped", "stopped"; log(f"{s.id}: stopped")
                    except CliError as e:
                        s.state, s.error = "failed", e.msg; log(f"{s.id}: FAILED: {e.msg}")
                    except Exception as e:                     # a bug in one step must not take the others down
                        s.state, s.error = "failed", f"{type(e).__name__}: {e}"; log(f"{s.id}: FAILED: {s.error}")
                if running and time.time() - t_last >= self.every:
                    t_last = time.time(); log(self.status_line())
        for s in S.values():
            if s.state == "wait":
                s.state, s.error = "not started", "stopped before it started"
        return S

    def _call(self, s):
        """s.fn, retried with growing waits (NAS_BLIP_WAITS, ~5 min in all) when it fails with a storage/network
        read error: a network-share reconnect mid-step must not fail the step (and skip everything after it)."""
        for i, w in enumerate((0,) + NAS_BLIP_WAITS):
            if w:
                self.ctx.log(f"{s.id}: storage read error, retry {i}/{len(NAS_BLIP_WAITS)} in {w}s")
                if ABORT.wait(w) or DRAIN.is_set():
                    raise Aborted()
            try:
                return s.fn()
            except (CliError, OSError) as e:
                if i == len(NAS_BLIP_WAITS) or not nas_blip(e):
                    raise

    def _report(self):
        """Feed the run registry: steps done/total, the running ones, ETA from past step times."""
        S = self.steps.values()
        RUN.update(dag=True, done=sum(s.state == "done" for s in S), total=len(self.steps),
                   running=[s.id for s in S if s.state == "running"],
                   eta_s=dag_eta(self.steps, self.width, (RUN.d or {}).get("cmd", ""), g := [], o := []),
                   eta_guess=bool(g), overdue=o)

    def status_line(self):
        c = {}
        for s in self.steps.values():
            c[s.state] = c.get(s.state, 0) + 1
        run = [f"{s.id} {time.time() - s.t0:.0f}s" for s in self.steps.values() if s.state == "running"]
        return f"status: {', '.join(f'{v} {k}' for k, v in sorted(c.items()))}" + (f"; running: {', '.join(run)}" if run else "")


REFRESH_FETCH = ["sat-au-30m", "sat-qld-s2-2025", "qld-tides", "osm-au", "osm-australia"]
# what each project reads: it waits only for these (an unrelated failed fetch no longer skips it)
REFRESH_PROJECT_NEEDS = {"qld-project": ["osm-australia", "sat-qld-s2-2025"],
                         "au-project": ["osm-australia", "sat-au-30m", "qld-project"]}   # au draws the qld VRTs


def refresh_plan(ctx):
    """[(id, argv-or-callable, deps, label)] for a full refresh. Fetches run at once (different servers; host
    limits still apply); osm-australia is cut from osm-au. qld project, THEN au project (au uses qld's files),
    each waiting only for the fetches it reads; with --refresh they rebuild the OSM-derived files and swap each
    in only when complete (no move-aside: a failed step leaves the old maps working). Areas wait for every fetch.
    --plan FILE (TOML [step.<id>] cmd=[...], deps=[...]) replaces it."""
    pf = getattr(ctx.a, "plan", None)
    if pf:
        try:
            st = tomllib.loads(Path(pf).read_text()).get("step") or {}
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise CliError(E_USAGE, f"--plan {pf}: {e}")
        return [(i, list(map(str, v["cmd"])), list(v.get("deps") or []), " ".join(map(str, v["cmd"]))) for i, v in st.items()]
    raw = getattr(ctx.a, "only", None)
    only = set(filter(None, (s.strip() for s in (raw or "").split(","))))
    if raw is not None and not only:               # 100a: an empty --only (blank script variable) must not mean all
        raise CliError(E_USAGE, "--only is empty; name items (e.g. --only dem,osm) or leave it out for everything")
    if getattr(ctx.a, "outdated", False):
        return refresh_plan_outdated(ctx, only)
    fetch = [f for f in REFRESH_FETCH if f in ctx.sources and (not only or f in only)]
    steps = [(f, ["data", "fetch", f, "--refresh"], ["osm-au"] if f == "osm-australia" and "osm-au" in fetch else [],
              f"fetch {f}") for f in fetch]
    after = [s[0] for s in steps]
    if not only or "projects" in only:
        osm = []                                       # derived files carry signatures; a new OSM is detected
        for sid, argv, label in (("qld-project", ["qld", "project"], "qld.qgz"), ("au-project", ["au", "project"], "australia.qgz")):
            have = {s[0] for s in steps}
            steps.append((sid, argv + osm, [d for d in REFRESH_PROJECT_NEEDS[sid] if d in have], label))
    names = getattr(ctx.a, "areas", None)
    names = [n for n in names.split(",") if n] if names is not None else \
        [p.parent.name for p in lib_jsons(ctx, "areas")]
    for n in names:
        try:
            aj = json.loads((lib_dir(ctx, "areas", n) / "area.json").read_text())
        except (OSError, ValueError) as e:
            raise CliError(E_NOTFOUND, f"area {n}: can't read area.json ({e})", "--areas a,b to pick areas")
        steps.append((f"area-{n}", ["area", "create", n, "--level", aj["level"],
                                    "--bbox=" + ",".join(str(x) for x in aj["bbox"])] + (["--refresh-live"] if getattr(ctx.a, "live", False) else []), after, f"area {n}"))
    return steps


def refresh_plan_outdated(ctx, only=()):
    """--outdated: fetch only the sources `lmap data outdated` finds newer upstream (osm-australia after osm-au),
    then rebuild only the projects and areas that read one of them. Sources it can't check are left alone."""
    rep = outdated_report(ctx)
    ctx.outdated = rep                                  # cmd_refresh reports it
    od = [r["id"] for r in rep if r.get("status") == "outdated" and (not only or r["id"] in only)]
    steps = [(f, ["data", "fetch", f, "--refresh"], ["osm-au"] if f == "osm-australia" and "osm-au" in od else [],
              f"fetch {f}") for f in od]
    have = set(od)
    if not only or "projects" in only:
        osm = "osm-australia" in have
        need = {"qld-project": {x for x in have if x.endswith("-state") and x.startswith("qld-")} |
                               ({"osm-australia", "sat-qld-s2-2025", "sat-qld-s2", "topo-qld-20m", "aerial-tsv-latest",
                                 "aerial-tsv-2019", "dem-qld-30m"} & have),
                "nsw-project": {x for x in have if x.endswith("-state") and x.startswith("nsw-")} |
                               ({"osm-australia", "dem-au-30m"} & have),
                "au-project": {"osm-australia", "sat-au-30m", "dem-au-30m", "dem-au-90m", "esa-worldcover"} & have}
        for sid, argv, label in (("qld-project", ["qld", "project"], "qld.qgz"), ("nsw-project", ["nsw", "project"], "nsw.qgz"),
                                 ("au-project", ["au", "project"], "australia.qgz")):
            if need[sid] or (sid == "au-project" and "qld-project" in {x[0] for x in steps}):
                deps = sorted(need[sid]) + (["qld-project"] if sid == "au-project" and "qld-project" in {x[0] for x in steps} else [])
                steps.append((sid, argv, deps, label))   # signatures pick up the changed inputs
    names = getattr(ctx.a, "areas", None)
    paths = [lib_dir(ctx, "areas", n) / "area.json" for n in names.split(",") if n] if names is not None else lib_jsons(ctx, "areas")
    for pth in paths:
        try:
            aj = json.loads(pth.read_text())
        except (OSError, ValueError) as e:
            raise CliError(E_NOTFOUND, f"area {pth.parent.name}: can't read area.json ({e})", "--areas a,b to pick areas")
        used = {i.get("from") for i in aj.get("items") or []} | {v.get("from") for v in (aj.get("built") or {}).values()
                                                                 if isinstance(v, dict)}
        used |= {"osm-au", "osm-australia"} if "osm (area clip)" in used else set()   # the clip reads the AU pbf
        hit = sorted(have & used)
        if hit:
            steps.append((f"area-{aj['name']}", ["area", "create", aj["name"], "--level", aj["level"],
                                                 "--bbox=" + ",".join(str(x) for x in aj["bbox"])]
                          + (["--refresh-live"] if getattr(ctx.a, "live", False) else []), hit,
                          f"area {aj['name']} ({', '.join(hit)})"))
    return steps


def lmap_step(ctx, sid, argv, logd):
    """Run `lmap <argv> --yes` as a child with its own log; forwards stop (SIGTERM) / abort (SIGUSR1)."""
    log = logd / f"{sid}.log"
    with open(log, "ab") as lf:
        pr = subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + argv + ["--yes", "--json"],
                              stdout=lf, stderr=lf, start_new_session=True, preexec_fn=_pdeath)
        child(pr); t_sp = time.time()                # an abort's hard exit kills it too (kill_children)
        try:
            sent = None
            while pr.poll() is None:
                if ABORT.is_set():
                    want = signal.SIGUSR1
                elif sent is not None:                   # pause/stop decided once: only an abort follows
                    want = sent                          # (a 2nd SIGUSR2 or a SIGTERM would abort the child)
                elif PAUSE.is_set():                     # pause only a child that says it can
                    cr = run_read(pr.pid)
                    want = (signal.SIGUSR2 if (cr or {}).get("pause")
                            else None if time.time() - t_sp < 60 else signal.SIGTERM)   # not registered yet: wait
                else:
                    want = signal.SIGTERM if DRAIN.is_set() else None
                if want and want != sent:
                    try:
                        pr.send_signal(want); sent = want
                    except ProcessLookupError:
                        pass
                time.sleep(1)
        finally:
            unchild(pr)
    out = log.read_text(errors="replace").strip().splitlines()
    res = {}
    for ln in reversed(out):
        if ln.startswith("{"):
            try:
                res = json.loads(ln); break
            except ValueError:
                pass
    if sent and (pr.returncode or res.get("ok") is not True):   # stopped part way; a paused step that finished counts
        raise Aborted()
    if pr.returncode or res.get("ok") is False or res.get("error"):
        raise CliError(E_FAIL, f"rc={pr.returncode}: {res.get('error') or (out[-1][-200:] if out else 'no output')} (log {log})")
    keep = {k: v for k, v in res.items() if k in ("built", "gb", "out", "unchanged", "fetched", "failed", "moved_aside")}
    if not res.get("failed"):                   # partly failed (tiles, layers): --resume must redo it
        refresh_mark_done(logd, sid, argv)
    return keep


def refresh_mark_done(logd, sid, argv):
    """done.json in the refresh's log dir: {step: argv} as each step finishes, so a crash or a network-share blip later
    in a many-hour refresh still leaves a record `refresh --resume` can skip."""
    with Lock(logd / ".done.lock"):
        f = logd / "done.json"
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {}
        d[sid] = argv; atomic_write(f, json.dumps(d, indent=1))


def refresh_last_done(plan_steps):
    """Steps of the newest earlier refresh that finished with the same command: {id: logdir}."""
    runs = sorted((STATE / "refresh").glob("2*"), reverse=True) if (STATE / "refresh").is_dir() else []
    for r in runs:
        try:
            d = json.loads((r / "done.json").read_text())
        except (OSError, ValueError):
            try:                                             # refreshes from before done.json: the summary
                d = {i: None for i, v in json.loads((r / "summary.json").read_text()).items() if v.get("state") == "done"}
            except (OSError, ValueError):
                continue
        want = {i: c for i, c, _, _ in plan_steps}
        return {i: str(r) for i, c in d.items() if i in want and (c is None or c == want[i])}
    return {}


def cmd_refresh(ctx):
    """Refresh everything (or --only/--plan) as one dependency graph: independent steps run at once, each in its
    own lmap child process + log under STATE/refresh/<time>/. Stop = drain every child; a failed step skips only
    the steps that need it."""
    need_root(ctx)
    steps = refresh_plan(ctx)
    width = int(getattr(ctx.a, "width", 0) or 6)
    skip = refresh_last_done(steps) if getattr(ctx.a, "resume", False) else {}
    if ctx.a.dry_run:
        od = getattr(ctx, "outdated", None)
        return {"ok": True, "dry_run": True, "action": "refresh", "width": width,
                "steps": [{"id": i, "run": c if isinstance(c, list) else l, "after": d, **({"skip": "done in " + skip[i]} if i in skip else {})}
                          for i, c, d, l in steps],
                **({"not_checkable": sorted(r["id"] for r in od if r.get("status") in ("unknown", "error"))} if od else {})}
    if getattr(ctx.a, "outdated", False) and not steps:
        return {"ok": True, "action": "refresh", "steps": {}, "note": "nothing outdated (of the sources that can be checked)"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to refresh without --yes", "preview: lmap refresh --dry-run")
    install_signals()
    logd = STATE / "refresh" / time.strftime("%Y%m%d-%H%M%S"); logd.mkdir(parents=True, exist_ok=True)
    ctx.log(f"refresh: {len(steps)} steps, up to {width} at once; logs in {logd}")
    with Lock(STATE / "locks" / "refresh.lock"):
        dag = Dag(ctx, width=width)
        for i, c, d, l in steps:
            if i in skip:                                # --resume: finished last time, not repeated
                refresh_mark_done(logd, i, c)
                dag.add(i, lambda i=i: {"unchanged": True, "resumed_from": skip[i]}, deps=d, label=l + " (done)"); continue
            dag.add(i, (lambda c=c, i=i: lmap_step(ctx, i, c, logd)) if isinstance(c, list) else c, deps=d, label=l)
        res = dag.run()
    out = {i: {"state": s.state, "secs": round(s.t1 - s.t0) if s.t1 and s.t0 else None,
               **({"error": s.error} if s.error else {}), **(s.result or {})} for i, s in res.items()}
    bad = [i for i, s in res.items() if s.state != "done"]
    if not bad and not DRAIN.is_set() and not ABORT.is_set():
        try:                                     # leave scratch empty (resume points of stopped jobs stay)
            c2 = copy.copy(ctx); c2.own_locks = ("refresh",)
            c2.a = argparse.Namespace(**{**vars(ctx.a), "resume": False, "dry_run": False, "yes": True})
            cr = cmd_clean(c2)
            if cr.get("freed_mb"):
                ctx.log(f"scratch: {cr['freed_mb']} MB of leftovers deleted")
        except (CliError, OSError) as e:
            ctx.log(f"scratch clean skipped: {getattr(e, 'msg', e)}")
    (logd / "summary.json").write_text(json.dumps(out, indent=1, default=str))
    return {"ok": not bad, "action": "refresh", "logs": str(logd), "steps": out,
            **({"hint": f"not done: {', '.join(bad)}; `lmap refresh --resume --yes` redoes only those (finished steps are not repeated)"} if bad else {})}


AREA_DEPS = {"field": ("osm",), "nsw-contours": ("?dem",), "contours-dem": ("dem",), "contours-best": ("qld-contours", "contours-lidar", "contours-nsw5", "nsw-contours", "contours-dem"), "spot-heights": ("dem",), "slope": ("dem",),
             "dem3d": ("dem",), "hls": ("slope",), "sat-even": ("sat", "vegetation")}


AREA_EXTRAS = {"3d": {"dem3d", "hls"}, "aerial": {"aerial"}}    # left out of `area create` unless asked


def cmd_area_create(ctx):
    need_root(ctx)
    need_tool("gdalwarp", "ogr2ogr", "gdalinfo")
    name = ctx.a.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", name):
        raise CliError(E_USAGE, f"bad area name {name!r}", "letters, digits, - _ . (e.g. magnetic-island)")
    lvl = ctx.a.level
    bbox = area_bbox(ctx)
    import math
    km2 = ((bbox[2] - bbox[0]) * 111.32 * math.cos(math.radians((bbox[1] + bbox[3]) / 2))) * ((bbox[3] - bbox[1]) * 110.574)
    lim = {"overview": 2e6, "regional": 1e5, "local": 1e4, "street": 3e3}[lvl]
    if km2 > lim and not ctx.a.force:
        raise CliError(E_USAGE, f"{km2:,.0f} km2 is big for --level {lvl} (limit {lim:,.0f} km2)",
                       "smaller box, a coarser --level, or --force")
    d = lib_dir(ctx, "areas", name, (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2)
    crs = mga_epsg((bbox[0] + bbox[2]) / 2)
    items = area_plan(ctx, bbox, lvl)
    # lean by default: a print needs none of 3D terrain / landing sites / aerial. --3d, --aerial, --full add them;
    # an area that already has one keeps it on re-runs (lmap refresh rebuilds areas with plain `area create`).
    try:
        _aj = json.loads((d / "area.json").read_text())
        had = set(_aj.get("extras") or [])
    except (OSError, ValueError):
        had = set(); _aj = {}
    full = getattr(ctx.a, "full", False)
    extras = {g for g, on in (("3d", full or getattr(ctx.a, "with_3d", False)), ("aerial", full or getattr(ctx.a, "aerial", False))) if on} | had
    had &= set(AREA_EXTRAS); extras &= set(AREA_EXTRAS)
    skipped = sorted({k for k, *_ in items if any(k in AREA_EXTRAS[g] for g in AREA_EXTRAS if g not in extras)})
    items = [i for i in items if i[0] not in skipped]
    est_gb = round(km2 * AREA_MB_KM2[lvl] / 1000, 2)
    sres = getattr(ctx.a, "sat_res", None)
    if sres is not None and not 0.05 <= sres <= 100:
        raise CliError(E_USAGE, f"--sat-res {sres}: use 0.05..100 metres")
    if sres:
        # measured on a coastal city test box: 2.4m 0.08, 0.3m 4, 0.1m 22 MB/km2 (JPEG q85); time ~11s/km2 at 0.3m, ~230s/km2 at 0.1m
        est_gb = round(est_gb + km2 * 0.62 / sres ** 1.55 / 1000, 2)
    have_aerial = any(k == "aerial" for k, *_ in items)
    if lvl in ("local", "street") and not have_aerial:
        est_gb = round(km2 * 0.2 / 1000, 2)
    summary = {"name": name, "level": lvl, "bbox": bbox, "km2": round(km2), "crs": crs, "dir": str(d),
               "est_gb": est_gb, "extras": sorted(extras), **({"skipped": skipped} if skipped else {}), "items": [{"key": k, "from": ex["from"], **({"res_m": ex["res"]} if "res" in ex else {})}
                                             for k, kind, src, ex in items]}
    notes = []
    if skipped:
        notes.append(f"lean build: skipped {', '.join(skipped)} (not needed for print maps); "
                     "--3d adds 3D terrain + landing sites, --aerial the aerial photo, --full both")
    if lvl in ("local", "street") and not have_aerial and "aerial" in extras:
        notes.append("no aerial covers this box (aerial: Paluma-Dalbeg / Townsville, Blue Mountains; `lmap data fetch` it first): satellite is the best imagery")
    if sres and sres < 2.4:
        notes.append(f"--sat-res {sres}: ~{km2 * 0.39 / sres ** 2.77 / 3600:.1f} h download (state server allows 3 connections)")
    if lvl == "street":
        notes.append("terrain is the 30m DEM; contours use LiDAR where flown (QLD LiDAR contour lines, else the 2m LiDAR DEM), "
                     "elsewhere approx from 30m (LiDAR DEM contours only if data/australia/dem-3d-2m-3dview.cog.tif is on disk)")
    if not overlaps(QLD_BOX, bbox):
        notes.append("outside QLD: no QLD Government layers")
    off = [f"{k} ({', '.join(ex['missing'])})" for k, kind, _, ex in items if kind == "gpkg-clip" and ex.get("missing")]
    if off:
        notes.append("offline copy lacks (not in this map; `lmap qld fetch --tier everything` adds them): " + "; ".join(off))
    live = [k for k, kind, *_ in items if kind == "arcgis"]
    if live:
        notes.append(f"needs internet for: {', '.join(live)} (`lmap qld fetch` stores them offline)")
    wcd = ctx.path(ctx.sources["esa-worldcover"]["dest"]) if "dest" in ctx.sources.get("esa-worldcover", {}) else None
    if any(kind == "veg" for _, kind, *_ in items) and not (wcd and len(list(wcd.glob("*_Map.tif"))) >= 128):
        notes.append("needs internet for: vegetation (`lmap data fetch esa-worldcover` stores all AU offline, 8 GB)")
    if notes:
        summary["notes"] = notes
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "area.create", **summary}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to build {d} without --yes",
                       f"preview: lmap area create {name} ... --dry-run")
    install_signals()
    built = {}
    with Lock(STATE / "locks" / f"area-{name}.lock"), \
            Reservation(ctx, f"area {name}", est_gb * 1e9, d), \
            Reservation(ctx, f"area {name} (OSM clip temp)", 3e9 if any(k == "osm-pbf" for _, k, _, _ in items) else 0,
                        ctx.scratch(), hint="pass --tmp or set [paths] tmp ="):
        d.mkdir(parents=True, exist_ok=True)
        for x in d.glob("*.part*"):
            x.unlink(missing_ok=True)
        # items in parallel: each waits only for what it reads (AREA_DEPS); servers/CPU stay capped by
        # host_slot / CpuSlots. --jobs sets the width (default 4).
        # the slow clips (dem/veg/sat/aerial/topo/surface water/osm) copied out of any area or map that already
        # holds this box (same CRS, built OK, same source, res as fine): seconds instead of minutes
        if not getattr(ctx.a, "rebuild", False):
            built.update(map_reuse(ctx, d, bbox, items, crs, lvl))
        todo = [i for i in items if i[0] not in built]
        if any(k.startswith("sheet-") and not readable(d / f"{k}.tif") for k, *_ in todo) and \
                not (sheets_dir(ctx) / "index.gpkg").is_file() and net_ok("services1.arcgis.com"):
            ctx.log("GA sheet index (once, ~2 min)")    # here, in the main thread: the Dag steps then just read it
            c2 = copy.copy(ctx); c2.a = argparse.Namespace(**{**vars(ctx.a), "dry_run": False, "yes": True, "refresh": False})
            from osgeo import gdal
            _ex = gdal.GetUseExceptions()                  # sheets index switches GDAL exceptions on: keep ours as was
            try:
                cmd_sheets_index(c2)
            except Aborted:
                pass                                       # ABORT stays set: the Dag starts nothing
            except Exception as e:                         # no index: the sheet steps just record skipped
                ctx.log(f"GA sheet index: {getattr(e, 'msg', e)}")
            finally:
                if not _ex:
                    gdal.DontUseExceptions()
        dag, keys = Dag(ctx, width=min(max(1, len(todo)), max(1, int(getattr(ctx.a, "jobs", 0) or 4)))), {k for k, *_ in todo}
        for key, kind, src, ex in todo:
            dag.add(key, lambda key=key, kind=kind, src=src, ex=ex: area_build_item(ctx, d, key, kind, src, ex, bbox, crs),
                    deps=[x for x in AREA_DEPS.get(key, ()) if x.lstrip("?") in keys], label=f"<- {ex['from']}")
        prev = {**{i.get("key"): {"from": i.get("from"), **({"res_m": i["res_m"]} if "res_m" in i else {})}
                   for i in _aj.get("items") or []},
                **{k: {x: v[x] for x in ("from", "res_m", "reused_from") if x in v}
                   for k, v in (_aj.get("built") or {}).items() if isinstance(v, dict) and "from" in v}}
        for key, s in dag.run().items():
            built[key] = s.result if s.state == "done" else {"error": s.error}
            if built[key].get("unchanged") and key in prev:
                built[key] = {**prev[key], **built[key]}   # kept file: record what it was BUILT from (map_reuse reads it)
        if not DRAIN.is_set():
            ctx.log("QGIS project")
            try:
                imp = import_clip(ctx, d, bbox, crs)    # the user's imported layers that overlap the area
                if imp:
                    built["imported"] = imp
                built["qgis"] = area_qgis(ctx, d, name, crs, bbox, lvl)
            except CliError as e:
                built["qgis"] = {"error": e.msg}
        lic = {ex["from"]: ctx.sources.get(ex["from"], {}).get("licence", "") for _, _, _, ex in items}
        zone = int(crs.split(":")[1]) - 7800 if crs != "EPSG:9473" else "?"
        (d / "README.txt").write_text(AREA_README.format(
            name=name, level=lvl, zone=zone, band=MGRS_BANDS[int(((bbox[1] + bbox[3]) / 2 + 80) // 8)],
            bbox=bbox, made=time.strftime("%Y-%m-%d"), ver=VERSION,
            imagery=imagery_text([{"key": k, "from": ex["from"]} for k, _, _, ex in items], d)))
        (d / "area.json").write_text(json.dumps({**summary, "built": built, "licences": lic,
                                                 "made": time.strftime("%Y-%m-%d %H:%M"), "lmap": VERSION}, indent=1))
    failed = [k for k, v in built.items() if "error" in v]
    res = {"ok": not failed and not DRAIN.is_set(), "action": "area.create", **summary, "built": built,
           "bytes": du_tree(d)}
    if failed:
        res["hint"] = f"re-run the same command to retry {failed} (finished parts are kept)"
    elif DRAIN.is_set():
        res["hint"] = "stopped; re-run the same command to continue"
    elif "file" in built.get("qgis", {}):
        res["open"] = f"qgis {d / (name + '.qgz')}"
    return res


def qld_tier_ids(ctx, tier):
    tiers = ("essentials",) if tier == "essentials" else ("essentials", "everything")
    return [k for k, v in ctx.sources.items() if v.get("tier") in tiers]


def cmd_qld_fetch(ctx):
    """Start (or report) the statewide QLD Government layer downloads for a tier, one background job
    each source in sequence. Resumes where it stopped. Areas then clip these locally (offline)."""
    need_root(ctx)
    ids = qld_tier_ids(ctx, ctx.a.tier)
    rows = []
    for sid in ids:
        s = ctx.src(sid); st = status_of(ctx, s)
        rows.append({"id": sid, "tier": s["tier"], "have": bool(st.get("complete")), "running": st["running"],
                     "est_gb": s.get("est_gb"), "est_h": s.get("est_h")})
    todo = [r["id"] for r in rows if ctx.a.refresh or not r["have"]]
    if ctx.a.dry_run or not todo:
        return {"ok": True, "dry_run": ctx.a.dry_run, "action": "qld.fetch", "tier": ctx.a.tier, "sources": rows,
                "todo": todo, "est_gb": round(sum(r["est_gb"] or 0 for r in rows if not r["have"]), 1),
                "est_hours": round(sum(r["est_h"] or 0 for r in rows if not r["have"]), 1)}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to start {len(todo)} statewide downloads without --yes",
                       f"preview: lmap qld fetch --tier {ctx.a.tier} --dry-run")
    STATE.mkdir(parents=True, exist_ok=True)
    lf = STATE / f"qld-{ctx.a.tier}.log"
    me = [sys.executable, str(Path(__file__).resolve())] + (["--root", str(ctx.root)]) + \
         (["--catalog", ctx.a.catalog] if getattr(ctx.a, "catalog", None) else [])
    need = sum(float(ctx.sources[i].get("est_gb") or 0) for i in todo) * 1.5e9
    if need > free_bytes(ctx.scratch()):
        raise CliError(E_FAIL, f"scratch {ctx.scratch()} has {room(ctx.scratch())}; "
                               f"building these needs ~{need / 1e9:.0f} GB", "set [paths] tmp = in ~/.config/lmap/config.toml to a bigger local disk")
    # one detached shell runs them in order; each `data fetch` is itself resumable and locked
    script = "; ".join(f"{' '.join(map(shlex_q, me))} data fetch {shlex_q(sid)} --yes" + (" --refresh" if ctx.a.refresh else "")
                       for sid in todo)
    with open(lf, "a") as log:
        log.write(f"\n== qld fetch --tier {ctx.a.tier} {time.strftime('%F %T')}: {' '.join(todo)}\n")
        p = subprocess.Popen(["sh", "-c", script], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    return {"ok": True, "action": "qld.fetch", "tier": ctx.a.tier, "started": todo, "pid": p.pid, "log": str(lf),
            "hint": f"progress: lmap qld status --tier {ctx.a.tier} ; stop: kill {p.pid} then lmap job stop <id> --yes"}


def shlex_q(x):
    import shlex
    return shlex.quote(str(x))


def cmd_qld_project(ctx):
    """Locked wrapper: two runs at once (or a scheduler retry) wait instead of racing on the same outputs."""
    if ctx.a.dry_run or not ctx.a.yes:
        return _cmd_qld_project(ctx)
    install_signals()                                  # stop/abort reach our GDAL children (no orphans)
    with Lock(STATE / "locks" / "project-qld.lock", wait=ctx):
        return _cmd_qld_project(ctx)


def _cmd_qld_project(ctx):
    """<root>/data/australia/queensland/qld.qgz: one QGIS project over the statewide offline copies (nothing copied, files used in place).
    Same styles as an area project; big layers only draw zoomed in (scale limits), so it stays responsive."""
    need_root(ctx)
    R = q = ctx.path("data/australia/queensland")
    def raster(sid):
        r = best_raster(ctx, sid); return str(r) if r else None
    sat_sid, sat_r = newest_raster(ctx, "sat-qld"); aer_sid, aer_r = newest_raster(ctx, "aerial")
    files = {"dem.tif": raster("dem-qld-30m") or raster("dem-au-30m"),   # QLD extent: hillshade + layer stay QLD-sized
             "sat.tif": str(sat_r) if sat_r else None,
             "topo.tif": raster("topo-qld-20m"), "aerial.tif": str(aer_r) if aer_r else None}
    names = {"sat.tif": LAYER_NAME.get(sat_sid, ""), "aerial.tif": LAYER_NAME.get(aer_sid, "")}
    # shading made once from the 30m DEM (~30 min, a build slot): in its own thread while the OSM extracts run
    hs = q / "hillshade-30m.cog.tif"
    import concurrent.futures as cf
    hx = cf.ThreadPoolExecutor(1)
    hsf = hx.submit(make_hillshade, ctx, files["dem.tif"], hs) \
        if files["dem.tif"] and not ctx.a.dry_run and ctx.a.yes else None    # skips itself when current
    og = ctx.path(ctx.sources["osm-australia"]["dest"])
    if og.is_file():
        files["osm.gpkg"] = str(og)
        # QLD-only copy of the layers the map draws, rows ordered by location: QGIS reading the 3.5M-road
        # Australia file over SMB took 10-40 s a view (scattered rows = many small network reads)
        qo = q / "qld-osm.gpkg"
        qo_stale, qo_sig = derived_stale(ctx, qo, [og], {"cut": "qld-osm", "v": 1}) if ctx.a.yes and not ctx.a.dry_run else (False, "")
        if qo_stale:
            part = ctx.scratch() / f"qld-osm.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
            w, so, e, n = QLD_BOX
            try:
                for i, (t, wh) in enumerate((("places", "place IS NOT NULL"), ("roads", "highway IS NOT NULL"),
                                             ("waterways", "waterway IN ('river','stream','canal')"))):
                    # -unsetFid: new FIDs in the sorted order, so nearby rows sit together in the file (keeping
                    # the Australia FIDs left them scattered: a Brisbane view read 99k rows from all over 500 MB)
                    run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + [part, files["osm.gpkg"], "-nln", t, "-unsetFid",
                         "-dialect", "SQLITE", "-spat", w - .5, so - .5, e + .5, n + .5, "-sql",
                         f"SELECT * FROM {t} WHERE {wh} ORDER BY CAST(ST_MinX(geom)*20 AS INT), CAST(ST_MinY(geom)*20 AS INT)"], ctx)
                install_file(part, qo); sig_write(qo, qo_sig)
            finally:
                part.unlink(missing_ok=True)
        if qo.is_file():
            files["osm.gpkg"] = str(qo)
    # one OGR VRT per area key: an area's qld-<key>.gpkg = <key>-state (+ <key>-all-state) tables
    for sid in AREA_LEVELS["street"]["qld"]:
        key = sid.replace("-tsv", ""); lays = []
        for k in (key + "-state", key + "-all-state"):
            v = ctx.sources.get(k)
            if v and ctx.path(v["dest"]).is_file():
                lays += [(t, ctx.path(v["dest"]).name) for t in v["layers"] if t not in {x for x, _ in lays}]
        if not lays:
            continue
        vrt = q / f"{key}.vrt"
        txt = ("<OGRVRTDataSource>\n" + "".join(
            f'  <OGRVRTLayer name="{t}"><SrcDataSource relativeToVRT="1" shared="1">{g}</SrcDataSource>'
            f'<SrcLayer>{t}</SrcLayer></OGRVRTLayer>\n' for t, g in lays) + "</OGRVRTDataSource>\n")
        write_if_changed(vrt, txt)               # rewritten only when its layers change (mtime stays put)
        files[key + ".gpkg"] = str(vrt)
    # QLD 10 m LiDAR contours as a vector-tile pyramid for viewing (prints use the full data). Built from the
    # network-share file only when the contours change (measured: 52 min from a local copy, 2 GB RAM, 1.2 GB file)
    qc = ctx.sources.get("qld-contours-state")
    if qc and ctx.path(qc["dest"]).is_file():
        mbt = q / "qld-contours.mbtiles"
        if not ctx.a.dry_run and ctx.a.yes:
            make_contour_tiles(ctx, ctx.path(qc["dest"]), mbt,
                               "SELECT geom, CAST(round(elevation_m) AS INTEGER) AS elevation_m FROM contour_lidar_5m "
                               "WHERE CAST(round(elevation_m) AS INTEGER) % 10 = 0", "QLD 10 m LiDAR contours (view only)")
        if mbt.is_file():
            files["qld-contours.mbtiles"] = str(mbt)
    # state-scale roads: OSM main roads in QLD, simplified (~50 m) into a small file (all 3.5M roads drew in 40 s)
    mr = q / "qld-main-roads.gpkg"
    mr_stale, mr_sig = derived_stale(ctx, mr, [files.get("osm.gpkg")], {"cut": "main-roads", "v": 1}) \
        if "osm.gpkg" in files and not ctx.a.dry_run and ctx.a.yes else (False, "")
    if mr_stale:
        part = ctx.scratch() / f"qld-main-roads.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        try:
            w, so, e, n = QLD_BOX
            run(["ogr2ogr", "-f", "GPKG", part, files["osm.gpkg"], "-nln", "main_roads", "-unsetFid", "-dialect", "SQLITE",
                 "-spat", w - .5, so - .5, e + .5, n + .5, "-sql",
                 "SELECT highway, name, ST_SimplifyPreserveTopology(geom, 0.0005) AS geom FROM roads WHERE highway IN "
                 "('motorway','trunk','motorway_link','trunk_link','primary','primary_link','secondary','secondary_link') "
                 "ORDER BY CAST(ST_MinX(geom)*20 AS INT), CAST(ST_MinY(geom)*20 AS INT)"], ctx)
            install_file(part, mr); sig_write(mr, mr_sig)
        finally:
            part.unlink(missing_ok=True)
    if mr.is_file():
        files["main-roads.gpkg"] = str(mr)
    files = {k: v for k, v in files.items() if v}
    missing = [k for k in ("dem.tif", "sat.tif", "topo.tif") if k not in files]
    if hsf is not None:
        hsf.result()                                   # raises its error here, after the OSM work is done
    hx.shutdown()
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "qld.project", "out": str(R / "qld.qgz"), "files": files,
                "missing": missing}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to write qld.qgz without --yes", "preview: lmap qld project --dry-run")
    # Web Mercator (like Google/OSM): QGIS draws lat/long data into it fast. GDA2020 Albers (9473) cost ~20 s on
    # the first draw + ~3 s per view for every vector layer (QGIS conic reprojection path). Areas stay on MGA.
    res = area_qgis(ctx, R, "qld", "EPSG:3857", list(QLD_BOX), "regional",
                    env={"LMAP_FILES": json.dumps(files), "LMAP_NAMES": json.dumps(names),
                         "LMAP_DEM_RANGE": "0,1600", "LMAP_NO3D": "1", "LMAP_STATEWIDE": "1",
                         "LMAP_HILLSHADE": str(hs) if hs.is_file() else ""})
    return {"ok": True, "action": "qld.project", "out": str(R / "qld.qgz"), "qgis": res, "missing": missing,
            "files": sorted(files)}


def cmd_nsw_project(ctx):
    """Locked wrapper (as qld project)."""
    if ctx.a.dry_run or not ctx.a.yes:
        return _cmd_nsw_project(ctx)
    install_signals()
    with Lock(STATE / "locks" / "project-nsw.lock", wait=ctx):
        return _cmd_nsw_project(ctx)


NSW_STATE = {"nsw-roads": "nsw-roads-state", "nsw-water": "nsw-water-state", "nsw-contours": "nsw-contours-state"}


def _cmd_nsw_project(ctx):
    """<root>/data/australia/new-south-wales/nsw.qgz: one QGIS project over the statewide NSW layers (files used in
    place) + NSW-cut copies of the national elevation shading and OSM (the national files over SMB were slow per view).
    Same styles as an area project; big layers draw only zoomed in. No satellite stored for NSW (live NSW imagery is
    per area): the project shows elevation, shading, OSM, NSW roads / creeks / contours."""
    need_root(ctx)
    R = ctx.path("data/australia/new-south-wales")
    w, so, e, n = NSW_BOX
    dem = best_raster(ctx, "dem-au-30m")
    files = {}
    # 30 m elevation cut to NSW (a VRT window on the national file: no copy) + shading made once from it
    dv = R / "dem-nsw-30m.vrt"; hs = R / "hillshade-30m.cog.tif"
    work = not ctx.a.dry_run and ctx.a.yes
    if work:                                   # a dry run / refusal writes nothing, not even the folder
        R.mkdir(parents=True, exist_ok=True)
    if dem and work:                                       # a second, rewritten only if it changed
        pv = dv.with_name(dv.stem + ".part.vrt")             # same folder (relative source path), then swap in
        run(["gdalbuildvrt", "-q", "-overwrite", "-te", w, so, e, n, pv, dem], ctx)
        if dv.is_file() and vrt_norm(dv.read_bytes()) == vrt_norm(pv.read_bytes()):   # GDAL adds stats to dv
            pv.unlink(missing_ok=True)
            if dem and not _built_after(dv, [dem]):      # DEM changed under the same window: drop its old stats
                atomic_write(dv, vrt_norm(dv.read_bytes()).decode())
        else:
            replace_retry(pv, dv)
    if dv.is_file():
        files["dem.tif"] = str(dv)
    import concurrent.futures as cf
    hx = cf.ThreadPoolExecutor(1)
    hsf = hx.submit(make_hillshade, ctx, str(dv), hs, deps=[dem] if dem else []) if dv.is_file() and work else None
    og = ctx.path(ctx.sources["osm-australia"]["dest"])
    no = R / "nsw-osm.gpkg"
    no_stale, no_sig = derived_stale(ctx, no, [og], {"cut": "nsw-osm", "v": 1}) if og.is_file() and work else (False, "")
    if no_stale:
        part = ctx.scratch() / f"nsw-osm.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        try:
            for i, (t, wh) in enumerate((("places", "place IS NOT NULL"), ("roads", "highway IS NOT NULL"),
                                         ("waterways", "waterway IN ('river','stream','canal')"))):
                run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + [part, og, "-nln", t, "-unsetFid",
                     "-dialect", "SQLITE", "-spat", w - .5, so - .5, e + .5, n + .5, "-sql",
                     f"SELECT * FROM {t} WHERE {wh} ORDER BY CAST(ST_MinX(geom)*20 AS INT), CAST(ST_MinY(geom)*20 AS INT)"], ctx)
            install_file(part, no); sig_write(no, no_sig)
        finally:
            part.unlink(missing_ok=True)
    if no.is_file():
        files["osm.gpkg"] = str(no)
    mr = R / "nsw-main-roads.gpkg"
    nm_stale, nm_sig = derived_stale(ctx, mr, [no], {"cut": "main-roads", "v": 1}) \
        if no.is_file() and work else (False, "")
    if nm_stale:
        part = ctx.scratch() / f"nsw-main-roads.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        try:
            run(["ogr2ogr", "-f", "GPKG", part, no, "-nln", "main_roads", "-unsetFid", "-dialect", "SQLITE", "-sql",
                 "SELECT highway, name, ST_SimplifyPreserveTopology(geom, 0.0005) AS geom FROM roads WHERE highway IN "
                 "('motorway','trunk','motorway_link','trunk_link','primary','primary_link','secondary','secondary_link') "
                 "ORDER BY CAST(ST_MinX(geom)*20 AS INT), CAST(ST_MinY(geom)*20 AS INT)"], ctx)
            install_file(part, mr); sig_write(mr, nm_sig)
        finally:
            part.unlink(missing_ok=True)
    if mr.is_file():
        files["main-roads.gpkg"] = str(mr)
    # NSW statewide layers, used in place. Contours: a VRT exposing the 10 m lines with the area name/field
    # (elevation -> elevation_m), so the area styles (CONTOUR_MIN scale limit, index 50 m) apply unchanged
    for key, sid in NSW_STATE.items():
        v = ctx.sources.get(sid)
        if not v or not ctx.path(v["dest"]).is_file():
            continue
        g = ctx.path(v["dest"])
        if key == "nsw-contours":
            mbt = R / "nsw-contours.mbtiles"               # vector-tile pyramid for viewing (prints use the gpkg)
            if work:
                make_contour_tiles(ctx, g, mbt, "SELECT geom, CAST(round(elevation) AS INTEGER) AS elevation_m "
                                   "FROM contours WHERE abs(elevation - round(elevation / 10.0) * 10) < 0.05",
                                   "NSW 10 m contours (Spatial Services 1:25k / large-scale mapping; view only)")
            if mbt.is_file():
                files["nsw-contours.mbtiles"] = str(mbt)
            vrt = R / "nsw-contours.vrt"
            if work:                                   # SrcSQL: OGR still passes the map window to the rtree (tested
                w_, s_, e_, n_ = NSW_BOX                   # 0.4 s local); the extent is given so QGIS never scans for it
                write_if_changed(vrt, '<OGRVRTDataSource>\n  <OGRVRTLayer name="contours"><SrcDataSource relativeToVRT="1">'
                    f'{g.name}</SrcDataSource><SrcSQL dialect="sqlite">SELECT geom, objectid, '
                    'CAST(round(elevation) AS INTEGER) AS elevation_m, '
                    'capturesourcecode' + (', verticalaccuracy' if has_field(g, "contours", "verticalaccuracy") else '')
                    + ' FROM contours WHERE abs(elevation - round(elevation / 10.0) * 10) &lt; 0.05</SrcSQL>'
                    '<GeometryType>wkbLineString</GeometryType><LayerSRS>EPSG:4326</LayerSRS>'
                    f'<ExtentXMin>{w_}</ExtentXMin><ExtentYMin>{s_}</ExtentYMin><ExtentXMax>{e_}</ExtentXMax>'
                    f'<ExtentYMax>{n_}</ExtentYMax></OGRVRTLayer>\n</OGRVRTDataSource>\n')
            files[key + ".gpkg"] = str(vrt) if vrt.is_file() or not work else None
        else:
            files[key + ".gpkg"] = str(g)
    files = {k: v for k, v in files.items() if v}
    missing = [k for k in ("dem.tif", "osm.gpkg", "nsw-roads.gpkg", "nsw-water.gpkg", "nsw-contours.gpkg") if k not in files]
    if hsf is not None:
        hsf.result()
    hx.shutdown()
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "nsw.project", "out": str(R / "nsw.qgz"), "files": files,
                "missing": missing}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to write nsw.qgz without --yes", "preview: lmap nsw project --dry-run")
    res = area_qgis(ctx, R, "nsw", "EPSG:3857", list(NSW_BOX), "regional",
                    env={"LMAP_FILES": json.dumps(files), "LMAP_NAMES": "{}",
                         "LMAP_DEM_RANGE": "0,2250", "LMAP_NO3D": "1", "LMAP_STATEWIDE": "1",
                         "LMAP_HILLSHADE": str(hs) if hs.is_file() else ""})
    return {"ok": True, "action": "nsw.project", "out": str(R / "nsw.qgz"), "qgis": res, "missing": missing,
            "files": sorted(files)}


AU_BOX = (112.0, -44.0, 154.0, -9.0)
AU_3857 = (12467782.97, -5465442.18, 17143201.58, -1006021.06)   # AU_BOX in EPSG:3857 (the 3D terrain extent)


def make_contour_tiles(ctx, src, out, sql, desc):
    """contours as a vector-tile pyramid (MBTiles, z10-15, extent 4096) for the statewide projects: a view is a
    few tile reads instead of thousands of small random reads over a network share (an NSW 1:16k test box 18-38 s -> 0.7 s).
    No extra simplification at z15 (measured vs source: max 0.17 m at z15, 0.8 m at z14, nothing missing; NSW build
    32 min, 0.5 GB RAM, 2.1 GB). View only: prints/verify use the full-precision gpkg.
    Rebuilt only when the source (size + mtime) or the SQL changed (sidecar .lmap-sig); --rebuild forces."""
    sig = build_sig([src], {"sql": sql, "v": "mvt-z10-15"})
    rb = getattr(ctx.a, "rebuild", False)
    fresh = lambda: sig_current(out, sig)
    if not rb and fresh():
        return
    t_ask = time.time()
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        if fresh() and (not rb or built_since(out, t_ask)):
            return                               # built by another run while we waited
        sig_stale(out)
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.mbtiles"; part.unlink(missing_ok=True)
        try:
            ctx.log(f"building {out.name} (vector tiles; NSW ~35 min)")
            _run(["ogr2ogr", "-f", "MVT", part, src, "-dialect", "SQLITE", "-sql", sql, "-nln", "contours",
                  "-dsco", "FORMAT=MBTILES", "-dsco", "MINZOOM=10", "-dsco", "MAXZOOM=15", "-dsco", "EXTENT=4096",
                  "-dsco", "SIMPLIFICATION=1", "-dsco", "SIMPLIFICATION_MAX_ZOOM=0", "-dsco", "MAX_SIZE=5000000",
                  "-dsco", "MAX_FEATURES=1000000", "-dsco", f"NAME={out.stem}", "-dsco", f"DESCRIPTION={desc}",
                  "--config", "GDAL_NUM_THREADS", str(slot.threads())], ctx)
            install_file(part, out)
            sig_write(out, sig); built_mark(out)       # source as read before the build: replaced mid-build -> rebuilt
        finally:
            part.unlink(missing_ok=True)
            Path(str(part) + ".temp.db").unlink(missing_ok=True)


def derived_stale(ctx, out, inputs, params):
    """(stale?, sig) for a derived file: rebuild when its inputs (size + mtime) or params changed, or --rebuild
    (no signature = rebuild). Used by the project-level cuts."""
    sig = build_sig(inputs, params)
    if getattr(ctx.a, "rebuild", False) or not readable(out):
        if readable(out):
            sig_stale(out)
        return True, sig
    if sig_current(out, sig):
        return False, sig
    sig_stale(out)                               # rebuild failing leaves the old cut marked STALE
    return True, sig


def make_hillshade(ctx, dem, out, scale=111120, deps=()):
    """Pre-made shading (gdaldem multidirectional) for a lat/long DEM: live QGIS shading of a geographic DEM
    in Web Mercator drew a stair-step grid. Built in scratch, copied to the network share. Takes a build slot (it uses
    BUILD_THREADS cores, like a COG build) and a lock on the output, so two projects never build it twice.
    Rebuilt when the DEM (or a VRT member) changes (signature); --rebuild forces."""
    sig = build_sig([dem, *deps], {"scale": scale, "v": "multidir"})     # deps: files behind a VRT
    rb = getattr(ctx.a, "rebuild", False)
    if not rb and sig_current(out, sig):
        return
    t_ask = time.time()
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        if sig_current(out, sig) and (not rb or built_since(out, t_ask)):
            return                                         # built by another run while we waited
        sig_stale(out)
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
        try:
            ctx.log(f"building {out.name} (QLD 30m ~30 min, Australia 90m ~20 min)")
            _run(["gdaldem", "hillshade", "-multidirectional", "-s", str(scale), "-compute_edges", "-of", "COG",
                  "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2", "-co", f"NUM_THREADS={slot.threads()}",
                  "-co", "BIGTIFF=YES", "-co", "OVERVIEW_RESAMPLING=AVERAGE",
                  "--config", "GDAL_CACHEMAX", str(BUILD_CACHE[0]), dem, part], ctx)
            install_file(part, out)
            sig_write(out, sig); built_mark(out)
        finally:
            part.unlink(missing_ok=True)


def au_dem3d_inputs(ctx, dem):
    """National elevation for the 30 m 3D terrain, coarse first (later files win where they overlap): national 90m,
    then 30m (the national file; only without it the old SE/QLD state files). Areas are NOT inputs: an area
    with finer elevation goes in as its own small patch (au_dem3d_patches), so a new area no longer re-warps all of
    Australia (~1 h, ~93 GB scratch); one cut from the national 30 m adds nothing and is skipped."""
    ins = [str(dem)]
    au30 = best_raster(ctx, "dem-au-30m")
    # fallback order SE then QLD on purpose: later inputs win in gdalwarp
    ins += [str(au30)] if au30 else [str(r) for sid in ("dem-se-aus-30m", "dem-qld-30m") if (r := best_raster(ctx, sid))]
    return ins


def au_dem3d_patch_srcs(ctx):
    """[(name, area dem-3d.tif)] for areas whose elevation is finer than the national 30 m (area.json built.dem
    res_m < 30); test-* areas skipped."""
    out = []
    for p in lib_jsons(ctx, "areas"):
        d3 = p.parent / "dem-3d.tif"
        if p.parent.name.startswith("test-") or not d3.is_file():
            continue
        try:
            m = json.loads(p.read_text())
        except ValueError:
            continue
        res = [i.get("res_m") for i in m.get("items", []) if i.get("key") == "dem"]
        if res and res[0] is not None and float(res[0]) < 30:
            out.append((p.parent.name, d3))
    return out


def au_dem3d_patches(ctx, A, plan_only=False):
    """One 3857 COG per finer area, beside dem-3d.vrt: dem-3d-patch-<area>.cog.tif (rebuilt only when the area's
    dem-3d.tif is newer). Minutes per area instead of the hour-long national re-warp. Old patches of areas that
    are gone / no longer finer are removed. Returns the patch paths (plan_only: which would be (re)built)."""
    want = {A / f"dem-3d-patch-{n}.cog.tif": src for n, src in au_dem3d_patch_srcs(ctx)}
    todo = [o for o, src in want.items() if not o.is_file() or o.stat().st_mtime < src.stat().st_mtime]
    if plan_only:
        return [o.name for o in todo]
    for o in A.glob("dem-3d-patch-*.cog.tif"):
        if o not in want:
            ctx.log(f"removing {o.name} (area gone or no finer than 30 m)"); o.unlink()
    for o in todo:
        src = want[o]
        with Lock(STATE / "locks" / f"out-{o.name}.lock", wait=ctx), BuildSlot(ctx, o.name) as slot:
            res = max(abs(float(json.loads(run(["gdalinfo", "-json", src], ctx))["geoTransform"][1])), 2)
            part = ctx.scratch() / f"{o.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
            try:
                ctx.log(f"building {o.name} (3D patch from {src.parent.name}, {res:g} m)")
                nt = slot.threads()
                _run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", f"NUM_THREADS={nt}", "-t_srs", "EPSG:3857",
                      "-tr", str(res), str(res), "-tap", "-r", DEM_RESAMPLE, "-ot", "Float32", "-srcnodata", "-32768",
                      "-dstnodata", "-32768", "-of", "COG", "-co", "COMPRESS=LERC_DEFLATE", "-co", "MAX_Z_ERROR=0.1",
                      "-co", "BIGTIFF=YES", "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", f"NUM_THREADS={nt}", src, part],
                     ctx, env={**os.environ, "GDAL_CACHEMAX": str(BUILD_CACHE[0])})
                why = deep_decode(ctx, part)
                if why:
                    raise CliError(E_FAIL, f"{o.name} failed its check ({why})", "re-run")
                install_file(part, o)
            finally:
                part.unlink(missing_ok=True)
    return sorted(want)


def au_dem3d_key(ins):
    """The input list as stored in the file (LMAP_INPUTS): full paths, order matters (later wins)."""
    return "|".join(str(i) for i in ins)


def au_dem3d_stale(out, ins):
    """Missing, older than an input (a new area's dem-3d, a re-fetched DEM), or built from a different input list
    (an input removed or swapped): rebuild. Files from before the list was stored are judged by time only."""
    try:
        if not out.is_file() or out.stat().st_mtime < max(Path(i).stat().st_mtime for i in ins):
            return True
    except OSError:                                    # an input vanished mid re-fetch: rebuild (gdalwarp fails loudly)
        return True
    try:                                               # unreadable output: rebuild
        ds = gdal_open(out); had = ds.GetMetadataItem("LMAP_INPUTS"); ds = None
    except CliError:
        return True
    return had is not None and had != au_dem3d_key(ins)


def au_dem3d(ctx, dem, out):
    """3D-only terrain for australia.qgz, in the map's own CRS (EPSG:3857: no reprojecting in the 3D view).
    30 m from the finest elevation we have at each spot (au_dem3d_inputs), cubic spline (rounded slopes, no new
    detail), Float32 with LERC to 0.25 m. 2026-10-15: from the 100 m Int16 version the 3D mesh drew 100 m steps
    as cliffs/terraces on hills; 60 m still did; 30 m is smooth. Full pyramid: zoomed out reads only coarse
    levels. A VRT of 4326 inputs straight in the 3D view never finished (>200 s): it has to be one 3857 file.
    Inputs go to gdalwarp directly (not a VRT): the area DEMs are in MGA, gdalbuildvrt skips mixed CRSs."""
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        ins = au_dem3d_inputs(ctx, dem)
        if not au_dem3d_stale(out, ins):
            return
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
        try:
            ctx.log(f"building {out.name} (3D terrain from {len(ins)} elevation files, ~1 h)")
            nt = slot.threads()
            _run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", f"NUM_THREADS={nt}", "-t_srs", "EPSG:3857",
                  "-te", *map(str, AU_3857), "-tr", "30", "30", "-tap", "-r", DEM_RESAMPLE, "-ot", "Float32",
                  "-dstnodata", "-32768", "-of", "COG", "-co", "COMPRESS=LERC_DEFLATE", "-co", "MAX_Z_ERROR=0.25",
                  "-co", "BIGTIFF=YES", "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", f"NUM_THREADS={nt}", *ins, part],
                 ctx, env={**os.environ, "GDAL_CACHEMAX": str(BUILD_CACHE[0])})
            why = deep_decode(ctx, part)
            if why or pyramid_short(part):
                raise CliError(E_FAIL, f"{out.name} failed its check ({why or 'short pyramid'})", "re-run")
            Path(str(out) + ".aux.xml").unlink(missing_ok=True)   # old stats describe the old raster
            install_file(part, out)
            try:                                               # input list into out.aux.xml (read-only open: the
                from osgeo import gdal                         # COG is not rewritten). Optional: without it the
                ds = gdal.Open(str(out))                       # check falls back to file times
                if ds:
                    ds.SetMetadataItem("LMAP_INPUTS", au_dem3d_key(ins))
                ds = None
            except Exception as e:
                ctx.log(f"note: could not record {out.name} inputs ({e})")
        finally:
            part.unlink(missing_ok=True)


AU_3D_CENTER, AU_3D_KM = (146.8, -19.26), 200          # the saved 3D view's box (example view)


def au_dem3d_fine(ctx, coarse, out, center=AU_3D_CENTER, km=AU_3D_KM):
    """10 m smooth 3D terrain for the saved 3D view's box, cut from the 30 m terrain (cubic spline: no new detail).
    2026-10-15: zoomed right in, the 3D mesh shows each 30 m cell as a ridge/facet ("steps"); a 10 m smooth grid
    draws curved slopes, same load time (34 s close view, 9 s on a 200 km test box). Whole AU at 10 m would be ~55 GB."""
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        if out.is_file() and out.stat().st_mtime >= Path(coarse).stat().st_mtime:
            return
        x0, y0, x1, y1 = au_3d_box(center, km)                   # same box as the 3D view (3857 metres)
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
        try:
            ctx.log(f"building {out.name} (10 m 3D terrain for the 3D view, ~3 min)")
            nt = slot.threads()
            _run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", f"NUM_THREADS={nt}", "-te", str(x0), str(y0),
                  str(x1), str(y1), "-tr", "10", "10", "-tap", "-r", DEM_RESAMPLE, "-ot", "Float32",
                  "-dstnodata", "-32768", "-of", "COG", "-co", "COMPRESS=LERC_DEFLATE", "-co", "MAX_Z_ERROR=0.1",
                  "-co", "BIGTIFF=YES", "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", f"NUM_THREADS={nt}", coarse, part],
                 ctx, env={**os.environ, "GDAL_CACHEMAX": str(BUILD_CACHE[0])})
            why = deep_decode(ctx, part)
            if why or pyramid_short(part):
                raise CliError(E_FAIL, f"{out.name} failed its check ({why or 'short pyramid'})", "re-run")
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)


def au_3d_box(center=AU_3D_CENTER, km=AU_3D_KM):
    """The saved 3D view's box in EPSG:3857 metres: (x0, y0, x1, y1)."""
    lo, la = center; h = km * 1000 / 2 / math.cos(math.radians(la))
    x, y = 6378137 * math.radians(lo), 6378137 * math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    return x - h, y - h, x + h, y + h


def lidar_coverage(ctx, box, out):
    """QLD LiDAR project footprints (2009-2021...) over box (3857) -> out (gpkg, 3857). Outside them the QLD DEM
    service returns 30 m SRTM, worse than our smoothed terrain, so only covered ground is used."""
    if out.is_file() and not (getattr(ctx.a, "refresh_live", False) or getattr(ctx.a, "refresh", False)):
        return out                                     # kept; re-asked only with --refresh-live / --refresh
    if out.is_file():                                  # due a refresh: new flights get added; offline keeps the old copy
        try:
            return _lidar_coverage(ctx, box, out)
        except (CliError, OSError) as e:
            ctx.log(f"LiDAR coverage refresh failed ({getattr(e, 'msg', e)}); using the copy from "
                    f"{time.strftime('%Y-%m-%d', time.localtime(out.stat().st_mtime))}")
            return out
    return _lidar_coverage(ctx, box, out)


def _lidar_coverage(ctx, box, out):
    x0, y0, x1, y1 = box
    q = urllib.parse.urlencode({"where": "1=1", "geometry": f"{x0},{y0},{x1},{y1}", "geometryType": "esriGeometryEnvelope",
                                "inSR": 3857, "spatialRel": "esriSpatialRelIntersects", "outFields": "project_name,year",
                                "outSR": 3857, "f": "geojson"})
    feats, off = [], 0
    while True:                                        # paged: the server returns at most maxRecordCount per ask
        j = json.loads(http_get(f"{lidar_src(ctx)['coverage']}?{q}&resultOffset={off}&resultRecordCount=500") or b"{}")
        if "features" not in j:
            raise CliError(E_UPSTREAM, "QLD LiDAR coverage query gave no features", "re-run later")
        feats += j["features"]; off += len(j["features"])
        if not j["features"] or not (j.get("exceededTransferLimit") or j.get("properties", {}).get("exceededTransferLimit")):
            break
    if not feats:
        raise CliError(E_UPSTREAM, "QLD LiDAR coverage query gave no features", "re-run later")
    b = json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    # built in local scratch: SQLite over SMB failed "database is locked" (2026-10-16), then copied in
    gj = ctx.scratch() / f"{out.stem}.{os.getpid()}.geojson"; part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.gpkg"
    try:
        gj.write_bytes(b)
        run(["ogr2ogr", "-q", "-overwrite", "-a_srs", "EPSG:3857", "-nln", "cov", part, gj], ctx)
        if out.is_file() and _cov_key(part) == _cov_key(out):
            os.utime(out, None)                        # same projects: just mark checked (no rebuild)
        else:
            out.parent.mkdir(parents=True, exist_ok=True); install_file(part, out)
    finally:
        gj.unlink(missing_ok=True); part.unlink(missing_ok=True)
    return out


def _cov_key(p):
    from osgeo import ogr
    ds = ro_ogr(p)
    if ds is None:
        return None
    return sorted(f.ExportToJson() for f in ds.GetLayer(0))


LIDAR_NONE = b"lmap:no-data\n"                         # what a 404 tile is stored as
MERC = 20037508.342787                                 # web-mercator half-width (the tile grid's origin)


def lidar_src(ctx):
    """The [source.lidar-qld] catalog entry (its server, zoom limit, tile shape, connections: per-source params)."""
    s = ctx.sources.get("lidar-qld")
    if not s:
        raise CliError(E_NOTFOUND, "no [source.lidar-qld] in the catalog", "update sources.toml")
    return dict(s, id="lidar-qld")


def lidar_zoom(res, max_zoom=18):
    """Cache level for a wanted ground resolution: the coarsest level at least as fine (5 m -> z15 4.8 m,
    2 m -> z17 1.2 m, 1 m -> z18 0.6 m)."""
    for z in range(10, max_zoom + 1):
        if 2 * MERC / 2 ** z / 256 <= res * 1.001:
            return z
    return max_zoom


def lidar_units(box, z, unit, cov):
    """Work units (ux, uy) at level z: unit x unit cache tiles each, over box, only those overlapping LiDAR
    coverage (edge-only contact skipped). Unit (ux, uy) = tiles x ux*unit.., y uy*unit.. (y down from the top)."""
    from osgeo import ogr
    ds = ro_ogr(cov)
    if ds is None:
        raise CliError(E_FAIL, f"{Path(cov).name} unreadable", f"delete it and re-run: {cov}")
    geoms = [g.Clone() for g in (f.GetGeometryRef() for f in ds.GetLayer(0)) if g is not None]
    um = 2 * MERC / 2 ** z * unit
    out = []
    for ux in range(int((box[0] + MERC) // um), int((box[2] + MERC) // um) + 1):
        for uy in range(int((MERC - box[3]) // um), int((MERC - box[1]) // um) + 1):
            x0, y1 = -MERC + ux * um, MERC - uy * um
            r = ogr.CreateGeometryFromWkt(f"POLYGON(({x0} {y1 - um},{x0 + um} {y1 - um},{x0 + um} {y1},{x0} {y1},{x0} {y1 - um}))")
            if any(g.Intersects(r) and not g.Touches(r) for g in geoms):
                out.append((ux, uy))
    return out


def lidar_unit_path(d, z, u):
    return d / f"z{z}" / f"{u[0]}_{u[1]}.tif"


def lidar_unit_build(ctx, s, z, u, tdir, out):
    """Assemble one unit from its downloaded cache tiles (each 257 px with 1 px overlap: the first 256 used)
    into a LERC GeoTIFF at out. A 404 tile (no data there) is stored as an empty file -> nodata."""
    import numpy as np
    from osgeo import gdal
    n, px = int(s["unit"]), int(s.get("tile_px", 257)) - 1   # last row/col repeats the next tile's first
    a = np.full((n * px, n * px), -32768, "f4")
    for j in range(n):
        for i in range(n):
            f = tdir / f"{u[1] * n + j}_{u[0] * n + i}"
            if f.read_bytes()[:len(LIDAR_NONE)] == LIDAR_NONE:   # (not "empty": a power cut can leave 0 bytes)
                continue
            try:                                       # DATATYPE: else GDAL reads it as Byte (0-255 m)
                ds = gdal.OpenEx(str(f), gdal.OF_RASTER, open_options=["DATATYPE=Float32"])
                t = ds.GetRasterBand(1).ReadAsArray(0, 0, px, px) if ds is not None else None
            except RuntimeError:
                t = None
            ds = None
            if t is None:                              # cut-off download: deleted so the retry fetches it again
                f.unlink(missing_ok=True)
                raise CliError(E_UPSTREAM, f"LiDAR tile {f.name} unreadable (deleted; refetched on retry)")
            a[j * px:(j + 1) * px, i * px:(i + 1) * px] = t
    tm = 2 * MERC / 2 ** z; r = tm / px
    # values sit ON the grid points (pixel 0 = the tile corner; checked 2026-10-16 against identify: 0.14 m
    # vs 0.25 m the other way), so the pixel area starts half a pixel up-left of the corner
    x0, y0 = -MERC + u[0] * n * tm - r / 2, MERC - u[1] * n * tm + r / 2
    part = out.with_name(out.stem + f".{os.getpid()}.part.tif")
    try:
        drv = gdal.GetDriverByName("GTiff")
        o = drv.Create(str(part), n * px, n * px, 1, gdal.GDT_Float32,
                       ["COMPRESS=LERC_DEFLATE", "MAX_Z_ERROR=0.05", "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512"])
        o.SetGeoTransform((x0, r, 0, y0, 0, -r)); o.SetProjection(_srs_wkt(3857))
        b = o.GetRasterBand(1); b.SetNoDataValue(-32768); b.WriteArray(a); o.FlushCache(); o = None
        replace_retry(part, out)
    finally:
        part.unlink(missing_ok=True)


def _srs_wkt(epsg):
    from osgeo import osr
    sr = osr.SpatialReference(); sr.ImportFromEPSG(epsg); return sr.ExportToWkt()


def lidar_fetch(ctx, box, z, d, cov):
    """Download the LiDAR cache tiles for every unit over the coverage at level z, unit by unit, through the
    shared tile downloader (keep-alive, per-server congestion control, shared connection budget), then
    assemble each unit (resumable: finished units kept; a stop keeps them). Returns the unit paths."""
    import concurrent.futures as cf
    s = lidar_src(ctx); n = int(s["unit"])
    units = lidar_units(box, z, n, cov)
    todo = [u for u in units if not lidar_unit_path(d, z, u).is_file()]
    (d / f"z{z}").mkdir(parents=True, exist_ok=True)
    for f in (d / f"z{z}").glob("*.part.tif"):            # a killed run's half-written units
        f.unlink(missing_ok=True)
    tiles = len(todo) * n * n
    ctx.log(f"LiDAR z{z} ({2 * MERC / 2 ** z / 256:.2f} m): {len(units)} units over the coverage, {len(todo)} to get "
            f"({tiles} tiles)")
    if not todo:
        return [lidar_unit_path(d, z, u) for u in units]
    random.shuffle(todo)                                   # random order: spreads load across the server's cache tiles
    url = s["url"]; host = urllib.parse.urlsplit(url).hostname or ""
    log_export_terms(ctx, s, url)                            # the service owner's export permission
    prof = server_profile(host)
    if s.get("max_conns"):                                  # this source's own ceiling (per-source params)
        prof["max"] = max(1, min(prof["max"], int(s["max_conns"])))
    if s.get("conns"):
        prof["start"] = max(1, min(prof["max"], int(s["conns"])))
    tf = TileFetcher(ctx, host, prof, LIDAR_NONE)          # 404 asked twice -> this marker = no data there
    prog = Progress({"id": f"au-lidar-z{z}"}, len(units), len(units) - len(todo))
    failed = []; U = max(2, ctx.shrink + 2)

    def one(u):
        tdir = ctx.scratch() / f"lidar-z{z}-{u[0]}_{u[1]}.tiles"
        try:
            tdir.mkdir(parents=True, exist_ok=True)
            need = [(x, y) for y in range(u[1] * n, (u[1] + 1) * n) for x in range(u[0] * n, (u[0] + 1) * n)
                    if not (tdir / f"{y}_{x}").is_file()]
            random.shuffle(need)
            batch = TileBatch(len(need))
            for x, y in need:
                tf.q.put((url.replace("${z}", str(z)).replace("${y}", str(y)).replace("${x}", str(x)), tdir / f"{y}_{x}", batch))
            batch.wait()
            if ABORT.is_set():
                raise Aborted()
            lidar_unit_build(ctx, s, z, u, tdir, lidar_unit_path(d, z, u))
            shutil.rmtree(tdir, ignore_errors=True)
            return u, None
        except (CliError, Aborted) as e:
            return u, e
        except Exception as e:
            return u, CliError(E_FAIL, f"{type(e).__name__}: {e}")

    try:
        for rnd in (1, 2):
            failed = []; it = iter(todo); run_ = set(); i = 0
            with cf.ThreadPoolExecutor(U) as ex:
                while True:
                    say_stop(ctx)
                    while not DRAIN.is_set() and not ABORT.is_set() and len(run_) < U:
                        u = next(it, None)
                        if u is None:
                            break
                        run_.add(ex.submit(one, u))
                    if not run_:
                        break
                    fin, _ = cf.wait(run_, timeout=5, return_when=cf.FIRST_COMPLETED)
                    for f in fin:
                        run_.discard(f); u, err = f.result()
                        if isinstance(err, Aborted):
                            continue
                        i += 1
                        if err:
                            failed.append(u)
                            if rnd == 2:
                                prog.tick(False)
                            ctx.log(f"LiDAR z{z} [{i}/{len(todo)}] FAIL {u}: {err.msg}")
                            continue
                        prog.tick(True, conns=int(tf.gate.c))
                        if i % 10 == 0 or i == len(todo):
                            ctx.log(f"LiDAR z{z} [{i}/{len(todo)}]  ({tf.tiles} tiles, x{int(tf.gate.c)})")
            if not failed or DRAIN.is_set() or ABORT.is_set():
                break
            ctx.log(f"LiDAR: retrying {len(failed)} failed unit(s)")
            todo = failed
    finally:
        tf.close()
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    if ABORT.is_set():
        raise Aborted()
    if DRAIN.is_set():
        kept = sum(1 for u in units if lidar_unit_path(d, z, u).is_file())
        raise CliError(E_DRAINED, f"LiDAR z{z}: stopped, {kept}/{len(units)} units kept", "re-run to resume")
    if failed:
        raise CliError(E_UPSTREAM, f"LiDAR z{z}: {len(failed)} unit(s) failed twice", "re-run (resumes)")
    for t in ctx.scratch().glob(f"lidar-z{z}-*.tiles"):     # strays (coverage/unit size changed): never reused
        shutil.rmtree(t, ignore_errors=True)
    return [lidar_unit_path(d, z, u) for u in units]


def lidar_stale(ctx, fine, out, res, cov_dir):
    """True if out must be (re)built: missing, older than the 10 m patch, or a unit is missing / newer than it
    (an earlier run stopped part-way). Offline-safe: no coverage file + out present = fresh."""
    if not out.is_file() or out.stat().st_mtime < Path(fine).stat().st_mtime:
        return True
    cov = cov_dir / "lidar-coverage.gpkg"
    if not cov.is_file():
        return False
    s = lidar_src(ctx); z = lidar_zoom(res, int(s["max_zoom"])); t_out = out.stat().st_mtime
    for u in lidar_units(au_3d_box(), z, int(s["unit"]), cov):
        try:
            if lidar_unit_path(cov_dir, z, u).stat().st_mtime > t_out:
                return True
        except FileNotFoundError:
            return True
    return False


def au_dem3d_lidar(ctx, fine, out, res, cov_dir):
    """The 3D view's box at res metres: the smooth 10 m patch (cubic spline) with real LiDAR on top wherever
    QLD has flown it (cut to the coverage polygons). 2026-10-16 test at 2 m: same 3D load times as 10 m
    (wide 5 s, close ~31 s), creek beds and roads visible. From the QldDem elevation cache (z15 for 5 m, z17 for 2 m).
    2 m: 714 units / 183k tiles, ~20 min download (2.5 GB) + ~35 min build -> 2.7 GB. 5 m: 667 MB."""
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx), BuildSlot(ctx, out.name) as slot:
        box = au_3d_box()
        cov = lidar_coverage(ctx, box, cov_dir / "lidar-coverage.gpkg")   # first: new flights make it stale
        if not lidar_stale(ctx, fine, out, res, cov_dir):
            return
        tiles = lidar_fetch(ctx, box, lidar_zoom(res, int(lidar_src(ctx)["max_zoom"])), cov_dir, cov)
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
        lv = ctx.scratch() / f"{out.stem}.{os.getpid()}.vrt"; cut = ctx.scratch() / f"{out.stem}.{os.getpid()}.cut.tif"
        try:
            ctx.log(f"building {out.name} ({res:g} m 3D terrain: LiDAR where flown, smooth 10 m elsewhere)")
            nt = slot.threads()
            _run(["gdalbuildvrt", "-q", "-overwrite", lv, *tiles], ctx)
            # LiDAR only inside the flown footprints (outside: the service's 30 m SRTM fill). Not -cutline: with the
            # 311k-vertex coverage that ran single-core ~18x slower; burning nodata outside gives the same pixels
            _run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", f"NUM_THREADS={nt}", "-te", *map(str, box),
                  "-tr", str(res), str(res), "-tap", "-r", "bilinear", "-dstnodata", "-32768", "-co", "COMPRESS=LERC_DEFLATE",
                  "-co", "MAX_Z_ERROR=0.05", "-co", "TILED=YES", "-co", "BIGTIFF=YES", lv, cut], ctx)
            _run(["gdal_rasterize", "-q", "-i", "-burn", "-32768", cov, cut], ctx)
            _run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", f"NUM_THREADS={nt}", "-te", *map(str, box),
                  "-tr", str(res), str(res), "-tap", "-r", DEM_RESAMPLE, "-ot", "Float32", "-srcnodata", "-32768",
                  "-dstnodata", "-32768", "-of", "COG", "-co", "COMPRESS=LERC_DEFLATE", "-co", "MAX_Z_ERROR=0.1",
                  "-co", "BIGTIFF=YES", "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", f"NUM_THREADS={nt}", fine, cut, part],
                 ctx, env={**os.environ, "GDAL_CACHEMAX": str(BUILD_CACHE[0])})
            why = deep_decode(ctx, part)
            if why or pyramid_short(part):
                raise CliError(E_FAIL, f"{out.name} failed its check ({why or 'short pyramid'})", "re-run")
            install_file(part, out)
        finally:
            for f in (part, lv, cut):
                f.unlink(missing_ok=True)


def au_lidar_plan(ctx, A):
    """Dry-run view of the LiDAR step: resolution, and whether a run would download/build."""
    lr = au_lidar_res(ctx, A)
    if not lr:
        return {"lidar_res": 0}
    out, fine = A / f"dem-3d-{lr:g}m-3dview.cog.tif", A / "dem-3d-10m-3dview.cog.tif"
    return {"lidar_res": lr, "builds_lidar": not fine.is_file() or lidar_stale(ctx, fine, out, lr, A / "lidar")}


def au_lidar_res(ctx, A):
    """--lidar-res M (0 = off), remembered in data/australia/lidar/res so a plain re-run keeps it. Default 5."""
    f = A / "lidar" / "res"
    v = getattr(ctx.a, "lidar_res", None)
    if v is None:
        try:
            v = float(f.read_text())
            if v and not 0.5 <= v <= 10:
                raise ValueError(v)
        except (OSError, ValueError):
            if f.is_file():
                ctx.log(f"{f}: unreadable/out of range; using 5 m")
            v = 5.0
    if v and not 0.5 <= v <= 10:
        raise CliError(E_USAGE, f"--lidar-res {v:g}: use 0 (off) or 0.5-10 m")
    if not ctx.a.dry_run:
        f.parent.mkdir(parents=True, exist_ok=True); f.write_text(f"{v:g}")
    return v


def au_dem3d_vrt(ctx, coarse, fine, out, patches=()):
    """The 3D view's terrain: 30 m country + the 10 m patch on top, one VRT at 10 m (both 3857, so no reproject;
    reads use the sources' overviews: 30 s close / 3 s wide). Sources must sit beside it (relative paths, checked)."""
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx):
        _au_dem3d_vrt(ctx, coarse, fine, out, patches)


def _au_dem3d_vrt(ctx, coarse, fine, out, patches=()):
    def res(p):
        try:                                           # unreadable: sort it low; gdalbuildvrt then fails loudly
            ds = gdal_open(p); r = abs(ds.GetGeoTransform()[1]); ds = None
            return r
        except CliError:
            return 99
    # later wins in a VRT: coarsest first, so the finest data is on top wherever sources overlap (a 5 m area patch
    # over the 10 m box, the 2 m LiDAR box over a 5 m patch); ties keep the given order
    srcs = [Path(coarse)] + sorted([*map(Path, patches), Path(fine)], key=lambda p: -res(p))
    if any(p.parent != out.parent for p in srcs):
        raise CliError(E_FAIL, f"{out.name}: its sources must sit beside it", "bug: report it")
    have = re.findall(r"<SourceFilename[^>]*>([^<]*)<", out.read_text()) if out.is_file() else None
    if have == [p.name for p in srcs] and out.stat().st_mtime >= max(p.stat().st_mtime for p in srcs):
        return
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    try:
        _run(["gdalbuildvrt", "-q", "-overwrite", "-resolution", "highest", "-vrtnodata", "-32768", tmp, *srcs],
             ctx)                                          # sources beside the VRT: gdalbuildvrt writes them relative
        t = tmp.read_text()
        if "/" in "".join(re.findall(r"<SourceFilename[^>]*>([^<]*)<", t)) or 'relativeToVRT="0"' in t:
            raise CliError(E_FAIL, f"{out.name}: source paths not relative", "re-run")
        replace_retry(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def _au_field(ctx, files, A):
    """Field points/lines for all of AU (fuel, hospitals, water, huts, fords, tracks...) straight from the raw
    .osm.pbf, same kinds/symbols as an area's field.gpkg. ~10 min, ~200 MB; rebuilt when the pbf is newer.
    Power towers, poles + fences left out (AU_FIELD_SKIP; power lines stay). Rebuilt if the skip list changes."""
    pbf = ctx.path(ctx.sources["osm-australia"]["from"]); out = A / "osm" / "au-field.gpkg"
    tag = out.with_name(out.name + ".skip")              # which kinds it was built without
    stale = lambda: pbf.is_file() and (not out.is_file() or getattr(ctx.a, "rebuild", False)
                                       or out.stat().st_mtime < pbf.stat().st_mtime
                                       or not tag.is_file() or tag.read_text() != ",".join(AU_FIELD_SKIP))
    if not pbf.is_file():
        ctx.log(f"no {pbf.name}: no all-AU field features")
    if stale():
        with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx):
            if stale():                                # again: a run we waited for may have built it
                _au_field_build(ctx, pbf, out)
    if out.is_file():
        files["field.gpkg"] = str(out)


AU_FIELD_SKIP = ("power tower", "power pole", "fence")   # all AU: 645k towers+poles / 243k fences; fences ~10 s per cold view


def _au_field_build(ctx, pbf, out):
    part = ctx.scratch() / f"au-field.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
    try:
        ctx.log("building au-field.gpkg (OSM field features, all AU, ~10 min)")
        n = build_field(ctx, pbf, part, skip=AU_FIELD_SKIP)
        if sum(n.values()) < 1000:                     # an empty/short pbf read must not replace a good file
            raise CliError(E_FAIL, f"au-field came out short ({sum(n.values())} features); not installed", "re-run")
        install_file(part, out)
        out.with_name(out.name + ".skip").write_text(",".join(AU_FIELD_SKIP))
    finally:
        part.unlink(missing_ok=True)


def _au_main_roads(ctx, files, A):
    """national main roads: OSM motorway/trunk/primary/secondary, simplified, spatially ordered (fast over SMB)"""
    mr = A / "osm" / "au-main-roads.gpkg"
    au_stale, au_sig = derived_stale(ctx, mr, [files.get("osm.gpkg")], {"cut": "au-main-roads", "v": 1}) if "osm.gpkg" in files else (False, "")
    if au_stale:
        part = ctx.scratch() / f"au-main-roads.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
        try:
            run(["ogr2ogr", "-f", "GPKG", part, files["osm.gpkg"], "-nln", "main_roads", "-unsetFid", "-dialect", "SQLITE",
                 "-sql", "SELECT highway, name, ST_SimplifyPreserveTopology(geom, 0.001) AS geom FROM roads WHERE highway IN "
                 "('motorway','trunk','motorway_link','trunk_link','primary','primary_link','secondary','secondary_link') "
                 "ORDER BY CAST(ST_MinX(geom)*5 AS INT), CAST(ST_MinY(geom)*5 AS INT)"], ctx)
            n = gpkg_count(part, "main_roads")
            if not n:                                  # 2026-10-15: an SMB I/O hiccup gave an empty, "successful" extract
                raise CliError(E_FAIL, f"au-main-roads extract came out empty ({n} roads); kept the old file",
                               "re-run `lmap au project --yes --refresh`")
            # whole-country view: 198k road pieces drew in 1.6-3.7 s; merged per class + 1-degree cell and
            # simplified ~500 m: 660 lines, 0.4 s, looks the same above 1:3M (the project switches at that scale)
            small = Path(str(part) + ".small.gpkg"); small.unlink(missing_ok=True)
            try:
                run(["ogr2ogr", "-f", "GPKG", small, part, "-nln", "main_roads_small", "-nlt", "MULTILINESTRING",
                     "-dialect", "SQLITE", "-sql",
                     "SELECT CASE WHEN highway IN ('motorway','trunk','motorway_link','trunk_link') THEN 'trunk' "
                     "ELSE 'primary' END AS highway, ST_SimplifyPreserveTopology(ST_LineMerge(ST_Collect(geom)), 0.005) "
                     "AS geom FROM main_roads WHERE highway NOT LIKE 'secondary%' "      # overview: no secondary (>1:3M)
                     "GROUP BY 1, CAST(ST_MinX(geom) AS INT), CAST(ST_MinY(geom) AS INT)"], ctx)
                run(["ogr2ogr", "-f", "GPKG", "-update", part, small, "-nln", "main_roads_small"], ctx)
            finally:
                small.unlink(missing_ok=True)
            install_file(part, mr); sig_write(mr, au_sig)
        finally:
            part.unlink(missing_ok=True)
    if mr.is_file():
        files["main-roads.gpkg"] = str(mr)


def cmd_au_project(ctx):
    """Locked wrapper: two runs at once (or a scheduler retry) wait instead of racing on the same outputs."""
    if ctx.a.dry_run or not ctx.a.yes:
        return _cmd_au_project(ctx)
    install_signals()                                  # stop/abort reach our GDAL children (no orphans)
    with Lock(STATE / "locks" / "project-au.lock", wait=ctx):
        return _cmd_au_project(ctx)


def _cmd_au_project(ctx):
    """<root>/data/australia/australia.qgz: whole-country overview (90m elevation + shading, OSM towns/roads/rivers,
    plus each state's offline satellite where we have it). Files used in place, paths relative."""
    need_root(ctx)
    A = ctx.path("data/australia")
    v = getattr(ctx.a, "lidar_res", None)
    if v and not 0.5 <= v <= 10 or v is not None and v < 0:
        raise CliError(E_USAGE, f"--lidar-res {v:g}: use 0 (off) or 0.5-10 m")
    files = {"dem.tif": str(best_raster(ctx, "dem-au-90m") or "")}
    # national 30m covers the whole country; else the QLD statewide copy (newest)
    sat_sid, sat = ("sat-au-30m", best_raster(ctx, "sat-au-30m"))
    if not sat:
        sat_sid, sat = newest_raster(ctx, "sat-qld")
    if sat:
        files["sat.tif"] = str(sat)
    names = {"sat.tif": LAYER_NAME.get(sat_sid, "")}
    og = ctx.path(ctx.sources["osm-australia"]["dest"])
    if og.is_file():
        files["osm.gpkg"] = str(og)
    files = {k: v for k, v in files.items() if v}
    if "dem.tif" not in files:
        raise CliError(E_NOTFOUND, "no Australia 90m elevation", "lmap data fetch dem-au-90m --yes")
    hs, d3 = A / "hillshade-90m.cog.tif", A / "dem-3d-30m.cog.tif"
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "au.project", "out": str(A / "australia.qgz"), "files": files,
                "builds_hillshade": not hs.is_file(),
                "builds_3d_terrain": au_dem3d_stale(d3, au_dem3d_inputs(ctx, files["dem.tif"])),
                "builds_3d_patch": not (A / "dem-3d-10m-3dview.cog.tif").is_file(),
                "builds_3d_area_patches": au_dem3d_patches(ctx, A, plan_only=True),
                **au_lidar_plan(ctx, A)}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to write australia.qgz without --yes", "preview: lmap au project --dry-run")
    import concurrent.futures as cf
    hx = cf.ThreadPoolExecutor(2)                      # shading + 3D terrain (build slots) beside the roads extract
    hsf = hx.submit(make_hillshade, ctx, files["dem.tif"], hs)                    # skips itself when current
    d3f = (hx.submit(au_dem3d, ctx, files["dem.tif"], d3)
           if au_dem3d_stale(d3, au_dem3d_inputs(ctx, files["dem.tif"])) else None)
    try:
        _au_main_roads(ctx, files, A)
        try:
            _au_field(ctx, files, A)
        except Exception as e:                         # optional layer: an old file (if any) is kept and used
            if isinstance(e, Aborted):
                raise
            ctx.log(f"field features not rebuilt ({type(e).__name__}: {getattr(e, 'msg', e)}); carrying on")
            if (A / "osm" / "au-field.gpkg").is_file():
                files["field.gpkg"] = str(A / "osm" / "au-field.gpkg")
    except BaseException:
        STOP_BUILDS.set()                              # don't leave hour-long GDAL builds running past our lock
        raise
    finally:
        errs = []
        for fu in (hsf, d3f):                          # wait for both, even when one (or the roads) failed
            if fu is not None:
                try:
                    fu.result()
                except Aborted:
                    if not STOP_BUILDS.is_set():
                        errs.append(Aborted())
                except BaseException as e:
                    errs.append(e)
        hx.shutdown()
        if STOP_BUILDS.is_set():                       # roads failed: say what else went wrong, the roads error raises
            for e in errs:
                ctx.log(f"also failed: {type(e).__name__}: {getattr(e, 'msg', e)}")
            STOP_BUILDS.clear()
    if errs:
        for e in errs[1:]:
            ctx.log(f"also failed: {type(e).__name__}: {getattr(e, 'msg', e)}")
        raise errs[0]
    d3p = A / "dem-3d-10m-3dview.cog.tif"; d3v = A / "dem-3d.vrt"
    au_dem3d_fine(ctx, d3, d3p)                            # no-op unless missing / older than the 30 m file
    top = d3p
    lr = au_lidar_res(ctx, A)
    if lr:
        top = A / f"dem-3d-{lr:g}m-3dview.cog.tif"
        try:
            au_dem3d_lidar(ctx, d3p, top, lr, A / "lidar")
        except Aborted:
            raise
        except Exception as e:                         # optional: no LiDAR (offline, server down) -> best we have
            if isinstance(e, CliError) and e.code == E_DRAINED:   # a stop is a stop, not a reason to carry on
                raise
            old = sorted(A.glob("dem-3d-*m-3dview.cog.tif"), key=lambda p: p.stat().st_mtime)
            old = [p for p in old if p != d3p and p.stat().st_mtime >= d3p.stat().st_mtime]
            top = top if top.is_file() else (old[-1] if old else d3p)
            ctx.log(f"LiDAR terrain not built ({getattr(e, 'msg', None) or f'{type(e).__name__}: {e}'}); "
                    f"3D uses {top.name}")
    au_dem3d_vrt(ctx, d3, top, d3v, au_dem3d_patches(ctx, A))
    # one map for the whole country: national layers underneath, the finer state/local data on top, each shown
    # from the zoom where it adds detail (QLD 10m satellite, Townsville 0.5m aerial, QLD Government layers)
    detail = []
    q24 = best_raster(ctx, "sat-qld-2m4")                # optional 2.4 m (greater Townsville by default): above
    if q24:                                              # the 10 m, under the 0.5 m aerial; from ~1:150k in
        detail.append({"file": str(q24), "name": LAYER_NAME["sat-qld-2m4"], "minscale": 150000})
    q10_sid, q10 = newest_raster(ctx, "sat-qld")
    if q10:
        detail.append({"file": str(q10), "name": LAYER_NAME.get(q10_sid, "Satellite QLD"), "minscale": 1500000})
    aer_sid, aer = newest_raster(ctx, "aerial")
    if aer:
        files["aerial.tif"] = str(aer); names["aerial.tif"] = LAYER_NAME.get(aer_sid, "")
    if (A / "contours-30m.gpkg").is_file():               # `lmap au contours --yes`
        files["contours-30m.gpkg"] = str(A / "contours-30m.gpkg")
    if (A / "mgrs-grid.gpkg").is_file():                  # `lmap au grid --yes`
        files["mgrs-grid.gpkg"] = str(A / "mgrs-grid.gpkg")
    if (A / "sheets").is_dir():                            # `lmap sheets index/get --yes`
        files["sheets"] = str(A / "sheets")
    Q = ctx.path("data/australia/queensland")
    for key in ("qld-hazard", "qld-flood", "qld-fire", "qld-water", "qld-surface-water", "qld-infra", "qld-parks",
                "qld-roads", "qld-cadastre", "qld-contours"):
        if (Q / f"{key}.vrt").is_file():             # written by `lmap qld project`
            files[key + ".gpkg"] = str(Q / f"{key}.vrt")
    res = area_qgis(ctx, A, "australia", "EPSG:3857", list(AU_BOX), "overview",
                    env={"LMAP_FILES": json.dumps(files), "LMAP_NAMES": json.dumps(names),
                         "LMAP_DEM_RANGE": "0,2200", "LMAP_NO3D": "1", "LMAP_STATEWIDE": "1",
                         "LMAP_3D_DEM": str(d3v), "LMAP_3D_SCALE": "1.5",
                         "LMAP_3D_CENTER": ",".join(map(str, AU_3D_CENTER)), "LMAP_3D_KM": str(AU_3D_KM),
                         "LMAP_HILLSHADE": str(hs), "LMAP_DETAIL": json.dumps(detail),
                         "LMAP_SLOPES": json.dumps([{"file": str(A / o), "name": n, "minscale": ms} for o, n, ms in (
                             ("slope-30m.cog.tif", "Slope 30 m", 0), ("slope-10m-lidar.cog.tif", "Slope 10 m (LiDAR)", 250000))
                             if (A / o).is_file()]),
                         "LMAP_LIGHTS": (str(ctx.path(ctx.sources["lights-au"]["dest"])) if "lights-au" in ctx.sources
                                         and ctx.path(ctx.sources["lights-au"]["dest"]).is_file() else "")})
    return {"ok": True, "action": "au.project", "out": str(A / "australia.qgz"), "qgis": res, "files": sorted(files)}


MOON_QGIS = r'''
import sys, json, os, re, zipfile
from qgis.core import *
from qgis.PyQt.QtGui import QColor, QPainter
QgsApplication.setPrefixPath('/usr', True); app = QgsApplication([], False); app.initQgis()
d, out, kind = sys.argv[1], sys.argv[2], sys.argv[3]; F = json.loads(os.environ['LMAP_MOON'])
p = QgsProject.instance(); p.clear(); p.setTitle('Moon - lmap' if kind == 'global' else 'Moon south pole - lmap')
root = p.layerTreeRoot(); n = 0
def lay(key, name):
    if key not in F: return None
    r = QgsRasterLayer(F[key], name, 'gdal')
    if not r.isValid(): raise SystemExit(f'{key}: not a valid raster: {F[key]}')
    # the files carry the same sphere under different datum names (unknown / D_Moon / Moon 2015): QGIS desktop then
    # finds "no transform" between them. One IAU code per projection, so all layers + the project share one CRS.
    c = QgsCoordinateReferenceSystem('IAU_2015:30135' if '+proj=stere' in r.crs().toProj() else 'IAU_2015:30110')
    if not c.isValid() or 'R=1737400' not in c.toProj() or c.toProj().split('+type')[0].split() != r.crs().toProj().split('+type')[0].split():
        raise SystemExit(f'{key}: unexpected CRS {r.crs().toProj()}')
    r.setCrs(c)
    p.addMapLayer(r, False); return r
def put(r, on=True, minscale=0, parent=None):
    global n
    if r is None: return None
    if minscale: r.setScaleBasedVisibility(True); r.setMinimumScale(minscale)
    (parent or root).addLayer(r).setItemVisibilityChecked(on); n += 1; return r
def gray(r, op=1.0, blend=None):
    if r is None: return None
    r.dataProvider().setUseSourceNoDataValue(1, False)   # 0 = shadowed crater floor (real black), not a hole
    ce = QgsContrastEnhancement(Qgis.DataType.Byte); ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
    ce.setMinimumValue(0); ce.setMaximumValue(255)       # fixed 0-255: NoEnhancement renders flat grey
    rn = QgsSingleBandGrayRenderer(r.dataProvider(), 1); rn.setContrastEnhancement(ce); rn.setOpacity(op); r.setRenderer(rn)
    if blend is not None: r.setBlendMode(blend)
    r.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler()); return r
def shade(r, op=1.0):
    if r is None: return None
    hs = QgsHillshadeRenderer(r.dataProvider(), 1, 315, 45); hs.setMultiDirectional(True)
    r.setRenderer(hs); r.renderer().setOpacity(op)   # normal blend: the polar photo is near-black (shadow), Multiply would hide the relief
    r.resampleFilter().setZoomedInResampler(QgsCubicRasterResampler()); return r
def relief(r, lo, hi):
    if r is None: return None
    ramp = QgsStyle.defaultStyle().colorRamp('Spectral')
    k = 9; items = [QgsColorRampShader.ColorRampItem(lo + (hi - lo) * i / (k - 1), ramp.color(1 - i / (k - 1)),
                                                     f"{lo + (hi - lo) * i / (k - 1):.0f} m") for i in range(k)]
    sh = QgsColorRampShader(lo, hi); sh.setColorRampType(QgsColorRampShader.Interpolated); sh.setColorRampItemList(items)
    rs = QgsRasterShader(); rs.setRasterShaderFunction(sh)
    rn = QgsSingleBandPseudoColorRenderer(r.dataProvider(), 1, rs); rn.setClassificationMin(lo); rn.setClassificationMax(hi)
    rn.setOpacity(0.5); r.setRenderer(rn); r.setBlendMode(QPainter.CompositionMode_Multiply); return r
M = QPainter.CompositionMode_Multiply
if kind == 'global':        # top of the list = drawn on top
    put(relief(lay('dem', 'Elevation colours (LOLA 118m)'), -9000, 10000), on=False)
    put(gray(lay('hillshade', 'Shading (LOLA 118m)'), 0.5, M))
    base = put(gray(lay('photo', 'Photo (LRO WAC 100m)')))
else:                       # south pole, polar stereographic: the poles are a smeared strip in the global map
    put(shade(lay('spole-5m', 'Elevation 87-90S, 5m (LOLA, shaded)')), minscale=500000)
    put(shade(lay('spole-20m', 'Elevation 80-90S, 20m (LOLA, shaded)')), minscale=4000000)
    put(relief(lay('spole-20m', 'Elevation colours 80-90S (LOLA 20m)'), -5000, 2500), on=False)
    base = put(gray(lay('photo-spole', 'Photo 70-90S (LRO WAC 100m)')))
base = base or next(iter(p.mapLayers().values()))
p.setCrs(base.crs()); p.setBackgroundColor(QColor(0, 0, 0))
# coordinate readout + measuring default to Earth (WGS 84 / its ellipsoid): QGIS desktop then shows "No transform
# available between IAU_2015:... and EPSG:4326 / Custom CRS". Moon lat/lon (IAU_2015:30100) + the Moon sphere.
_ll = QgsCoordinateReferenceSystem('IAU_2015:30100'); assert _ll.isValid()
ds = p.displaySettings(); ds.setCoordinateType(Qgis.CoordinateDisplayType.CustomCrs); ds.setCoordinateCustomCrs(_ll)
p.setEllipsoid('IAU_2015:30100')   # the CRS's own sphere (R 1737400): a custom PARAMETER: one breaks the 3D globe view
                                   # (QGIS 3.44 builds a datum-less geocentric CRS: "No inverse operation")
p.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(base.extent(), base.crs()))
p.viewSettings().setPresetFullExtent(QgsReferencedRectangle(base.extent(), base.crs()))   # zoom full: no layer scan
# 3D terrain: whole Moon 118m (height scale 0.5 applied by the provider), pole 20m (5m: too big a tile load).
# Saved view "3D terrain" (closed), centred where the detail is: equator 0,0 / the pole; 2x height.
k3 = 'dem' if kind == 'global' else 'spole-20m'
dem3 = next((l for l in p.mapLayers().values() if l.source() == F.get(k3)), None) if k3 in F else None
if dem3 is None and k3 in F:
    dem3 = lay(k3, 'Terrain (3D)')
if dem3 is not None:
    tp = QgsRasterDemTerrainProvider(); tp.setLayer(dem3); tp.setScale(2.0); p.elevationProperties().setTerrainProvider(tp)
def _collapse(node):                                # every group + layer legend folded on open (tidy layer list)
    for c in node.children():
        c.setExpanded(False); _collapse(c)
_collapse(p.layerTreeRoot())
p.writeEntry('Paths', '/Absolute', False)
qgs = os.path.join(d, '.moon.build.qgs'); assert p.write(qgs)
s = open(qgs).read().replace(d.rstrip('/') + '/', './')
if dem3 is not None and '<mapViewDocks3D><view' not in s:
    from xml.sax.saxutils import quoteattr as qa
    crs = p.crs(); half = 400000 if kind == 'global' else 150000      # opening view: 800 / 300 km wide
    v = QgsRectangle(-half, -half * 0.6, half, half * 0.6)
    lay_xml = ''.join(f'<layer id={qa(l.layer().id())}/>' for l in root.findLayers() if l.isVisible() and l.layer())
    crs_xml = (f'<spatialrefsys nativeFormat="Wkt"><wkt>{crs.toWkt(QgsCoordinateReferenceSystem.WKT_PREFERRED)}</wkt>'
               f'<authid>{crs.authid()}</authid></spatialrefsys>')
    view = (f'<mapViewDocks3D><view name="3D terrain" isOpen="0" isDocked="0">'
            f'<qgis3d><origin x="0" y="0" z="0"/>'
            f'<extent xMin="{v.xMinimum()}" yMin="{v.yMinimum()}" xMax="{v.xMaximum()}" yMax="{v.yMaximum()}" showIn2dView="0"/>'
            f'<camera field-of-view="45" projection-type="1" camera-navigation-mode="terrain-based-navigation" '
            f'camera-movement-speed="5"/><color background="#000000" selection="#ffff00"/>'
            f'<crs>{crs_xml}</crs>'
            f'<terrain terrain-rendering-enabled="1" exaggeration="2" texture-size="1024" max-terrain-error="3" '
            f'max-ground-error="0.5" shading-enabled="0" elevation-offset="0" show-labels="0">'
            f'<layers>{lay_xml}</layers><generator type="dem" layer={qa(dem3.id())} resolution="64" skirt-height="10"/>'
            f'</terrain><skybox skybox-enabled="0"/></qgis3d>'
            f'<camera x="0" y="0" elev="0" dist="{half * 1.6:.0f}" pitch="45" yaw="0"/>'
            f'</view></mapViewDocks3D>')
    s = s.replace('<mapViewDocks3D/>', view, 1) if '<mapViewDocks3D/>' in s else \
        s.replace('<projectlayers', view + '\n  <projectlayers', 1)
s = re.sub(r'(fontFamily|family)="DejaVu[^"]*"', r'\1="Liberation Sans"', s)
os.remove(qgs)
for x in os.listdir(d):
    if x.startswith('.moon.build'): os.remove(os.path.join(d, x))
q2 = out[:-4] + '.qgs'; open(q2, 'w').write(s)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(q2, os.path.basename(q2))
os.remove(q2)
print(json.dumps({'layers': n, 'crs': p.crs().description()}))
'''

MOON_SRC = {"photo": "moon-photo-100m", "dem": "moon-dem-118m", "spole-20m": "moon-spole-20m", "spole-5m": "moon-spole-5m"}
MOON_SPOLE = "+proj=stere +lat_0=-90 +lon_0=0 +k=1 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs"


def moon_photo_spole(ctx, photo, out):
    """The global photo reprojected to south polar stereographic, 70-90S at 100 m (~0.1 GB): QGIS can't
    reproject the global (equirectangular) Moon onto a polar view on the fly (draws nothing near the pole)."""
    with Lock(STATE / "locks" / f"out-{out.name}.lock", wait=ctx):
        if out.is_file():
            return
        part = ctx.scratch() / f"{out.stem}.{os.getpid()}.part.tif"; part.unlink(missing_ok=True)
        try:
            ctx.log(f"building {out.name} (photo 70-90S, polar)")
            run(["gdalwarp", "-q", "-overwrite", "-multi", "-wo", "NUM_THREADS=ALL_CPUS", "-t_srs", MOON_SPOLE,
                 "-te", "-610000", "-610000", "610000", "610000", "-tr", "100", "100", "-r", "bilinear",
                 "-dstnodata", "0", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
                 "-co", "NUM_THREADS=ALL_CPUS", photo, part], ctx)
            install_file(part, out)
        finally:
            part.unlink(missing_ok=True)


def cmd_moon_project(ctx):
    """<root>/data/moon/moon.qgz (whole Moon: photo + shading, elevation colours off) and moon-spole.qgz (south
    pole in its own polar projection: photo 70-90S, 20 m shading, 5 m from 1:500k). Lunar CRSs (Moon sphere,
    R 1737.4 km); files used in place, paths relative. First run builds a hillshade + the polar photo."""
    need_root(ctx)
    M = ctx.path("data/moon")
    files, missing = {}, []
    for k, sid in MOON_SRC.items():
        s = ctx.sources.get(sid)
        f = ctx.path(s["dest"]) if s else None
        (files.__setitem__(k, str(f)) if f and f.is_file() else missing.append(sid))
    if "photo" not in files and "dem" not in files:
        raise CliError(E_NOTFOUND, "no whole-Moon photo or elevation yet",
                       "lmap job start moon-photo-100m --yes ; lmap job start moon-dem-118m --yes")
    hs, pp = M / "hillshade-118m.cog.tif", M / "photo-spole-100m.cog.tif"
    outs = [M / "moon.qgz"] + ([M / "moon-spole.qgz"] if "spole-20m" in files or "spole-5m" in files else [])
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "moon.project", "out": [str(o) for o in outs], "files": files,
                "missing": missing, "builds": [str(x) for x, k in ((hs, "dem"), (pp, "photo")) if k in files and not x.is_file()]}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to write moon.qgz without --yes", "preview: lmap moon project --dry-run")
    import importlib.util
    if not importlib.util.find_spec("qgis"):
        raise CliError(E_NOTFOUND, "python3-qgis not installed", "apt install python3-qgis")
    install_signals()
    import concurrent.futures as cf
    with Lock(STATE / "locks" / "project-moon.lock", wait=ctx), cf.ThreadPoolExecutor(2) as ex:
        jobs = []
        if "dem" in files and not hs.is_file():         # metres on both axes: scale 1 (Australia's is degrees)
            jobs.append(ex.submit(make_hillshade, ctx, files["dem"], hs, 1))
        if "photo" in files and len(outs) > 1 and not pp.is_file():
            jobs.append(ex.submit(moon_photo_spole, ctx, files["photo"], pp))
        for j in jobs:
            j.result()
        if hs.is_file():
            files["hillshade"] = str(hs)
        if pp.is_file():
            files["photo-spole"] = str(pp)
        res = {}
        for out, kind in zip(outs, ("global", "spole")):
            sc = ctx.scratch() / f"moon-qgis.{os.getpid()}"; sc.mkdir(parents=True, exist_ok=True)
            try:
                r = _run([sys.executable, "-c", MOON_QGIS, str(M), str(sc / out.name), kind], ctx, timeout=600,
                         env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "LMAP_MOON": json.dumps(files)})
                line = next((l for l in r.splitlines() if l.startswith("{")), None)
                if not line:
                    raise CliError(E_FAIL, f"QGIS gave no result line for {out.name}", "re-run with -v")
                install_file(sc / out.name, out); res[out.name] = json.loads(line)
            finally:
                shutil.rmtree(sc, ignore_errors=True)
    return {"ok": True, "action": "moon.project", "out": [str(o) for o in outs], "files": sorted(files),
            "missing": missing, "qgis": res}


def gpkg_count(path, layer):
    """Feature count of one layer, None if unreadable."""
    try:
        r = subprocess.run(["ogrinfo", "-so", str(path), layer], capture_output=True, text=True, timeout=120)
        m = re.search(r"Feature Count: (\d+)", r.stdout)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_qld_status(ctx):
    need_root(ctx)
    rows = []
    for sid in qld_tier_ids(ctx, ctx.a.tier):
        s = ctx.src(sid); st = status_of(ctx, s)
        ck = ctx.scratch() / f"{sid}.resume.json"
        prog = json.loads(ck.read_text()) if ck.is_file() else {}
        rows.append({"id": sid, "tier": s["tier"], "state": "done" if st.get("complete") else
                     ("running" if st["running"] else ("partial" if prog else "missing")),
                     "gb": round((st.get("bytes") or 0) / 1e9, 2),
                     "layers": {k: v[0] for k, v in prog.items()} if prog else None})
    return {"ok": True, "action": "qld.status", "tier": ctx.a.tier, "qld": rows}


WMM_COF = HERE / "share" / "WMM_2025.COF"     # NOAA World Magnetic Model 2025-2030 (public domain)


class ModelExpired(ValueError):
    """The magnetic model doesn't cover this date: a declination from it would be quietly wrong."""


def wmm_model(cof=None):
    """(name, epoch, valid_until) of the bundled World Magnetic Model. Each model is valid 5 years from its epoch."""
    head = open(cof or WMM_COF).readline().split()
    epoch = float(head[0])
    return (head[1] if len(head) > 1 else "WMM"), epoch, epoch + 5


def wmm_declination(lat, lon, year, cof):
    """Magnetic declination (degrees, +east) from a WMM .COF file. Standard WMM algorithm (NOAA TR).
    Raises ModelExpired outside the model's 5-year span: an old model drifts ~0.1-0.2 deg/yr at random, and a
    compass bearing taken from a stale value is wrong with nothing to show it. Replace share/WMM_*.COF with
    NOAA's current one (https://www.ncei.noaa.gov/products/world-magnetic-model) to fix."""
    import math
    lines = open(cof).read().splitlines(); epoch = float(lines[0].split()[0]); N = 12
    if not (epoch <= year < epoch + 5):
        raise ModelExpired(f"magnetic model {lines[0].split()[1] if len(lines[0].split()) > 1 else ''} covers "
                           f"{epoch:.0f}-{epoch + 5:.0f}, not {year:.1f}")
    g = [[0.0] * (N + 1) for _ in range(N + 1)]; h = [[0.0] * (N + 1) for _ in range(N + 1)]
    for l in lines[1:]:
        p = l.split()
        if len(p) < 6 or p[0].startswith('9999'): break
        n, m = int(p[0]), int(p[1]); dt = year - epoch
        g[n][m] = float(p[2]) + dt * float(p[4]); h[n][m] = float(p[3]) + dt * float(p[5])
    a, b = 6378.137, 6356.7523142; re_ = 6371.2
    rlat, rlon = math.radians(lat), math.radians(lon)
    # geodetic (alt 0) -> geocentric spherical
    e2 = 1 - (b / a) ** 2; Rc = a / math.sqrt(1 - e2 * math.sin(rlat) ** 2)
    p_ = Rc * math.cos(rlat); z = Rc * (1 - e2) * math.sin(rlat)
    r = math.hypot(p_, z); phi = math.asin(z / r)
    # Schmidt semi-normalised associated Legendre P[n][m](sin phi) and derivative wrt phi
    s, c = math.sin(phi), math.cos(phi)
    P = [[0.0] * (N + 2) for _ in range(N + 2)]; dP = [[0.0] * (N + 2) for _ in range(N + 2)]
    P[0][0] = 1.0
    for n in range(1, N + 1):
        for m in range(0, n + 1):
            if n == m:
                k = math.sqrt(1 - 1 / (2 * n)) if n > 1 else 1.0
                P[n][m] = k * c * P[n - 1][m - 1]
                dP[n][m] = k * (c * dP[n - 1][m - 1] - s * P[n - 1][m - 1])
            else:
                k1 = (2 * n - 1) / math.sqrt(n * n - m * m)
                k2 = math.sqrt(((n - 1) ** 2 - m * m) / (n * n - m * m)) if n > 1 else 0.0
                P[n][m] = k1 * s * P[n - 1][m] - (k2 * P[n - 2][m] if n > 1 else 0)
                dP[n][m] = k1 * (s * dP[n - 1][m] + c * P[n - 1][m]) - (k2 * dP[n - 2][m] if n > 1 else 0)
    # the m=1.. recursion above uses unnormalised diagonal for m=1 with n=1 handled by k=1
    X = Y = Z = 0.0
    for n in range(1, N + 1):
        f = (re_ / r) ** (n + 2)
        for m in range(0, n + 1):
            cm, sm = math.cos(m * rlon), math.sin(m * rlon)
            X += -f * (g[n][m] * cm + h[n][m] * sm) * dP[n][m]
            Y += f * m * (g[n][m] * sm - h[n][m] * cm) * P[n][m] / c
            Z += -(n + 1) * f * (g[n][m] * cm + h[n][m] * sm) * P[n][m]
    d = phi - rlat                                  # rotate to geodetic
    Xg = X * math.cos(d) - Z * math.sin(d)
    return math.degrees(math.atan2(Y, Xg))


def cmd_area_viewshed(ctx):
    """What can be seen from a point (or what can see it): a red overlay in the area's project."""
    need_root(ctx); need_tool("gdal_viewshed")
    d = lib_dir(ctx, "areas", ctx.a.name); dem = d / "dem.tif"
    if not readable(dem):
        raise CliError(E_NOTFOUND, f"{dem} missing", f"build the area first: lmap area create {ctx.a.name} ...")
    try:
        lat, lon = [float(x) for x in ctx.a.point.split(",")]
    except ValueError:
        raise CliError(E_USAGE, f"bad --point {ctx.a.point!r}", "lat,lon e.g. --point=-19.0,146.2")
    info = json.loads(run(["gdalinfo", "-json", dem], ctx))
    wkt = info["coordinateSystem"]["wkt"]
    from osgeo import osr
    osr.UseExceptions()
    a, b = osr.SpatialReference(), osr.SpatialReference(); a.ImportFromEPSG(4326); b.ImportFromWkt(wkt)
    for x in (a, b):
        x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x, y, _ = osr.CoordinateTransformation(a, b).TransformPoint(lon, lat)
    gt = info["geoTransform"]; w, h = info["size"]
    if not (gt[0] <= x <= gt[0] + w * gt[1] and gt[3] + h * gt[5] <= y <= gt[3]):
        raise CliError(E_USAGE, "point is outside this area's DEM")
    label = ctx.a.label or f"{lat:.4f},{lon:.4f}"
    if not re.fullmatch(r"[A-Za-z0-9_.,-]{1,40}", label):
        raise CliError(E_USAGE, f"bad --label {label!r}", "letters, digits, - _ . ,")
    out = d / f"viewshed-{label}.tif"
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "area.viewshed", "out": str(out), "mga": [round(x), round(y)],
                "observer_m": ctx.a.height, "target_m": ctx.a.target_height, "radius_km": ctx.a.radius_km}
    part = d / f"viewshed-{label}.part.tif"
    try:
        # curvature + refraction on (-cc 0.85714 = standard 4/3 earth); vv 255 visible, iv 0 not
        run(["gdal_viewshed", "-q", "-ox", x, "-oy", y, "-oz", ctx.a.height, "-tz", ctx.a.target_height,
             "-md", ctx.a.radius_km * 1000, "-cc", "0.85714", "-vv", "255", "-iv", "0", "-ov", "0",
             "-of", "GTiff", "-co", "COMPRESS=DEFLATE", dem, part], ctx)
        part.rename(out)
    except BaseException:
        part.unlink(missing_ok=True); raise
    from osgeo import gdal
    a_ = gdal_open(out).ReadAsArray(); seen = int((a_ == 255).sum()) * abs(gt[1] * gt[5]) / 1e6
    res = {"ok": True, "action": "area.viewshed", "file": out.name, "visible_km2": round(seen, 1),
           "observer": {"lat": lat, "lon": lon, "height_m": ctx.a.height}, "target_height_m": ctx.a.target_height,
           "note": "terrain only (no trees/buildings): real visibility is LESS"}
    if not ctx.a.no_project:
        try:
            meta = json.loads((d / "area.json").read_text())
            res["qgis"] = area_qgis(ctx, d, ctx.a.name, meta["crs"], meta["bbox"], meta["level"])
        except (CliError, OSError, ValueError, KeyError) as e:
            res["qgis"] = {"error": str(e)}
    return res
