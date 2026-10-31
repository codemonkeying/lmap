# lmap: map. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- map: one command, a printed sheet anywhere
# Builds only what the sheet's styles draw, for the sheet's own footprint (+2 km), into <root>/maps/<name>/
# (an area folder: area.json + <name>.qgz, so `area print/export` work on it too). Files already built for an
# area or map that holds this box are copied (clipped), not rebuilt. Then prints one PDF per style.
MAP_TOPO = {"dem", "contours-dem", "qld-contours", "contours-lidar", "contours-nsw5", "contours-best", "osm", "field", "grid",
            "spot-heights", "vegetation", "sea", "qld-surface-water", "qld-roads", "qld-parks", "nsw-roads", "nsw-water", "nsw-contours"}
MAP_PHOTO = {"sat", "sat-even"}
MAP_EXTRA = {"aerial": {"aerial"}, "topo-raster": {"topo"}, "hazard": {"qld-hazard", "qld-flood", "qld-fire"},
             "3d": {"dem3d", "slope", "hls"}, "infra": {"qld-infra", "qld-water"}, "cadastre": {"qld-cadastre"}}


def map_place(ctx, q):
    """lat, lon of a place name from the national OSM places (exact name first, then prefix; biggest place)."""
    g = ctx.path(ctx.sources.get("osm-australia", {}).get("dest", "data/australia/osm/osm-australia.gpkg"))
    if not g.is_file():
        raise CliError(E_NOTFOUND, f"--place needs {g}", "lmap data fetch osm-australia --yes, or use --center")
    c = ro_sqlite(g)
    try:
        return _map_place(c, q)
    finally:
        c.close()


def _map_place(c, q):
    import sqlite3
    rank = "CASE place WHEN 'city' THEN 0 WHEN 'town' THEN 1 WHEN 'suburb' THEN 2 WHEN 'village' THEN 3 ELSE 4 END"
    gc = c.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name='places'").fetchone()[0]
    def look(where, arg):
        return c.execute(f"SELECT name, place, ST_MinX({gc}), ST_MinY({gc}) FROM places WHERE {where} "
                         f"ORDER BY {rank} LIMIT 6", arg).fetchall()
    try:
        c.enable_load_extension(True); c.load_extension("mod_spatialite")
    except (sqlite3.Error, AttributeError):           # no spatialite / Python without extension loading
        pass
    try:
        rows = look("name = ? COLLATE NOCASE", (q,)) or look("name LIKE ? COLLATE NOCASE", (q + "%",))
    except sqlite3.OperationalError:                 # no spatialite: read the GPKG blob header (envelope)
        import struct
        def look(where, arg):
            out = []
            for n, pl, b in c.execute(f"SELECT name, place, {gc} FROM places WHERE {where} ORDER BY {rank} LIMIT 6", arg):
                flags = b[3]; off = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
                x, y = struct.unpack("<dd" if flags & 1 else ">dd", b[off + 5:off + 21]); out.append((n, pl, x, y))
            return out
        rows = look("name = ? COLLATE NOCASE", (q,)) or look("name LIKE ? COLLATE NOCASE", (q + "%",))
    if not rows:
        raise CliError(E_NOTFOUND, f"no place called {q!r} in OSM places", "use --center=lat,lon (peaks aren't places)")
    n, pl, x, y = rows[0]
    return y, x, {"name": n, "place": pl, "others": [f"{r[0]} ({r[1]}, {r[3]:.3f},{r[2]:.3f})" for r in rows[1:]]}


def map_footprint(lat, lon, scale, paper, portrait):
    """lon/lat box of one sheet's map frame (+2 km all round, so a moved --center still fits)."""
    import math
    W, H = PAPER[paper]
    if portrait:
        W, H = H, W
    fw, fh = sheet_map_mm(W, H)
    mw = fw / 1000 * scale / 2 + 2000; mh = fh / 1000 * scale / 2 + 2000
    dla = mh / 110574; dlo = mw / (111320 * math.cos(math.radians(lat)))
    return [round(lon - dlo, 5), round(lat - dla, 5), round(lon + dlo, 5), round(lat + dla, 5)]


def _sig_core(s):
    """A signature without the box/extras: what it was made FROM (inputs, kind, code, no age)."""
    try:
        j = json.loads(s)
        p = j.get("p") or {}
        return json.dumps([j.get("v"), j.get("in"), {k: p.get(k) for k in ("kind", "key", "crs", "fs", "code", "live")}],
                          sort_keys=True, default=str)
    except (ValueError, TypeError, AttributeError):
        return None


def map_reuse(ctx, d, bbox, items, crs, lvl):
    """Copy files for items [(key, kind, src, ex)] out of any built area/map whose box holds ours (clipped). Only from
    a source in the same CRS whose item built OK at the same or finer resolution (osm: same level), and whose
    file's signature says it was made from today's inputs: a stale neighbour is never copied. The copy gets its own
    signature. Newest source first."""
    got = {}
    by = {k: (kind, src, ex) for k, kind, src, ex in items}
    want = {k: ex for k, (_, _, ex) in by.items()}
    sigs = {}
    names = {"dem": "dem.tif", "vegetation": "vegetation.tif", "sat": "sat.tif", "aerial": "aerial.tif", "topo": "topo.tif",
             "qld-surface-water": "qld-surface-water.gpkg", "osm": "osm.gpkg",       # the slow clips: big state files / pbf scan
             "nsw-water": "nsw-water.gpkg", "nsw-contours": "nsw-contours.gpkg",
             "contours-nsw5": "contours-nsw5.gpkg"}                            # 5 m model: a download per box
    srcs = sorted((aj for k in ("areas", "maps") for aj in lib_jsons(ctx, k)),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for aj in srcs:
            if aj.parent == d:
                continue
            try:
                m = json.loads(aj.read_text())
            except ValueError:
                continue
            if not contains(m.get("bbox", [0, 0, 0, 0]), bbox) or m.get("crs") != crs:
                continue
            plan = {i.get("key"): i for i in m.get("items") or []}
            for k, fn in names.items():
                f = aj.parent / fn
                b = (m.get("built") or {}).get(k)
                if isinstance(b, dict) and "file" in b and "from" not in b and k in plan:   # re-run: {"unchanged"}
                    b = {**b, "from": plan[k].get("from"), **({"res_m": plan[k]["res_m"]} if "res_m" in plan[k] else {})}
                if not (k in want and k not in got and readable(f) and not readable(d / fn) and isinstance(b, dict)
                        and "file" in b and b.get("from") == want[k]["from"]):
                    continue
                if "res" in want[k] and not (b.get("res_m") or 1e9) <= want[k]["res"] * 1.001:
                    continue
                if b.get("reused_from") and by[k][0] in ("sat", "aerial", "topo") and ctx.quality != "original":
                    continue                     # photos: one lossy re-encode at most (no copy of a copy)
                if k == "osm" and m.get("level") != lvl:
                    continue
                if k not in sigs:
                    sigs[k] = item_sig(d, k, *by[k], bbox, crs)[2]
                want_sig = sigs[k]
                have = _sig_read(f)
                if not have or have == "STALE" or _sig_core(have) is None or _sig_core(have) != _sig_core(want_sig):
                    continue                     # neighbour's copy is not from today's inputs: build
                if True:
                    vec = fn.endswith(".gpkg")
                    part = ctx.scratch() / f"map-{d.name}-{fn}.{os.getpid()}.part.{'gpkg' if vec else 'tif'}"
                    try:
                        if vec:
                            run(["ogr2ogr", "-f", "GPKG", "-spat", *bbox, "-spat_srs", "EPSG:4326", part, f], ctx)
                        else:
                            run(["gdal_translate", "-q", "-projwin_srs", "EPSG:4326", "-projwin", bbox[0], bbox[3], bbox[2],
                                 bbox[1], "-of", "COG", *area_raster_co(ctx, by[k][0], f), f, part], ctx)
                        if part.is_file() if vec else gdal_ok(part):
                            install_file(part, d / fn); sig_write(d / fn, want_sig)
                            got[k] = {**b, "reused_from": aj.parent.name}
                    except CliError:
                        pass
                    finally:
                        part.unlink(missing_ok=True)
    return got


def cmd_map(ctx):
    """lmap map: a printed sheet (PDF) of anywhere, built just for that sheet."""
    need_root(ctx); contour_widths(ctx.a)          # bad width: usage error before any work
    need_tool("gdalwarp", "gdal_translate", "ogr2ogr", "gdalinfo")
    a = ctx.a
    scale = {"25k": 25000, "50k": 50000, "100k": 100000}.get(str(a.scale).lower(), None)
    if scale is None:
        try:
            scale = int(str(a.scale).replace(",", ""))
        except ValueError:
            raise CliError(E_USAGE, f"--scale {a.scale!r}: 25k, 50k, 100k or a number (e.g. 40000)")
    if not 5000 <= scale <= 250000:
        raise CliError(E_USAGE, f"--scale 1:{scale}: use 1:5000 .. 1:250000")
    where = [x for x in (a.center, a.bbox, a.place) if x]
    if len(where) != 1:
        raise CliError(E_USAGE, "say where: one of --center=lat,lon, --bbox=w,s,e,n or --place NAME")
    place = None
    if a.place:
        lat, lon, place = map_place(ctx, a.place)
    elif a.center:
        try:
            lat, lon = (float(x) for x in a.center.split(","))
        except ValueError:
            raise CliError(E_USAGE, f"--center wants lat,lon (e.g. --center=-19.26,146.82), got {a.center!r}")
    if a.bbox:
        box = list(parse_bbox(a.bbox)); lat, lon = (box[1] + box[3]) / 2, (box[0] + box[2]) / 2
        bw = (box[2] - box[0]) * 111320 * math.cos(math.radians(lat)); bh = (box[3] - box[1]) * 110574
        def frame(port):
            W, H = PAPER[a.paper]
            W, H = (H, W) if port else (W, H)
            fw_, fh_ = sheet_map_mm(W, H)
            return fw_ / 1000 * scale, fh_ / 1000 * scale
        fw, fh = frame(a.portrait)
        if (bw > fw * 1.001 or bh > fh * 1.001) and not a.portrait:
            pw, ph = frame(True)
            if bw <= pw * 1.001 and bh <= ph * 1.001:
                a.portrait = True; fw, fh = pw, ph       # tall box: portrait fits it
        if bw > fw * 1.001 or bh > fh * 1.001:
            k = max(bw / fw, bh / fh)
            raise CliError(E_USAGE, f"--bbox is {bw / 1000:.1f} x {bh / 1000:.1f} km; one {a.paper} sheet at 1:{scale:,} "
                           f"shows {fw / 1000:.1f} x {fh / 1000:.1f} km",
                           f"--scale {int(math.ceil(scale * k / 5000) * 5000)}, a bigger --paper, or a smaller box")
    if not (-45 <= lat <= -9 and 112 <= lon <= 154):
        raise CliError(E_USAGE, f"{lat:.3f},{lon:.3f} is outside Australia (lat first: --center=-19.26,146.82)")
    styles = ["topo", "photo"] if a.style == "both" else [a.style]
    extra = set()
    for x in (a.add or []):
        for y in x.split(","):
            if y and y not in MAP_EXTRA:
                raise CliError(E_USAGE, f"--add {y}: one of {', '.join(MAP_EXTRA)}")
            extra |= MAP_EXTRA.get(y, set())
    name = a.name or (re.sub(r"[^a-z0-9]+", "-", place["name"].lower()).strip("-") if place
                      else f"{lat:.3f}_{lon:.3f}".replace("-", "s"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", name):
        raise CliError(E_USAGE, f"bad map name {name!r}", "letters, digits, - _ .")
    bbox = map_footprint(lat, lon, scale, a.paper, a.portrait)
    lvl = "street" if scale <= 30000 else "local"
    want = set(MAP_TOPO) | (MAP_PHOTO if "photo" in styles else set()) | extra
    items = [i for i in area_plan(ctx, bbox, lvl) if i[0] in want]
    d = lib_dir(ctx, "maps", name, lat, lon)
    crs = mga_epsg(lon)
    km2 = ((bbox[2] - bbox[0]) * 111.32 * math.cos(math.radians(lat))) * ((bbox[3] - bbox[1]) * 110.574)
    have = {k for k in ("dem", "vegetation", "sat", "aerial") if readable(d / f"{k}.tif")}
    summary = {"name": name, "dir": str(d), "center": [round(lat, 5), round(lon, 5)], "bbox": bbox,
               "scale": f"1:{scale:,}", "paper": f"{a.paper} {'portrait' if a.portrait else 'landscape'}",
               "styles": styles, "km2": round(km2), "crs": crs, "level": lvl,
               "items": [{"key": k, "from": ex["from"], **({"res_m": ex["res"]} if "res" in ex else {})}
                         for k, kind, src, ex in items],
               "pdfs": [str(d / f"print-{st}-{scale // 1000}k.pdf") for st in styles]}
    if place:
        summary["place"] = place
    notes = []
    live = [k for k, kind, *_ in items if kind == "arcgis"]
    if live:
        notes.append(f"needs internet for: {', '.join(live)} (`lmap qld fetch` stores them offline)")
    if notes:
        summary["notes"] = notes
    if a.dry_run:
        return {"ok": True, "dry_run": True, "action": "map", **summary, "already_built": sorted(have)}
    if not a.yes:
        raise CliError(E_REFUSED, f"refusing to build {d} without --yes", "preview: add --dry-run")
    install_signals()
    t0 = time.time()
    d.mkdir(parents=True, exist_ok=True)
    built = {}
    with Lock(STATE / "locks" / f"map-{name}.lock"):
        for x in d.glob("*.part*"):
            x.unlink(missing_ok=True)
        try:
            old = json.loads((d / "area.json").read_text())
        except (OSError, ValueError):
            old = None
        if a.refresh or (old and (not contains(old.get("bbox", [0, 0, 0, 0]), bbox) or old.get("crs") != crs
                                  or old.get("level") != lvl)):
            for x in list(d.glob("*.gpkg")) + list(d.glob("*.tif")) + list(d.glob("*.aux.xml")) \
                    + list(d.glob("*.gpkg.lmap-sig")) + list(d.glob("*.tif.lmap-sig")):
                x.unlink(missing_ok=True)                 # other scale/box/paper: its layers (+ their sigs) don't fit
        if not a.refresh and not getattr(a, "rebuild", False):
            built.update(map_reuse(ctx, d, bbox, items, crs, lvl))
        todo = [i for i in items if i[0] not in built]
        dag, keys = Dag(ctx, width=min(max(1, len(todo)), 6)), {k for k, *_ in todo}
        for key, kind, src, ex in todo:
            dag.add(key, lambda key=key, kind=kind, src=src, ex=ex: area_build_item(ctx, d, key, kind, src, ex, bbox, crs),
                    deps=[x for x in AREA_DEPS.get(key, ()) if x.lstrip("?") in keys], label=f"<- {ex['from']}")
        for key, s_ in dag.run().items():
            built[key] = s_.result if s_.state == "done" else {"error": s_.error}
        map_stop()
        if getattr(ctx.a, "contours_from", None) and "error" in built.get("contours-best", {}):
            # an asked-for contour source that can't be had: no sheet (never silently another source)
            raise CliError(E_USAGE, f"contours: {built['contours-best']['error']}", "pick another --contours-from (or leave it out)")
        lic = {ex["from"]: ctx.sources.get(ex["from"], {}).get("licence", "") for _, _, _, ex in items}
        imp = import_clip(ctx, d, bbox, crs)          # the user's imported layers that overlap the sheet
        if imp:
            built["imported"] = imp
        meta = {**summary, "built": built, "licences": lic, "made": time.strftime("%Y-%m-%d %H:%M"), "lmap": VERSION}
        (d / "area.json").write_text(json.dumps(meta, indent=1))
        t_build = round(time.time() - t0)
        ctx.log(f"layers ready in {t_build}s; QGIS project")
        tq = time.time()
        # project + print from a local copy: over a network share (CIFS) every GeoPackage open costs ~5 s and the
        # project save ~45 s (Mt Cooper: 104 s); local it's ~3 s. The sheet's files are small (~30-60 MB).
        loc = ctx.scratch() / f"map-{name}-{os.getpid()}"
        for x in ctx.scratch().glob(f"map-{name}-*"):     # left by a killed run
            shutil.rmtree(x, ignore_errors=True) if x.is_dir() else x.unlink(missing_ok=True)
        try:
          loc.mkdir(parents=True)
          for f in d.iterdir():
            if f.is_file() and f.suffix in (".gpkg", ".tif", ".json") and ".part" not in f.name:
                shutil.copy2(f, loc / f.name)
          for f in (d / f"{name}.qgz", d / f"{name}.qgz.lmap-sig"):    # the project + its sig: unchanged -> no rebuild
            if f.is_file():
                shutil.copy2(f, loc / f.name)
          q = area_qgis(ctx, loc, name, crs, bbox, lvl)
          if "file" not in q:
            raise CliError(E_FAIL, f"map project failed: {q}")
          import zipfile
          with zipfile.ZipFile(loc / q["file"]) as z:       # the copy must point at ./files, not scratch
            if any(str(loc).encode() in z.read(n) for n in z.namelist() if n.endswith(".qgs")):
                raise CliError(E_FAIL, "map project has scratch paths in it (bug)")
          if not q.get("unchanged"):
              install_file(loc / q["file"], d / q["file"], link=False)
              sig_ = loc / (q["file"] + ".lmap-sig")
              if sig_.is_file():
                  install_file(sig_, d / sig_.name, link=False)
          t_qgis = round(time.time() - tq)
          pdfs, t_print, intervals, georef = [], {}, {}, {}
          gone = {k for k, v in built.items() if isinstance(v, dict) and ("error" in v or "skipped" in v)}
          gone |= {k for st in styles for k in STYLE_NEEDS[st]} - set(built)     # no source for it here at all
          for st in styles:
            map_stop()
            need_gone = sorted(gone & STYLE_NEEDS[st])
            ctx.log(f"printing {st}")
            pa = argparse.Namespace(**{**vars(a), "name": name, "scale": scale, "style": st, "center": f"{lat},{lon}",
                                       "label": f"{st}-{scale // 1000}k", "title": a.title or None,
                                       "contours": "best", "interval": a.interval, "dry_run": False,
                                       "incomplete": ", ".join(f"no {STYLE_NAMES.get(k, k)}" for k in need_gone) or None})
            c2 = copy.copy(ctx); c2.a = pa; c2.eta_run = NullRun(time.time())   # the map's own steps report to RUN, not the print
            tp = time.time()
            r = print_sheet(c2, loc)
            out = d / Path(r["file"]).name
            shutil.copyfile(r["file"], out); pdfs.append(str(out))
            out.with_suffix(".json").unlink(missing_ok=True)
            if Path(r["file"]).with_suffix(".json").is_file():          # what was drawn (verify reads the interval)
                shutil.copyfile(Path(r["file"]).with_suffix(".json"), out.with_suffix(".json"))
            t_print[st] = round(time.time() - tp); intervals[st] = r.get("interval")
            if r.get("georef_vs_grid_m"):
                georef[st] = r["georef_vs_grid_m"]
        finally:
            shutil.rmtree(loc, ignore_errors=True)
    failed = {k: v["error"] for k, v in built.items() if isinstance(v, dict) and "error" in v}
    skipped = {k: v["skipped"] for k, v in built.items() if isinstance(v, dict) and "skipped" in v}
    res = {"ok": True, "action": "map", **summary, "pdfs": pdfs, "seconds": round(time.time() - t0),
           "build_s": t_build, "qgis_s": t_qgis, "print_s": t_print, "contour_interval_m": intervals, **({"georef_vs_grid_m": georef} if georef else {}), "reused": {k: v["reused_from"] for k, v in built.items() if "reused_from" in v}}
    absent = {k: "no source covers this sheet" for st in styles for k in STYLE_NEEDS[st] if k not in built}
    if failed or skipped or absent:
        res["missing_layers"] = {**absent, **failed, **skipped}
        res["hint"] = "sheet printed without those; re-run (online) to add them"
    bad = {st: sorted(set(res.get("missing_layers", {})) & STYLE_NEEDS[st]) for st in styles}
    bad = {st: v for st, v in bad.items() if v}
    if bad:                                    # the PDF is kept (offline: better than nothing), marked INCOMPLETE on it
        res["ok"] = False; res["exit_code"] = E_UPSTREAM; res["incomplete"] = bad
        res["error"] = "sheet incomplete: " + "; ".join(f"{st} without {', '.join(v)}" for st, v in bad.items())
        res["hint"] = "the PDF is marked INCOMPLETE; re-run online (or inside a stored area) to add them"
    return res


# layers a sheet style can't do without: missing one = the run fails (the PDF still prints, stamped INCOMPLETE)
STYLE_NEEDS = {"topo": {"dem", "osm"}, "photo": {"dem", "osm", "sat"}}
STYLE_NAMES = {"sat": "satellite imagery", "dem": "terrain (contours)", "osm": "roads/places"}


def map_stop():
    if ABORT.is_set():
        raise CliError(E_ABORTED, "map stopped (stop --now)", "re-run the same command to continue")
    if DRAIN.is_set():
        raise CliError(E_DRAINED, "map stopped", "re-run the same command to continue (built layers kept)")


PHOTO_CONTOURS = "orange"          # --photo-contours default
IMAGE_MAX_PX = 2_000_000_000       # ~2 Gpx: a city-sized area at just over 1 m. Bigger needs --force
IMAGE_COPY_MAX_PX = 500_000_000   # --jpeg / --kmz copies: a viewer decodes all of it


def cmd_area_image(ctx):
    """The area's map (ticked layers, contours as in print) as one big zoomable GeoTIFF (COG, JPEG/WEBP inside,
    overviews), optional plain JPEG and KMZ copies. Detail: --res m/px, or --scale + --dpi."""
    need_root(ctx)
    d = sheet_dir(ctx, ctx.a.name); meta_p = d / "area.json"; qgz = d / f"{ctx.a.name}.qgz"
    con_a, iv_a = ctx.a.contours, ctx.a.interval
    if iv_a != 10 and con_a not in ("best", "dem", "lidar"):
        raise CliError(E_USAGE, f"--interval {iv_a} works with --contours best, dem or lidar")
    label = ctx.a.label or (f"{ctx.a.res:g}m" if ctx.a.res is not None else f"{ctx.a.scale / 1000:g}k-{ctx.a.dpi}dpi")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", label):
        raise CliError(E_USAGE, f"bad --label {label!r}")
    if ctx.a.res is not None and not 0.1 <= ctx.a.res <= 1000:
        raise CliError(E_USAGE, "--res must be 0.1-1000 m/px")
    if not 50 <= ctx.a.dpi <= 600:
        raise CliError(E_USAGE, "--dpi must be 50-600")
    if not 1000 <= ctx.a.scale <= 10_000_000:
        raise CliError(E_USAGE, "--scale must be 1000-10000000")
    box = None
    if ctx.a.bbox:
        try:
            box = [float(v) for v in ctx.a.bbox.split(",")]; assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
        except (ValueError, AssertionError):
            raise CliError(E_USAGE, f"bad --bbox {ctx.a.bbox!r}: w,s,e,n degrees, e.g. --bbox=146.75,-19.3,146.85,-19.22")
    if not readable(meta_p) or not readable(qgz):
        raise CliError(E_NOTFOUND, f"area {ctx.a.name} not built", f"lmap area create {ctx.a.name} ...")
    meta = json.loads(meta_p.read_text()); crs = meta["crs"]
    con, iv, best, note = area_contours(d, con_a, iv_a)
    # detail: m/px; the map is styled as at scale S on a dpi printer (S * 0.0254 / dpi = m/px)
    dpi = ctx.a.dpi
    _cx = 6.0 if ctx.a.codec in IMAGE_LOSSLESS or getattr(ctx, "quality", "standard") == "original" else 1.0     # size vs JPEG (LZW measured 5.5x on a topo image)
    res = ctx.a.res if ctx.a.res is not None else ctx.a.scale * 0.0254 / dpi
    scale = round(res * dpi / 0.0254)
    if box:
        a_ = meta["bbox"]
        box = [max(box[0], a_[0]), max(box[1], a_[1]), min(box[2], a_[2]), min(box[3], a_[3])]
        if box[0] >= box[2] or box[1] >= box[3]:
            raise CliError(E_USAGE, f"--bbox is outside area {ctx.a.name} {meta['bbox']}")
    _, g = area_box_geom(crs, box or meta["bbox"]); ex = g.GetEnvelope()      # (minx, maxx, miny, maxy)
    x0, x1 = ex[0] // res * res, -(-ex[1] // res) * res; y0, y1 = ex[2] // res * res, -(-ex[3] // res) * res
    W, H = round((x1 - x0) / res), round((y1 - y0) / res)
    px = W * H
    out = d / f"image-{label}.tif"; jpg = out.with_suffix(".jpg"); kmz = out.with_suffix(".kmz")
    plan = {"action": "area.image", "file": str(out), "jpeg": str(jpg) if ctx.a.jpeg else None,
            "kmz": str(kmz) if ctx.a.kmz else None, "m_per_px": round(res, 3), "styled_as": f"1:{scale} at {dpi} dpi",
            "pixels": [W, H], "megapixels": round(px / 1e6, 1), "est_mb": round(px * 3 * 0.06 * _cx / 1e6, 1),
            "codec": ctx.a.codec, "contours": con, "interval": iv, "crs": crs, "bbox": box or meta["bbox"]}
    if px > IMAGE_MAX_PX and not ctx.a.force:
        raise CliError(E_USAGE, f"{W}x{H} px ({px / 1e9:.1f} Gpx) is over the {IMAGE_MAX_PX / 1e9:.0f} Gpx limit",
                       "coarser --res, a smaller --bbox, or --force")
    for on, what in ((ctx.a.jpeg, "--jpeg"), (ctx.a.kmz, "--kmz")):
        if on and px > IMAGE_COPY_MAX_PX:   # a viewer must decode a whole JPEG; Avenza/Earth choke on huge KMZs
            raise CliError(E_USAGE, f"{what}: {px / 1e6:.0f} Mpx is over the {IMAGE_COPY_MAX_PX // 10**6} Mpx limit "
                           "for copies", "coarser --res or a smaller --bbox (the GeoTIFF has no such limit)")
    mpx = px / 1e6                                # units: megapixels; copies (jpeg/kmz) are their own phases
    ik = f"{ctx.a.codec}-{getattr(ctx, 'quality', 'standard')}-{dpi}dpi"     # history per setting that changes the cost
    eta = WorkEta([("image-setup", 1, "image: preparing"), (f"image-draw-{dpi}dpi", mpx, "image: drawing"), (f"image-cog-{ik}", mpx, "image: packing COG")]
                  + ([("image-jpeg", mpx, "image: JPEG copy")] if ctx.a.jpeg else [])
                  + ([("image-kmz", mpx, "image: KMZ copy")] if ctx.a.kmz else []))
    plan["eta_s"] = eta.left()                    # pre-start: past runs' s/Mpx; None = no run measured yet
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write {out.name} without --yes",
                       f"preview: lmap area image {ctx.a.name} --dry-run")
    need_tool("gdal_translate")
    install_signals()
    files, done = [], []
    tw = time.time()
    with Lock(STATE / "locks" / f"area-image-{ctx.a.name}.lock"):
        eta.t_start += time.time() - tw          # waiting for another image of this area is not set-up cost
        sc = ctx.scratch() / f"image-{ctx.a.name}-{label}-{os.getpid()}"; sc.mkdir(parents=True, exist_ok=True)
        raw, cog = sc / "raw.tif", sc / "cog.tif"
        # raw ZSTD ~ 1 byte/px for a map; COG + copies on top
        need_b = px * (1.0 + 0.3 * _cx + (0.3 if ctx.a.jpeg else 0) + (0.4 if ctx.a.kmz else 0)) + 200e6
        free = shutil.disk_usage(sc).free
        if free < need_b:
            shutil.rmtree(sc, ignore_errors=True)
            raise CliError(E_FAIL, f"scratch {sc.parent} has {free / 1e9:.1f} GB free, needs ~{need_b / 1e9:.1f} GB")
        args = {"qgz": str(qgz), "contours": con, "interval": iv, "best": str(best), "extent": [x0, y0, x1, y1],
                "res": res, "size": [W, H], "dpi": dpi, "crs": crs, "out": str(raw), "margin": 256,
                "tile": int(os.environ.get("LMAP_IMAGE_TILE", "4096"))}
        try:
            eta.phase(0, t0=eta.t_start); ctx.log(f"drawing {W}x{H} px ({res:.2f} m/px, styled as 1:{scale})"); eta.phase(1)
            # tracked child in its own group: an abort / lmap dying takes QGIS with it (no orphan render)
            pr = child(subprocess.Popen([sys.executable, "-c", IMAGE_PY, json.dumps(args)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=_pdeath,
                                        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}))
            errs, outs = [], []
            def _read():
                for l in pr.stderr:
                    if l.startswith("tile "):
                        ctx.vlog(l.strip()); f = tile_line_frac(l)
                        if f is not None:
                            eta.progress(f, f" {l.strip()}")
                    else:
                        errs.append(l)
            def _out():
                outs.append(pr.stdout.read())
            rt = threading.Thread(target=_read, daemon=True); rt.start()
            ro = threading.Thread(target=_out, daemon=True); ro.start()
            try:
                while pr.poll() is None:            # a stop must not wait for a slow tile
                    if DRAIN.is_set():
                        pr.terminate()
                        try:
                            pr.wait(10)
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    try:
                        pr.wait(1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if pr.poll() is None:
                    try:
                        os.killpg(pr.pid, signal.SIGKILL)
                    except OSError:
                        pr.kill()
                pr.wait(); unchild(pr)
            rt.join(5); ro.join(5); so = "".join(outs); rc = pr.returncode
            if DRAIN.is_set():
                raise CliError(E_ABORTED, "stopped; nothing written")
            line = next((l for l in so.splitlines() if l.startswith("{")), None)
            if line and json.loads(line).get("no_contours"):
                raise CliError(E_NOTFOUND, f"--contours {con}: the project has no such contour layer (image not written)",
                               f"lmap area create {ctx.a.name} ... --yes (rebuilds the project), or --contours ticked")
            if rc or not line or not raw.is_file():
                child_fail(ctx, "image", rc, errs, None if rc else ("no result line" if not line else "no image written"))
            ctx.log("packing COG + overviews"); eta.phase(2)
            co = ["-co", "BIGTIFF=YES", "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=AVERAGE",
                  "-co", "NUM_THREADS=ALL_CPUS", "-co", f"COMPRESS={ctx.a.codec.upper()}"]
            if ctx.a.codec in IMAGE_LOSSLESS:
                co += ["-co", "PREDICTOR=2"]             # lossless already: --quality original changes nothing
            elif getattr(ctx, "quality", "standard") == "original":    # lossless: text/lines/photo pixels exact
                co = co[:-2] + ["-co", "COMPRESS=WEBP", "-co", "QUALITY=100"]
            else:
                co += ["-co", "QUALITY=85"]
            run(["gdal_translate", "-q", "-of", "COG"] + co + [raw, cog], ctx)
            if not gdal_ok(cog):
                raise CliError(E_FAIL, f"{out.name}: COG failed gdalinfo")
            stage = [(cog, out)]                # everything is built in scratch first, then installed
            if ctx.a.jpeg:
                eta.phase(3); t = sc / "x.jpg"
                run(["gdal_translate", "-q", "-of", "JPEG", "-co", "QUALITY=85", "-co", "WORLDFILE=YES", raw, t], ctx)
                stage += [(t, jpg), (t.with_suffix(".wld"), jpg.with_suffix(".wld"))]
                if (aux := sc / "x.jpg.aux.xml").is_file():
                    stage.append((aux, Path(str(jpg) + ".aux.xml")))
            if ctx.a.kmz:
                eta.phase(len(eta.ph) - 1); t = sc / "x.kmz"
                run(["gdal_translate", "-q", "-of", "KMLSUPEROVERLAY", "-co", "FORMAT=JPEG", raw, t], ctx)
                stage.append((t, kmz))
            raw.unlink(missing_ok=True)
            for s_, o_ in stage:
                if DRAIN.is_set():
                    raise CliError(E_ABORTED, "stopped; nothing written")
                install_file(s_, o_); done.append(o_); s_.unlink(missing_ok=True)
            eta.finish()                        # install is part of the last phase's measured cost
            files = [o_ for o_ in done if o_.suffix in (".tif", ".jpg", ".kmz")]
        except BaseException:
            for o_ in done:                     # all or nothing: no half set of new files left behind
                o_.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(sc, ignore_errors=True)
    return {"ok": True, **plan, "files": [str(f) for f in files], "bytes": sum(f.stat().st_size for f in files)}


# ---- phone map: the area's own map drawn as web-map tiles (offline, every zoom styled for that zoom) ----
# One QGIS run per zoom: the print styling (TOPO_PY / PHOTO_PY) depends on the scale it is drawn at.
IMAGE_CODECS = ("lzw", "deflate", "jpeg", "webp")   # area image --codec; first = default (opens in MapNero/Avenza)
IMAGE_LOSSLESS = ("lzw", "deflate")
PHONE_SAT_Z = 16               # photo top zoom when the area has no aerial (2.4 m sat: z16 = 2.25 m/px shows it all)
PHONE_ZOOMS = (12, 17)          # default --zoom: 12 ~ whole area, 17 ~ 1 m/px (street level)
PHONE_DPI = 160                 # symbol/label size on tiles: 96 = desktop web map (too small on a phone)
PHONE_META = 8                  # tiles per side drawn in one go: labels never cross a 256 px tile seam inside it
# field layers drawn on the phone map whether ticked in the project or not (from this zoom)
PHONE_FIELD_LAYERS = ("Water: drinking water / tap", "Water: well / bore / tank", "Shelter / hut", "Ford", "Gate",
                      "Ford (on road)", "Power line", "Power tower", "Walking tracks", "Mtb trails", "Horse trails",
                      "Trailbike trails", "Park roads", "Waterholes", "Rockholes", "Bores", "Emergency")
PHONE_FIELD_ZOOM = 14



def phone_tile_args(a, qgz, s, q, con, iv, best, fmt, out):
    """PHONE_TILES_PY's args for one style + zoom (a function so t_childargs checks it supplies every a['k'] read)."""
    return {"qgz": str(qgz), "style": s, "z": q["z"], "tiles": q["tiles"], "meta": PHONE_META,
            "dpi": a.dpi, "scale": q["styled_as"], "contours": con, "interval": iv, "best": str(best),
            "cw": contour_widths(a), "pcon": PHOTO_CONTOURS, "overlay": "full", "fmt": fmt,
            "mstyle": map_style(),              # the print styling code reads it (as area print passes it)
            "quality": 80, "field": list(PHONE_FIELD_LAYERS), "field_z": PHONE_FIELD_ZOOM, "out": str(out)}


CHILD_LOGS_KEEP = 20


def child_fail(ctx, what, rc, errs, why=None):
    """A QGIS child failed: keep its whole stderr in STATE/logs (the scratch dir goes), raise naming the error and the log."""
    text = "".join(errs)
    lines = [l for l in text.strip().splitlines() if l.strip()]
    err = next((l for l in reversed(lines) if re.match(r"[\w.]*(Error|Exception)\b", l.strip())), lines[-1] if lines else "no message")
    d = STATE / "logs"; d.mkdir(parents=True, exist_ok=True)
    f = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{re.sub(r'[^A-Za-z0-9]+', '-', what).strip('-')}.log"
    f.write_text(f"{what}: exit code {rc}\n{text}")
    for old in sorted(d.glob("*.log"))[:-CHILD_LOGS_KEEP]:
        old.unlink(missing_ok=True)
    ctx.vlog(f"--- {what} failed (exit code {rc}); child stderr follows ---\n{text[-6000:]}--- end ---")
    raise CliError(E_FAIL, f"{what} failed (exit code {rc}{', ' + why if why else ''}): {err.strip()}", hint=f"full error log: {f}")
PHONE_MAX_TILES = 500_000       # per style; a city-sized area z12-17 = 33k. More needs --force


def phone_contours(z):
    """(contour set, interval) for a phone zoom: 10 m at 15+, 20 m at 14, none below (a solid brown mass)."""
    return ("best", 10) if z >= 15 else ("best", 20) if z == 14 else ("none", 10)


PHONE_TILES_PY = PROJ_PY + r"""
import io, math, sqlite3
import numpy as np
from PIL import Image
from qgis.PyQt.QtGui import QImage, QPainter
from qgis.PyQt.QtCore import QSize
""" + PICK_PY + TOPO_PY + r"""
_byname = {l.name(): l for l in _all}
_add = []
if a['z'] >= a['field_z']:                       # field layers on, ticked or not (points on top of lines)
    _f = [_byname[n] for n in a['field'] if n in _byname]
    _add = [l for l in _f if l.type() == QgsMapLayerType.VectorLayer and l.geometryType() == Qgis.GeometryType.Point] + \
           [l for l in _f if not (l.type() == QgsMapLayerType.VectorLayer and l.geometryType() == Qgis.GeometryType.Point)]
_gl = next((l for l in _all if os.path.basename(_src(l)) == 'grid.gpkg' and 'grid_lines' in l.source()), None)
if _gl is not None and a['z'] >= 13:             # MGRS grid: lines from z13, km labels from z14
    _g = _gl.clone(); p.addMapLayer(_g, False); _g.setLabelsEnabled(a['z'] >= 14 and _gl.labelsEnabled()); _add.append(_g)
_vis = [l for l in _vis if l.id() not in {x.id() for x in _add} and (_gl is None or l.id() != _gl.id())]
_vis = _add + _vis
for _l in _add:
    _noscale(_l) if a.get('style') == 'topo' else _l.setScaleBasedVisibility(False)
""" + PHOTO_PY + r"""
# 30 m terrain shading / slope under a 1-30 m/px tile = blocky cross-hatch (seen 2026-10-26): off, contours carry relief
_vis = [l for l in _vis if not (l.type() == QgsMapLayerType.RasterLayer and (l.name() in ('Elevation (coloured)', 'Slope (degrees)')
        or l.name() == 'Hillshade' or os.path.basename(_src(l)) in ('dem.tif', 'slope.tif')))] or _vis
for _l in _vis:
    if _l.type() == QgsMapLayerType.RasterLayer and _l.resampleFilter() is not None:
        _l.resampleFilter().setZoomedInResampler(QgsBilinearRasterResampler())
        _l.resampleFilter().setZoomedOutResampler(QgsBilinearRasterResampler())
bg = QColor(255, 255, 255) if (a.get('style') != 'topo' or _white_bg) else QColor(*PAPER_RGB)
O = 20037508.342789244; z = a['z']; ts = 2 * O / 2 ** z; res = ts / 256; M = a['meta']; Mg = 128
x0, x1, y0, y1 = a['tiles']
db = sqlite3.connect(a['out']); n = 0   # a .part file in scratch
tot = ((x1 - x0) // M + 1) * ((y1 - y0) // M + 1)
crs = QgsCoordinateReferenceSystem('EPSG:3857')
les = QgsLabelingEngineSettings(p.labelingEngineSettings()); les.setFlag(Qgis.LabelingFlag.UsePartialCandidates, False)
for my in range(y0, y1 + 1, M):
    for mx in range(x0, x1 + 1, M):
        nx, ny = min(M, x1 - mx + 1), min(M, y1 - my + 1); w, h = nx * 256, ny * 256
        L0, T0 = -O + mx * ts, O - my * ts            # metatile top-left (web mercator m)
        ms = QgsMapSettings(); ms.setLayers(_vis); ms.setDestinationCrs(crs); ms.setBackgroundColor(bg)
        ms.setOutputDpi(a['dpi']); ms.setOutputSize(QSize(w + 2 * Mg, h + 2 * Mg))
        ms.setExtent(QgsRectangle(L0 - Mg * res, T0 - (h + Mg) * res, L0 + (w + Mg) * res, T0 + Mg * res))
        ms.setTransformContext(p.transformContext()); ms.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
        ms.setLabelBoundaryGeometry(QgsGeometry.fromRect(QgsRectangle(L0, T0 - h * res, L0 + w * res, T0)))
        ms.setLabelingEngineSettings(les)
        img = QImage(w + 2 * Mg, h + 2 * Mg, QImage.Format_RGB32); img.fill(bg)
        pt = QPainter(img); job = QgsMapRendererCustomPainterJob(ms, pt); job.start(); job.waitForFinished(); pt.end()
        b = img.constBits(); b.setsize(img.sizeInBytes())
        arr = np.frombuffer(b, np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[Mg:Mg + h, Mg:Mg + w, 2::-1]
        for j in range(ny):
            for i in range(nx):
                im = Image.fromarray(np.ascontiguousarray(arr[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256]))
                bio = io.BytesIO()
                if a['fmt'] == 'png':                   # line art: 256-colour PNG (crisp text, ~1/3 of full colour)
                    im.quantize(256, method=Image.Quantize.FASTOCTREE).save(bio, 'PNG')
                else:
                    im.save(bio, 'JPEG', quality=a['quality'])
                db.execute('INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)', (z, mx + i, 2 ** z - 1 - (my + j), bio.getvalue()))
        db.commit(); n += 1; print(f"tile {n}/{tot}", file=sys.stderr, flush=True)
db.close()
print(json.dumps({"result": 0, "no_contours": a['contours'] not in ('none', 'ticked') and pick is None}))
app.exitQgis()
"""


def tile_range(z, bbox):
    """(x0, x1, y0, y1) XYZ tile indices covering bbox (w, s, e, n degrees) at zoom z."""
    n = 2 ** z; E = 1e-9                     # a bound exactly on a tile edge: not the next tile over
    cl = lambda v: max(0, min(n - 1, int(v)))
    tx = lambda lon: cl((max(-180, min(180, lon)) + 180) / 360 * n)
    ty = lambda lat: cl((1 - math.asinh(math.tan(math.radians(max(-85.0511, min(85.0511, lat))))) / math.pi) / 2 * n)
    return tx(bbox[0]), tx(bbox[2] - E), ty(bbox[3]), ty(bbox[1] + E)


def qgis_child(ctx, code, args, on_line=None):
    """Run a QGIS script (-c code, argv[1] = json args) as a tracked child in its own process group; a stop
    (DRAIN) ends it. Returns (rc, stdout, stderr lines that weren't progress)."""
    pr = child(subprocess.Popen([sys.executable, "-c", code, json.dumps(args)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=_pdeath,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}))
    errs, outs = [], []
    def _read():
        for l in pr.stderr:
            (on_line(l.strip()) if on_line else None) if l.startswith("tile ") else errs.append(l)
    rt = threading.Thread(target=_read, daemon=True); rt.start()
    ro = threading.Thread(target=lambda: outs.append(pr.stdout.read()), daemon=True); ro.start()
    try:
        while pr.poll() is None:
            if DRAIN.is_set():
                pr.terminate()
                try:
                    pr.wait(10)
                except subprocess.TimeoutExpired:
                    pass
                break
            try:
                pr.wait(1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if pr.poll() is None:
            try:
                os.killpg(pr.pid, signal.SIGKILL)
            except OSError:
                pr.kill()
        pr.wait(); unchild(pr)
    rt.join(5); ro.join(5)
    return pr.returncode, "".join(outs), errs


def cmd_area_phone_map(ctx):
    """The area's own map (print styling, drawn per zoom) as phone tiles: <area>/phone-<style>.mbtiles and
    .sqlitedb (OsmAnd). Fully offline: only the area's files are read."""
    need_root(ctx)
    d = sheet_dir(ctx, ctx.a.name); meta_p = d / "area.json"; qgz = d / f"{ctx.a.name}.qgz"
    if not readable(meta_p) or not readable(qgz):
        raise CliError(E_NOTFOUND, f"area {ctx.a.name} not built", f"lmap area create {ctx.a.name} ...")
    try:
        z0, z1 = (int(v) for v in (f"{PHONE_ZOOMS[0]}-{PHONE_ZOOMS[1]}" if ctx.a.zoom is None else ctx.a.zoom).split("-"))
        assert 8 <= z0 <= z1 <= 19
    except (ValueError, AssertionError):
        raise CliError(E_USAGE, f"bad --zoom {ctx.a.zoom!r}: MIN-MAX between 8 and 19, e.g. --zoom 12-17")
    if not 72 <= ctx.a.dpi <= 400:
        raise CliError(E_USAGE, "--dpi must be 72-400")
    meta = json.loads(meta_p.read_text()); bbox = meta["bbox"]
    if ctx.a.bbox:
        try:
            b = [float(v) for v in ctx.a.bbox.split(",")]; assert len(b) == 4 and b[0] < b[2] and b[1] < b[3]
        except (ValueError, AssertionError):
            raise CliError(E_USAGE, f"bad --bbox {ctx.a.bbox!r}: w,s,e,n degrees, e.g. --bbox=146.75,-19.34,146.79,-19.3")
        bbox = [max(b[0], bbox[0]), max(b[1], bbox[1]), min(b[2], bbox[2]), min(b[3], bbox[3])]
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise CliError(E_USAGE, f"--bbox is outside area {ctx.a.name} {meta['bbox']}")
    styles = ["topo", "photo"] if ctx.a.style == "both" else [ctx.a.style]
    lat = math.radians((bbox[1] + bbox[3]) / 2)
    zooms = []
    for z in range(z0, z1 + 1):
        x0, x1, y0, y1 = tile_range(z, bbox)
        res_m = 2 * 20037508.342789244 / 2 ** z / 256 * math.cos(lat)          # ground metres per pixel
        zooms.append({"z": z, "tiles": [x0, x1, y0, y1], "n": (x1 - x0 + 1) * (y1 - y0 + 1),
                      "m_per_px": round(res_m, 2), "styled_as": round(res_m * ctx.a.dpi / 0.0254, -2)})
    # photo stops at the zoom its picture can fill: with no aerial.tif the photo is 2.4 m satellite, which z16
    # (2.25 m/px) already shows in full; z17 would only enlarge it at 4x the tiles. An explicit --zoom is kept as asked.
    photo_z1 = z1 if readable(d / "aerial.tif") or ctx.a.zoom is not None else min(z1, PHONE_SAT_Z)
    zs = {s: [q for q in zooms if s != "photo" or q["z"] <= photo_z1] for s in styles}
    ntiles = max(sum(q["n"] for q in zs[s]) for s in styles)      # the biggest style's count (the per-style limit)
    lab = f"-{ctx.a.label}" if ctx.a.label else ""
    if lab and not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", ctx.a.label):
        raise CliError(E_USAGE, f"bad --label {ctx.a.label!r}")
    outs = {s: d / f"phone-{s}{lab}.mbtiles" for s in styles}
    plan = {"action": "area.phone-map", "styles": styles, "zoom": [z0, z1], "dpi": ctx.a.dpi, "tiles_per_style": ntiles,
            "files": [str(f) for s in styles for f in (outs[s], outs[s].with_suffix(".sqlitedb"))],
            "zooms": zooms, "max_zoom": {s: zs[s][-1]["z"] for s in styles}}
    eta = WorkEta([(f"phone-{s}-{ctx.a.dpi}dpi", sum(q["n"] for q in zs[s]), f"phone-map {s}", len(zs[s])) for s in styles])   # one QGIS run per zoom
    plan["eta_s"] = eta.left()                    # pre-start: past runs' start-up + s/tile; None = never measured
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if ntiles > PHONE_MAX_TILES and not ctx.a.force:
        raise CliError(E_USAGE, f"{ntiles} tiles per style is over {PHONE_MAX_TILES} (each zoom +1 = 4x tiles, ~3 ms each)",
                       "lower --zoom max, a smaller --bbox, or --force")
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to write phone maps without --yes",
                       f"preview: lmap area phone-map {ctx.a.name} --dry-run")
    import sqlite3
    install_signals()
    made, took, warn = [], {}, []
    with Lock(STATE / "locks" / f"area-phone-{ctx.a.name}.lock"):
        sc = ctx.scratch() / f"phone-{ctx.a.name}-{os.getpid()}"; sc.mkdir(parents=True, exist_ok=True)
        try:
            for si, s in enumerate(styles):
                eta.phase(si, auto=False)              # each zoom's child is started below
                mb = sc / (outs[s].name + ".part")
                db = sqlite3.connect(mb)   # .part until installed
                db.executescript("CREATE TABLE metadata (name text, value text); CREATE TABLE tiles (zoom_level integer, "
                                 "tile_column integer, tile_row integer, tile_data blob); CREATE UNIQUE INDEX tile_index "
                                 "ON tiles (zoom_level, tile_column, tile_row);")
                fmt = "png" if s == "topo" else "jpg"
                db.executemany("INSERT INTO metadata VALUES (?,?)", [
                    ("name", f"{ctx.a.name} {s}"), ("type", "baselayer"), ("version", "1"), ("format", fmt),
                    ("description", f"lmap {ctx.a.name} {s} map (print styling per zoom)"),
                    ("bounds", ",".join(f"{v:.6f}" for v in bbox)), ("minzoom", str(z0)), ("maxzoom", str(zs[s][-1]["z"]))])
                db.commit(); db.close()
                for q in zs[s]:
                    eta.child(q["n"])                    # timed from here: contour prep is part of each zoom's cost
                    con, iv = phone_contours(q["z"])
                    try:
                        con, iv, best, _ = area_contours(d, con, iv)
                    except CliError as e:            # drawn without contours: say so, a crew relies on them
                        warn.append(f"{s} z{q['z']}: NO contours ({e.msg})"); con, best = "none", d / "contours-best.gpkg"
                    t0 = eta.tc or time.time()
                    ctx.log(f"{s} z{q['z']}: {q['n']} tiles ({q['m_per_px']} m/px, styled as 1:{q['styled_as']:.0f})")
                    args = phone_tile_args(ctx.a, qgz, s, q, con, iv, best, fmt, mb)
                    def _tl(l, q=q):
                        ctx.vlog(l); f = tile_line_frac(l)
                        if f is not None:
                            eta.progress(f, f" z{q['z']} tiles {round(q['n'] * f)}/{q['n']}")
                    rc, so, errs = qgis_child(ctx, PHONE_TILES_PY, args, _tl)
                    if DRAIN.is_set():
                        raise CliError(E_ABORTED, "stopped; nothing written")
                    line = next((l for l in so.splitlines() if l.startswith("{")), None)
                    if rc or not line:
                        child_fail(ctx, f"phone map {s} z{q['z']}", rc, errs, None if rc else "no result line")
                    if json.loads(line).get("no_contours"):
                        warn.append(f"{s} z{q['z']}: NO contours (the project has no {con} contour layer)")
                    took[f"{s}-z{q['z']}"] = round(time.time() - t0, 1)
                    eta.child_end()
                mbtiles_to_sqlitedb(mb, sc / outs[s].with_suffix(".sqlitedb").name)
            # all styles built first, then installed without a stop check (a stop mid-install would mix new and old files)
            try:
                for s in styles:
                    for src, dst in ((sc / (outs[s].name + ".part"), d / outs[s].name),
                                     (sc / outs[s].with_suffix(".sqlitedb").name, d / outs[s].with_suffix(".sqlitedb").name)):
                        install_file(src, dst); made.append(dst); src.unlink(missing_ok=True)
            except Exception as e:               # e.g. a file held open on a network share: say which files are new
                raise CliError(E_FAIL, f"install failed part way ({e}); NEW: {[f.name for f in made]}, the rest are "
                               "the previous build", "close the map in QGIS/phone sync and rerun --yes")
            eta.finish()                         # after install: a failed install records nothing
        finally:
            shutil.rmtree(sc, ignore_errors=True)
    qg = None
    try:                                         # the project lists the phone maps as fast base layers: refresh it
        with Lock(STATE / "locks" / f"area-{ctx.a.name}.lock", wait=ctx):     # not while area create rebuilds it
            qg = area_qgis(ctx, d, ctx.a.name, meta["crs"], meta["bbox"], meta["level"])
    except (CliError, OSError, KeyError, ValueError) as e:
        warn.append(f"QGIS project not refreshed ({e}): lmap area create {ctx.a.name} ... adds the phone maps")
    for w_ in warn:
        ctx.log(f"WARNING: {w_}")
    return {"ok": True, **plan, "seconds": took, "warnings": warn, "qgis": qg,
            "bytes": {f.name: f.stat().st_size for f in made}}


def mbtiles_to_sqlitedb(src, out):
    """MBTiles -> OsmAnd/Locus .sqlitedb (BigPlanet layout: z = 17 - zoom, y not flipped)."""
    import sqlite3
    part = Path(str(out) + ".part"); part.unlink(missing_ok=True)
    a = ro_sqlite(src); b = sqlite3.connect(part)
    try:
        b.executescript("CREATE TABLE tiles (x int, y int, z int, s int, image blob, PRIMARY KEY (x,y,z,s));"
                        "CREATE TABLE info (minzoom, maxzoom, tilenumbering, url, ellipsoid, inverted_y, "
                        "timecolumn, expireminutes, rule, referer, randoms);")
        for z, x, y, img in a.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
            b.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,0,?)", (x, (1 << z) - 1 - y, 17 - z, img))
        lo, hi = a.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
        if lo is not None:
            b.execute("INSERT INTO info (minzoom, maxzoom, tilenumbering, ellipsoid, inverted_y) VALUES (?,?,?,0,0)",
                      (17 - hi, 17 - lo, "BigPlanet"))
        b.commit()
    except BaseException:
        a.close(); b.close(); part.unlink(missing_ok=True)   # no half-written .part left behind
        raise
    a.close(); b.close()
    part.rename(out)
    return out


AREA_EXPORT_README = """{name} - phone / ATAK bundle (lmap {ver}, {made})

ATAK (ATAK-CIV / WinTAK)
  atak/{name}-package.zip   TAK data package: copy to the phone, then ATAK > Import > Local SD > pick the zip.
                            Brings in the map tiles, points (water, shelter, emergency...), tide gauges and
                            landing-site candidates as overlays.
  atak/DTED/                Elevation for ATAK (line of sight, heights): copy the DTED folder to /sdcard/atak/DTED/.
  MGRS: use ATAK's own grid (Overlay Manager > Grid Lines > MGRS).

OsmAnd / Locus           tiles/*.sqlitedb -> copy to Android/media/net.osmand.plus/files/tiles/ (OsmAnd) or
                         Locus/mapsOnline/custom/ ; points: poi/{name}.gpx (import via the app's "Tracks/Import").
AlpineQuest / Avenza     tiles/*.mbtiles ; points: poi/{name}.kmz . Avenza also takes print/*.pdf (GeoPDF).
Google Earth             poi/{name}.kmz

Tiles: {tiles}
Points: {npoi} ({kinds})
Coordinates in the GPX/KMZ are WGS84 (what GPS/ATAK use).  Things change: check on the ground.
"""


def cmd_area_export(ctx):
    """Phone / ATAK bundle for one area: MBTiles (+ .sqlitedb for OsmAnd), DTED, POI KMZ + GPX, ATAK data package.
    Plain files by default; --encrypt packs the bundle into an AES-256 zip."""
    import uuid, zipfile
    need_root(ctx); need_tool("gdal_translate", "gdalwarp", "gdaladdo", "ogr2ogr")
    name = ctx.a.name; d = lib_dir(ctx, "areas", name)
    meta_p = d / "area.json"
    if not readable(meta_p):
        raise CliError(E_NOTFOUND, f"area {name} not built", f"lmap area create {name} ...")
    meta = json.loads(meta_p.read_text()); bbox = meta["bbox"]
    out = Path(ctx.a.target).resolve() / f"lmap-{name}" if ctx.a.target else d / "export"
    if ctx.a.target and not Path(ctx.a.target).is_dir():
        raise CliError(E_NOTFOUND, f"target {ctx.a.target} is not a directory", "mount the USB first")
    layers = [(k, d / f"{k}.tif") for k in ("sat", "topo", "aerial") if readable(d / f"{k}.tif")]
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "area.export", "to": str(out), "tiles": [k for k, _ in layers],
                "max_zoom": ctx.a.max_zoom, "encrypt": ctx.a.encrypt, "bbox": bbox}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write {out} without --yes", f"preview: lmap area export {name} --dry-run")
    pw = read_passphrase(ctx) if ctx.a.encrypt else None
    install_signals()
    stage = (ctx.scratch() / f"area-export-{name}") if ctx.a.encrypt else out
    stage.mkdir(parents=True, exist_ok=True)
    res = {"tiles": [], "dted": 0}
    with Lock(STATE / "locks" / f"area-export-{name}.lock"):
        # 1. tiles: MBTiles (ATAK, Avenza, Locus, AlpineQuest) + .sqlitedb copy (OsmAnd)
        (stage / "tiles").mkdir(exist_ok=True); done = set()
        for k, src in layers:
            say_stop(ctx)
            if DRAIN.is_set():
                break
            mb = stage / "tiles" / f"{name}-{k}.mbtiles"; done.add(mb.name)
            sq = mb.with_suffix(".sqlitedb")
            if ctx.a.refresh or not mbtiles_current(mb, mbtiles_key(src, bbox, 80, ctx.a.max_zoom)):
                ctx.log(f"tiles: {k}")                 # (a newer source / other bbox / zoom: never the old tiles)
                sq.unlink(missing_ok=True)             # its copy goes with it (an interrupted run can't keep it)
                to_mbtiles(ctx, src, mb, bbox, quality=80, max_zoom=ctx.a.max_zoom)
            if not sq.is_file() or sq.stat().st_mtime_ns < mb.stat().st_mtime_ns:
                mbtiles_to_sqlitedb(mb, sq)
            res["tiles"].append(mb.name)
        if not DRAIN.is_set():
            drop_other_tiles(stage / "tiles", done, {f"{name}-{k}.mbtiles" for k in ("sat", "topo", "aerial")})
        # 2. points: field features + QLD emergency/community + water bores + tides + landing sites -> KMZ + GPX
        poi = stage / "poi"; poi.mkdir(exist_ok=True)
        gj = ctx.scratch() / f"area-export-{name}.geojson"
        feats = []
        def take(path, layer, kindsql, namesql="name", where="1=1"):
            if not readable(path):
                return
            tmp = ctx.scratch() / f"area-export-{name}-part.geojson"; tmp.unlink(missing_ok=True)
            try:
                run(["ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-dialect", "SQLITE", "-sql",
                     f"SELECT {kindsql} AS kind, {namesql} AS name, geom FROM {layer} WHERE {where}", tmp, path], ctx)
                feats.extend(json.loads(tmp.read_text()).get("features", []))
            except (CliError, ValueError):
                pass
            finally:
                tmp.unlink(missing_ok=True)
        take(d / "field.gpkg", "field_points", "kind")
        take(d / "qld-infra.gpkg", "emergency", "feature_type")
        take(d / "qld-infra.gpkg", "community", "feature_type")
        take(d / "qld-water.gpkg", "bores", "'bore'", "coalesce(name, 'bore')")
        take(d / "hls.gpkg", "hls", "'landing site candidate'", "'HLS ' || size_m || ' m'")
        tides = ctx.path(ctx.sources["qld-tides"]["dest"]) / "stations.gpkg"
        take(tides, "tide_stations", "'tide gauge'", "station",
             f"ST_X(geom) BETWEEN {bbox[0]} AND {bbox[2]} AND ST_Y(geom) BETWEEN {bbox[1]} AND {bbox[3]}")
        feats = [f for f in feats if f.get("geometry") and f["geometry"]["type"] == "Point"]
        for f in feats:
            p_ = f["properties"]; p_["name"] = p_.get("name") or p_.get("kind")
            p_["description"] = p_.get("kind"); p_["desc"] = p_.get("kind")   # desc = GPX's description field
        gj.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
        kinds = sorted({f["properties"]["kind"] for f in feats if f["properties"].get("kind")})
        kml = poi / f"{name}.kml"; gpx = poi / f"{name}.gpx"
        try:
            for x in (kml, gpx):
                x.unlink(missing_ok=True)
            if feats:
                run(["ogr2ogr", "-f", "KML", "-dsco", "NameField=name", "-dsco", "DescriptionField=description",
                     "-nln", f"{name} points", kml, gj], ctx)
                run(["ogr2ogr", "-f", "GPX", "-dsco", "GPX_USE_EXTENSIONS=NO", "-nln", "waypoints",
                     "-select", "name,desc", gpx, gj], ctx)       # (DESC is an SQL keyword: no -sql here)
                with zipfile.ZipFile(poi / f"{name}.kmz", "w", zipfile.ZIP_DEFLATED) as z:
                    z.write(kml, "doc.kml")
                kml.unlink()
        finally:
            gj.unlink(missing_ok=True)
        res["points"] = len(feats)
        # 3. DTED for ATAK (reuses the export code: 1 degree cells over the box)
        atak = stage / "atak"; atak.mkdir(exist_ok=True)
        try:
            r = export_atak_dted(ctx, atak, bbox)
            res["dted"] = r
        except CliError as e:
            res["dted"] = {"error": e.msg}
        # 4. TAK data package: tiles + points (one zip ATAK imports in one go)
        pkg = atak / f"{name}-package.zip"
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lmap-area:{name}"))   # stable: re-import replaces
        entries = [f"tiles/{t}" for t in res["tiles"]] + ([f"poi/{name}.kmz"] if (poi / f"{name}.kmz").is_file() else [])
        man = ('<MissionPackageManifest version="2"><Configuration>'
               f'<Parameter name="uid" value="{uid}"/><Parameter name="name" value="lmap {name}"/>'
               '<Parameter name="onReceiveImport" value="true"/><Parameter name="onReceiveDelete" value="false"/>'
               '</Configuration><Contents>' + "".join(f'<Content ignore="false" zipEntry="{e}"/>' for e in entries)
               + '</Contents></MissionPackageManifest>')
        part = Path(str(pkg) + ".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:   # tiles are JPEG already: store
            z.writestr("MANIFEST/manifest.xml", man)
            for e in entries:
                z.write(stage / e, e)
        part.rename(pkg)
        res["atak_package"] = pkg.name
        # 5. print sheets already made for this area travel along (Avenza takes GeoPDF)
        prints = sorted(d.glob("print-*.pdf"))
        if prints:
            (stage / "print").mkdir(exist_ok=True)
            for f in prints:
                install_file(f, stage / "print" / f.name, link=False)   # a copy: edits mustn't reach the library
        (stage / "README.txt").write_text(AREA_EXPORT_README.format(
            name=name, ver=VERSION, made=time.strftime("%F"), tiles=", ".join(res["tiles"]) or "none",
            npoi=len(feats), kinds=", ".join(kinds) or "-"))
        if ctx.a.encrypt:
            out.mkdir(parents=True, exist_ok=True)
            z = encrypt_zip(ctx, stage, out / f"{name}.zip", pw)
            (out / "README-ENCRYPTED.txt").write_text(ENC_README)
            shutil.rmtree(stage, ignore_errors=True)
            res["encrypted"] = z.name
        n, _ = write_sums(ctx, out)
    return {"ok": True, "action": "area.export", "to": str(out), **res, "files": n,
            "mb": round(du_tree(out) / 1e6, 1), "verify": f"lmap export verify {out}"}


def cmd_area_list(ctx):
    need_root(ctx)
    out = []
    for j in lib_jsons(ctx, "areas"):
        try:
            m = json.loads(j.read_text())
        except ValueError:
            continue
        out.append({"name": m["name"], "level": m["level"], "km2": m["km2"], "made": m.get("made"),
                    "gb": round(du_tree(j.parent) / 1e9, 2), "dir": str(j.parent)})
    return out
