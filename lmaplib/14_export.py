# lmap: export. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- export
EXPORT_SKIP = (".part", ".lock", ".tmp", ".lmap.lock", ".unverified")
ATAK_POI = {   # OSM amenity -> label, exported as a KML overlay for ATAK
    "hospital": "Hospital", "clinic": "Clinic", "pharmacy": "Pharmacy", "fuel": "Fuel",
    "police": "Police", "fire_station": "Fire station", "drinking_water": "Drinking water",
    "shelter": "Shelter", "townhall": "Town hall", "doctors": "Doctor"}


def parse_bbox(v):
    if not v:
        raise CliError(E_USAGE, "need --bbox w,s,e,n (degrees)", "e.g. --bbox=146.6,-19.4,147.0,-19.1")
    try:
        w, so, e, n = (float(x) for x in v.split(","))
    except ValueError:
        raise CliError(E_USAGE, f"--bbox wants w,s,e,n in degrees, got {v!r}")
    if not (w < e and so < n and -180 <= w <= 180 and -90 <= so <= 90):
        raise CliError(E_USAGE, f"--bbox out of order/range: {v}")
    return w, so, e, n


def fs_type(p):
    r = subprocess.run(["stat", "-f", "-c", "%T", str(p)], capture_output=True, text=True)
    return r.stdout.strip()


def sha256(p, bs=1 << 22):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(bs):
            if ABORT.is_set():
                raise Aborted()
            h.update(b)
    return h.hexdigest()


def write_sums(ctx, top, written=(), since=None):
    """SHA256SUMS over everything under top. A cached hash (.lmap-sums.json, v2) is reused only for a file lmap did
    not write in this run and whose size + mtime_ns are unchanged. lmap knows what it wrote: `written` (paths
    relative to top: rsync's transferred files, which get their OLD mtime back) and anything modified since `since`
    are always hashed again. ctime can't decide it: on FAT/exFAT sticks it is the mtime again after a remount."""
    cache_p = top / ".lmap-sums.json"
    try:
        cache = json.loads(cache_p.read_text())
        cache = cache.get("files", {}) if cache.get("v") == 2 else {}     # older format: hash everything once
    except (OSError, ValueError, AttributeError):
        cache = {}
    written = set(written); cut = None if since is None else int((since - 2) * 1e9)
    files = sorted(f for f in top.rglob("*") if f.is_file() and f.name not in ("SHA256SUMS", ".lmap-sums.json")
                   and not f.name.endswith((".unverified", ".unverified.part")))
    new, lines, hashed = {}, [], 0
    for f in files:
        rel = f.relative_to(top).as_posix(); st = f.stat()
        c = cache.get(rel)
        if (c and c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns and rel not in written
                and (cut is None or st.st_mtime_ns < cut)):
            h = c["sha256"]
        else:
            h = sha256(f); hashed += 1
            if hashed % 50 == 0:
                ctx.log(f"  hashed {hashed} files...")
        new[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": h}
        lines.append(f"{h}  {rel}")
    (top / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    cache_p.write_text(json.dumps({"v": 2, "files": new}))
    return len(files), hashed


def export_copy(ctx, out, plan):
    """Mirror the data root to out/maps (rsync, FAT-safe), plus the lmap tool itself."""
    need_tool("rsync")
    dst = out / "maps"
    total = du_tree(ctx.root)
    if plan:
        r = {"would_copy": str(ctx.root), "to": str(dst), "approx_gb": round(total / 1e9, 2)}
        if getattr(ctx.a, "jpeg", False):
            r["would_jpeg"] = [str(f.relative_to(ctx.root)) for f in webp_rasters(ctx.root)]
        return r
    dst.mkdir(parents=True, exist_ok=True)
    fat = fs_type(out) in ("msdos", "vfat", "fat")
    if fat:
        big = [str(f) for f in ctx.root.rglob("*") if f.is_file() and f.stat().st_size >= 4 * 1024**3]
        if big:
            raise CliError(E_FAIL, f"target is FAT32 and {len(big)} file(s) are >= 4GB: {big[:3]}",
                           "format the USB as exFAT or ext4")
    ex = []
    for pat in ("*.part", "*.part.*", "*.unverified", "*.unverified.*", "*.lock", ".lmap.lock", "*.ovr.tmp", ".wmscache/"):
        ex += ["--exclude", pat]
    webp = webp_rasters(ctx.root) if getattr(ctx.a, "jpeg", False) else []
    for f in webp:                       # re-encoded below; excluded files are also safe from --delete-after
        ex += ["--exclude", "/" + str(f.relative_to(ctx.root))]
    flags = ["-rt", "--modify-window=2", "--partial", "--delete-after"] if fat else ["-rt", "--partial", "--delete-after"]
    ctx.log(f"copying {round(total / 1e9, 1)} GB to {dst} ...")
    got = run(["rsync", *flags, "--out-format=%n", *ex, f"{ctx.root}/", f"{dst}/"], ctx) or ""
    ctx.export_written = getattr(ctx, "export_written", set()) | {"maps/" + l.strip() for l in got.splitlines() if l.strip()}
    converted = [jpeg_copy(ctx, f, dst / f.relative_to(ctx.root)) for f in webp]
    tool = out / "lmap-tool"
    if tool.is_symlink():
        raise CliError(E_USAGE, f"{tool} is a symlink: refusing to write the tool through it", "remove the link")
    tool.mkdir(exist_ok=True)
    hr, tr = Path(HERE).resolve(), tool.resolve()
    if hr == tr or hr in tr.parents or tr in hr.parents:
        raise CliError(E_USAGE, f"export target {tool} is (inside) the running lmap checkout {hr}",
                       "export to another directory (e.g. the mounted USB)")
    tf = tool_filter(HERE)
    lst = next((a.split("=", 1)[1] for a in tf if a.startswith("--files-from=")), None)
    if lst:                              # a drive exported before: drop what's no longer (or never was) the tool
        keep = {x for x in Path(lst).read_bytes().decode(errors="replace").split("\0") if x}
        for f in sorted(tool.rglob("*"), reverse=True):
            r = f.relative_to(tool).as_posix()
            if f.is_symlink() or f.is_file():
                if r not in keep:
                    f.unlink(missing_ok=True)
            elif f.is_dir() and not any(f.iterdir()):
                f.rmdir()
    try:
        got = run(["rsync", "-rt", "--out-format=%n"] + tf + [f"{HERE}/", f"{tool}/"], ctx) or ""
    finally:
        for a in tf:
            if a.startswith("--files-from="):
                Path(a.split("=", 1)[1]).unlink(missing_ok=True)
    ctx.export_written |= {"lmap-tool/" + l.strip() for l in got.splitlines() if l.strip()}
    res = {"copied_to": str(dst), "gb": round(du_tree(dst) / 1e9, 2), "fat": fat}
    if getattr(ctx.a, "jpeg", False):
        res["jpeg"] = converted
    return res


def webp_rasters(root):
    """WEBP-compressed .tif files under root (what --jpeg re-encodes for readers without WEBP)."""
    out = []
    for f in sorted(Path(root).rglob("*.tif")):
        if ".wmscache" in f.parts or ".part" in f.name:
            continue
        r = subprocess.run(["gdalinfo", str(f)], capture_output=True, text=True)
        if "COMPRESSION=WEBP" in r.stdout:
            out.append(f)
    return out


def jpeg_copy(ctx, src, out):
    """src (WEBP COG) -> out as a JPEG q85 COG, same name so projects still find it. Skips if out is newer."""
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return {"file": str(out), "unchanged": True}
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    args, _ = cog_co(types.SimpleNamespace(webp=False), src)
    ctx.log(f"JPEG: {src.name} ({round(src.stat().st_size / 1e9, 1)} GB) ...")
    try:
        run(["gdal_translate", "-q", "-of", "COG", *args, str(src), str(part)], ctx)
        if not gdal_ok(part):
            raise CliError(E_FAIL, f"{out.name}: JPEG copy failed gdalinfo")
        replace_retry(part, out)
    except BaseException:
        part.unlink(missing_ok=True); raise
    return {"file": str(out), "gb": round(out.stat().st_size / 1e9, 2)}


def dem_sources(ctx):
    """Copernicus elevation files on hand, finest first: [(res, file, src)]."""
    out = []
    for i, v in ctx.sources.items():
        if v["kind"] == "copernicus" and (r := best_raster(ctx, i)):
            out.append((v.get("res", 90), r, dict(v, id=i)))
    return sorted(out, key=lambda x: x[0])


def tool_filter(here):
    """rsync args so the USB gets the tool itself, not the checkout's private bits: in a git checkout only the
    tracked files (a dev worktree, .devenv, PLAN.md, local notes stay home); else the .gitignore rules."""
    here = Path(here)
    try:
        r = subprocess.run(["git", "-C", str(here), "ls-files", "-z"], capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            have = [x for x in r.stdout.split(b"\0") if x and (here / x.decode(errors="replace")).is_file()]
            fd, nm = tempfile.mkstemp(prefix="lmap-tool-", suffix=".lst")
            with os.fdopen(fd, "wb") as fh:         # tracked + present (a deleted-not-committed file would fail rsync)
                fh.write(b"\0".join(have) + b"\0")
            lst = Path(nm)
            return ["--from0", f"--files-from={lst}"]
    except (OSError, subprocess.SubprocessError):
        pass
    return ["--exclude", ".git", "--exclude", "__pycache__", "--exclude", "/dev/", "--exclude", ".devenv",
            "--filter", ":- .gitignore"]


def mbtiles_key(src, bbox, quality=80, max_zoom=None):
    """What an MBTiles was built from: source file (name, size, mtime), bbox, quality, zoom cap."""
    st = Path(src).stat()
    return json.dumps([Path(src).name, st.st_size, st.st_mtime_ns, [round(float(x), 6) for x in bbox], quality,
                       max_zoom])


def mbtiles_bounds(f):
    """[w, s, e, n] from an MBTiles' metadata, None if unreadable."""
    try:
        db = ro_sqlite(f)
        try:
            r = db.execute("SELECT value FROM metadata WHERE name = 'bounds'").fetchone()
        finally:
            db.close()
        b = [float(v) for v in r[0].split(",")]
        return b if len(b) == 4 else None
    except Exception:
        return None


def drop_other_tiles(d, keep, ours):
    """Remove lmap's own tile files in d that this export didn't build or keep (a layer the new area doesn't
    cover): another area's tiles under a layer's name would show the old place as current. Only names lmap makes
    here (ours: *.mbtiles names) and their .sqlitedb copies are touched; anything else in d is the user's."""
    gone = []
    ours = set(ours) | {f.name for f in Path(d).glob("area-*.mbtiles")                 # removed/renamed areas too
                        if f.stem.rsplit("-", 1)[-1] in AREA_IMG
                        or PHONE_MAP_RE.fullmatch(f.stem.rsplit("~", 1)[-1])}
    for n in sorted(ours - set(keep)):
        for f in (Path(d) / n, (Path(d) / n).with_suffix(".sqlitedb")):
            if f.is_file():
                f.unlink(missing_ok=True); gone.append(f.name)
    return gone


def imagery_names(ctx):
    """Every <id>.mbtiles name an export can make (any finished-file source but elevation)."""
    return {f"{i}.mbtiles" for i, v in ctx.sources.items() if v.get("out") and v.get("kind") != "copernicus"} | {
        f"{area_tile_name(aj, k, dup)}.mbtiles" for js, dup in [area_jsons(ctx)] for aj in js
        for k in AREA_IMG} | {phone_map_name(aj, k, dup) for js, dup in [area_jsons(ctx)] for aj in js for k in phone_map_keys(aj.parent)}


PHONE_MAP_KEYS = ("phone-topo", "phone-photo")     # area phone-map files carried by export as area-<name>~<key>
PHONE_MAP_RE = re.compile(r"phone-(topo|photo)(-[^~/]+)?")   # + --label maps: phone-topo-<label>


def phone_map_keys(d):
    """Every phone map in an area dir: phone-<style> and phone-<style>-<label> (.mbtiles stems)."""
    try:
        return sorted(f.stem for f in Path(d).glob("phone-*.mbtiles") if PHONE_MAP_RE.fullmatch(f.stem))
    except OSError:
        return []


def phone_map_name(aj, k, dup):
    """area-<dir>~<key>.mbtiles, area-<state>~<dir>~<key> when the dir is in 2 states: '~' (no name rule allows it)
    so no other area's file name (x-phone, x.phone, qld-x) can equal it."""
    n = aj.parent.name
    return f"area-{aj.parent.parent.name}~{n}~{k}.mbtiles" if n in dup else f"area-{n}~{k}.mbtiles"


def mbtiles_current(out, key):
    """out exists, opens, and was built from exactly this source + bbox (a re-export of ANOTHER area must not keep
    the old area's tiles under the same name)."""
    import sqlite3
    if not Path(out).is_file():
        return False
    try:
        c = ro_sqlite(out)
        try:
            r = c.execute("SELECT value FROM metadata WHERE name = 'lmap_key'").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return False
    return bool(r) and r[0] == key


def to_mbtiles(ctx, src, out, bbox, quality=80, max_zoom=None):
    """Warp any raster to Web Mercator MBTiles (JPEG) clipped to bbox, with overview zooms.
    max_zoom caps detail (zoom z = 156543 m/px / 2^z at the equator; 17 ~ 1.2 m, 16 ~ 2.4 m)."""
    part = Path(str(out).replace(".mbtiles", ".part.mbtiles"))
    part.unlink(missing_ok=True)
    w, so, e, n = bbox
    try:
        # gdalwarp can't pick an MBTiles zoom itself: warp to a VRT, then translate (snaps to a zoom level)
        vrt = Path(str(part) + ".vrt")
        tr = []
        if max_zoom:
            info = json.loads(run(["gdalinfo", "-json", src], ctx)); px = abs(info["geoTransform"][1])
            geo = "GEOGCRS" in info["coordinateSystem"]["wkt"][:10]
            px_m = px * 111320 if geo else px
            r = 156543.03392804097 / 2 ** max_zoom
            # only coarsen, never invent detail. 1% over: UPPER would round an exact z17 up to z18
            tr = ["-tr", r * 1.01, r * 1.01] if px_m < r else []
        run(["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-te_srs", "EPSG:4326", "-te", w, so, e, n,
             "-r", "bilinear", "-dstalpha", "-of", "VRT"] + tr + [src, vrt], ctx)
        try:
            run(["gdal_translate", "-q", "-of", "MBTiles", "-co", "TILE_FORMAT=JPEG", "-co", f"QUALITY={quality}",
                 "-co", "ZOOM_LEVEL_STRATEGY=UPPER", "-co", "RESAMPLING=BILINEAR", "--config", "GDAL_CACHEMAX", "512",
                 vrt, part], ctx)
        finally:
            vrt.unlink(missing_ok=True)
        run(["gdaladdo", "-q", "-r", "average", part, "2", "4", "8", "16", "32", "64", "128", "256"], ctx)
        if not gdal_ok(part):
            raise CliError(E_FAIL, f"{out.name}: MBTiles failed gdalinfo")
        import sqlite3
        c = sqlite3.connect(part)                      # what it was built from: lets a re-run keep it
        try:
            with c:
                c.execute("DELETE FROM metadata WHERE name = 'lmap_key'")
                c.execute("INSERT INTO metadata (name, value) VALUES ('lmap_key', ?)",
                          (mbtiles_key(src, bbox, quality, max_zoom),))
        finally:
            c.close()
        os.replace(part, out)
    except BaseException:
        for x in part.parent.glob(part.name + "*"):
            x.unlink(missing_ok=True)
        part.unlink(missing_ok=True); raise
    return out


def bbox_mga55(bbox):
    """lon/lat bbox -> MGA zone 55 (EPSG:7855) envelope, via gdaltransform (no pyproj needed)."""
    w, so, e, n = bbox
    pts = [(w, so), (w, n), (e, so), (e, n), ((w + e) / 2, so), ((w + e) / 2, n)]
    r = subprocess.run(["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", "EPSG:7855", "-output_xy"],
                       input="\n".join(f"{x} {y}" for x, y in pts), capture_output=True, text=True)
    xy = [tuple(map(float, l.split()[:2])) for l in r.stdout.splitlines() if l.strip()]
    if len(xy) != len(pts):
        raise CliError(E_FAIL, f"gdaltransform failed: {r.stderr.strip()[-200:]}")
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    return min(xs), min(ys), max(xs), max(ys)


AREA_IMG = ("aerial", "sat", "topo")             # an area's own imagery clips (live services: NSW, tile servers)


def area_layers(ctx, bbox):
    """[(name, file, from_source)] of built areas' imagery clips (aerial/sat/topo.tif) overlapping bbox. Live
    sources (NSW imagery/topo, tile servers) have no national file, only these clips: without them a NSW
    phone pack held just the 30 m Landsat. Same clip in two areas (same box + size): carried once."""
    out, seen = [], set()
    js, dup = area_jsons(ctx)
    for aj in js:
        a = area_meta(aj)
        if not a:
            continue
        frm = a["from"]
        for k in AREA_IMG:
            f = aj.parent / f"{k}.tif"
            if k not in frm or not f.is_file() or not (b := raster_ll_box(f)) or not overlaps(b, list(bbox)):
                continue
            if frm[k] in ctx.sources and best_raster(ctx, frm[k]):
                continue                           # its source's own file is in the pack already: no duplicate
            sig = (k, tuple(round(x, 4) for x in b), f.stat().st_size)
            if sig not in seen:
                seen.add(sig); out.append((area_tile_name(aj, k, dup), f, frm[k]))
    return out


def area_meta(aj):
    """{"from": {key: source id}} of an area.json: what each file was really built from (built[k].from), else
    the plan (items[].from); keys whose build failed dropped. None for an unreadable / malformed file."""
    try:
        a = json.loads(aj.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(a, dict):
        return None
    frm = {i.get("key"): i.get("from") for i in (a.get("items") or []) if isinstance(i, dict)}
    bl = a.get("built") if isinstance(a.get("built"), dict) else {}
    for k, v in bl.items():
        if isinstance(v, dict):
            if v.get("error"):
                frm.pop(k, None)
            elif v.get("from") or v.get("reused_from"):
                frm[k] = v.get("from") or v.get("reused_from")
    return {"from": {k: v for k, v in frm.items() if isinstance(k, str) and isinstance(v, str)}}


def area_tile_name(aj, k, dup):
    """area-<dir>-<key>; the state too when that dir name is in more than one state (no overwrite)."""
    n = aj.parent.name
    return f"area-{aj.parent.parent.name}-{n}-{k}" if n in dup else f"area-{n}-{k}"


def area_jsons(ctx):
    js = sorted(Path(ctx.root, "areas").glob("*/*/*/area.json"))
    names = [j.parent.name for j in js]
    return js, {n for n in names if names.count(n) > 1}


def imagery_layers(ctx, bbox, scratch=None):
    """[(id, file)] of finished imagery/topo files that overlap bbox (sat, aerial, topo; not elevation), then
    the built areas' own clips of them (area_layers)."""
    out = []
    for i, v in ctx.sources.items():
        if v.get("out") and v["kind"] != "copernicus" and (f := best_raster(ctx, i)):
            b = raster_ll_box(f)
            if b and overlaps(b, list(bbox)):
                out.append((i, f))
    return out + [(n, f) for n, f, _ in area_layers(ctx, bbox)]


def restricted_in(ctx, fmts, bbox):
    """Sources marked restricted=true (NC / ND / personal-use licences) that this export would carry, with
    their licence text: the user must know before handing the drive to someone else."""
    hit = {}
    for i, v in ctx.sources.items():
        if not v.get("restricted"):
            continue
        f = best_raster(ctx, i) if v.get("out") else None
        if "copy" in fmts:
            p = ctx.path(v["out"]) if v.get("out") else None
            if (p and p.exists()) or f:
                hit[i] = v.get("licence", "?")
        elif f and bbox and (b := raster_ll_box(f)) and overlaps(b, list(bbox)):
            hit[i] = v.get("licence", "?")
    for aj in area_jsons(ctx)[0]:                       # area clips carry their source's licence
        a = area_meta(aj)
        for k, frm in ((a or {}).get("from") or {}).items():
            f = aj.parent / f"{k}.tif"
            if k not in AREA_IMG or not f.is_file():
                continue
            if "copy" not in fmts and not (bbox and (b := raster_ll_box(f)) and overlaps(b, list(bbox))):
                continue
            v = ctx.sources.get(frm)
            if v is None:
                hit[frm] = "unknown source (not in the catalog): check its licence before sharing"
            elif v.get("restricted"):
                hit[frm] = v.get("licence", "?")
    return hit


def export_mobile(ctx, out, bbox, plan):
    """out/phone/*.mbtiles for OsmAnd / Locus / Alpine Quest / Avenza / ATAK."""
    dst = out / "phone"
    scratch = ctx.scratch() / "export"
    scratch.mkdir(exist_ok=True)
    if plan:
        js, dup = area_jsons(ctx)
        pm = [phone_map_name(aj, k, dup) for aj in js for k in phone_map_keys(aj.parent)
              if (aj.parent / f"{k}.mbtiles").is_file() and (b := mbtiles_bounds(aj.parent / f"{k}.mbtiles"))
              and overlaps(b, list(bbox))]
        return {"would_build": [f"{i}.mbtiles" for i, _ in imagery_layers(ctx, bbox)], "phone_maps": pm,
                "note": "every .mbtiles also gets an OsmAnd .sqlitedb copy (about the same size)",
                "to": str(dst), "bbox": bbox}
    dst.mkdir(parents=True, exist_ok=True)
    built, kept = [], []
    for name, src in imagery_layers(ctx, bbox, scratch):
        say_stop(ctx)
        if DRAIN.is_set():
            break
        o = dst / f"{name}.mbtiles"
        if mbtiles_current(o, mbtiles_key(src, bbox)):
            kept.append(o.name); continue
        ctx.log(f"building {o.name} (this can take a while for aerial) ...")
        to_mbtiles(ctx, src, o, bbox)
        built.append(o.name)
    # the areas' own maps drawn for phones (lmap area phone-map): copied whole, newest wins
    js, dup = area_jsons(ctx)
    for aj in js:
        if DRAIN.is_set():
            break
        for k in phone_map_keys(aj.parent):
            src = aj.parent / f"{k}.mbtiles"
            if not src.is_file() or not (b := mbtiles_bounds(src)) or not overlaps(b, list(bbox)):
                continue
            o = dst / phone_map_name(aj, k, dup)
            if o.is_file() and o.stat().st_size == src.stat().st_size and o.stat().st_mtime >= src.stat().st_mtime:
                kept.append(o.name); continue
            ctx.log(f"copying {o.name} (area phone map) ...")
            install_file(src, o, link=False); built.append(o.name)
    # OsmAnd reads only .sqlitedb: one next to every .mbtiles (rebuilt when older than it)
    for n in built + kept:
        if DRAIN.is_set():
            break
        o = dst / n; sq = o.with_suffix(".sqlitedb")
        if o.is_file() and (not sq.is_file() or sq.stat().st_mtime < o.stat().st_mtime):
            mbtiles_to_sqlitedb(o, sq)
    dropped = drop_other_tiles(dst, set(built + kept), imagery_names(ctx)) if not DRAIN.is_set() else []
    osm = ctx.path(ctx.sources["osmand-au"]["dest"])
    return {"phone_dir": str(dst), "built": built, "kept": kept, "removed": dropped,
            "osmand_maps": sorted(f.name for f in osm.glob("*.obf.zip")) if osm.is_dir() else []}


def dted_cells(bbox):
    import math
    w, so, e, n = bbox
    for la in range(math.floor(so), math.ceil(n)):
        for lo in range(math.floor(w), math.ceil(e)):
            yield la, lo


def _dted(ctx, dst, bbox, scratch):
    """DTED cells (ATAK layout DTED/e146/s20.dt2) over bbox: DTED2 from 30m where we have it, else DTED1 from 90m."""
    cells = list(dted_cells(bbox))
    srcs = [(res, f, raster_ll_box(f)) for res, f, _ in dem_sources(ctx)]
    made, skipped = [], []
    for la, lo in cells:
        say_stop(ctx)
        if DRAIN.is_set():
            break
        ns, ew = ("s" if la < 0 else "n"), ("w" if lo < 0 else "e")
        lat_name, lon_name = f"{ns}{abs(la):02d}", f"{ew}{abs(lo):03d}"
        pick = next(((res, f) for res, f, b in srcs if b and b[0] <= lo + .5 <= b[2] and b[1] <= la + .5 <= b[3]), None)
        if not pick:
            skipped.append(f"{lon_name}/{lat_name}"); continue
        res, v = pick
        size, lvl = (3601, 2) if res <= 30 else (1201, 1)
        o = dst / "DTED" / lon_name / f"{lat_name}.dt{lvl}"
        if o.is_file() and gdal_ok(o):
            made.append(o.relative_to(dst).as_posix()); continue
        o.parent.mkdir(parents=True, exist_ok=True)
        h = 0.5 / (size - 1)
        t = scratch / f"dted-{lat_name}{lon_name}.tif"
        try:
            run(["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:4326", "-te", lo - h, la - h, lo + 1 + h, la + 1 + h,
                 "-ts", size, size, "-r", "bilinear", "-ot", "Int16", "-dstnodata", "-32767", v, t], ctx)
            part = o.with_suffix(o.suffix + ".part")
            st = subprocess.run(["gdalinfo", "-stats", str(t)], capture_output=True, text=True).stdout
            if "STATISTICS_MEAN" not in st:                       # all sea / no data: no cell (as before)
                skipped.append(f"{lon_name}/{lat_name}"); continue
            run(["gdal_translate", "-q", "-of", "DTED", t, part], ctx)
            part.rename(o)
            Path(str(part) + ".aux.xml").unlink(missing_ok=True)
        finally:
            t.unlink(missing_ok=True)
        made.append(o.relative_to(dst).as_posix())
    return made, skipped


def export_atak_dted(ctx, atak_dir, bbox):
    scratch = ctx.scratch() / "export"; scratch.mkdir(exist_ok=True)
    made, skipped = _dted(ctx, atak_dir, bbox, scratch)
    return {"cells": len(made), "missing": skipped}


# tappable ATAK features: kind -> KML colour (aabbggrr). Lines get width 3.
ATAK_FEAT_COL = {"track": "ff1e64a0", "4WD track": "ff0078ff", "power line": "ff0000dc", "pipeline": "ff808080",
                 "ford (line)": "ffff9600", "peak": "ff000000"}
ATAK_FEAT_TAGS = ("surface", "tracktype", "4wd_only", "access", "seasonal", "smoothness", "width", "operator",
                  "voltage", "drinking_water", "fee", "opening_hours", "phone", "ele", "intermittent", "capacity")
ATAK_FEAT_MAX = 20000          # per layer: ATAK stays responsive; the rest is in the MBTiles / QGIS


def _hstore(s):
    return dict(re.findall(r'"([^"]+)"=>"((?:[^"\\]|\\.)*)"', s or ""))


def atak_features_kml(ctx, kml, bbox, peaks=()):
    """One KML of tappable features in bbox: OSM field points/lines (au-field.gpkg: water, huts, fuel, fords,
    gates, tracks, power lines...) + named peaks. A folder per kind; a tap shows kind, name, MGRS, lat/lon,
    length (lines) and the useful OSM tags. Returns {kind: count} ({} when there is no field layer)."""
    he = _kml_txt
    from osgeo import ogr
    ogr.UseExceptions()
    fg = ctx.path("data/australia") / "osm" / "au-field.gpkg"
    by = {}                                           # kind -> [placemark xml]
    w, s, e, n = bbox
    cut = []
    try:
        ds = ogr.Open(str(fg)) if fg.is_file() else None
    except RuntimeError as e_:
        ctx.log(f"ATAK features: {fg.name} unreadable ({e_}); no field features"); ds = None
    if ds is not None:
        for ln in ("field_points", "field_lines"):
            ly = ds.GetLayerByName(ln)
            if ly is None:
                continue
            ly.SetSpatialFilterRect(w, s, e, n); k = 0
            has_tags = ly.GetLayerDefn().GetFieldIndex("osm_tags") >= 0
            for f in ly:
                if k >= ATAK_FEAT_MAX:
                    cut.append(ln); ctx.log(f"ATAK features: {ln} capped at {ATAK_FEAT_MAX} (box too big)"); break
                g = f.GetGeometryRef()
                if g is None:
                    continue
                if g.GetGeometryCount() > 1 or ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbMultiLineString:
                    g = ogr.ForceToLineString(g.Clone())   # multi-part way: merged (gaps bridged) for the tap
                kind = f.GetField("kind") or "feature"; name = f.GetField("name")
                tags = _hstore(f.GetField("osm_tags")) if has_tags else {}
                pm = _atak_pm(kind, name, g, tags, he)
                if pm:
                    by.setdefault(kind, []).append(pm); k += 1
        ds = None
    for name, lon, lat, tags in peaks:
        g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(lon, lat)
        by.setdefault("peak", []).append(_atak_pm("peak", name, g, tags, he))
    if not by:
        return {}
    st = []
    for kind in by:
        c = ATAK_FEAT_COL.get(kind, "ff00b4ff"); sid = _atak_sid(kind)
        st.append(f'<Style id="{sid}"><IconStyle><color>{c}</color><scale>0.9</scale><Icon><href>'
                  'http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle>'
                  f'<LineStyle><color>{c}</color><width>3</width></LineStyle><LabelStyle><scale>0.8</scale></LabelStyle></Style>')
    body = "".join(f"<Folder><name>{he(k)} ({len(v)})</name>{''.join(v)}</Folder>" for k, v in sorted(by.items()))
    part = kml.with_name(kml.name + ".part")
    part.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                    f"<name>lmap features</name>{''.join(st)}{body}</Document></kml>\n", encoding="utf-8")
    os.replace(part, kml)
    return {**{k: len(v) for k, v in by.items()}, **({"capped": cut} if cut else {})}


def _atak_sid(kind):
    """KML style id: readable + a short hash, so kinds that differ only in case/punctuation never collide."""
    import hashlib
    return re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") + "-" + hashlib.sha1(kind.encode()).hexdigest()[:6]


def _half_way(pts):
    """(lon, lat) halfway ALONG the line (by distance), not the middle vertex."""
    seg = [_geo_len_km([a, b]) for a, b in zip(pts, pts[1:])]; half = sum(seg) / 2; run_ = 0.0
    for (a, b), s in zip(zip(pts, pts[1:]), seg):
        if run_ + s >= half and s > 0:
            f = (half - run_) / s
            return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])
        run_ += s
    return pts[0][0], pts[0][1]


def _atak_pm(kind, name, g, tags, he):
    """One KML Placemark: geometry + an HTML table ATAK shows on tap."""
    from osgeo import ogr
    sid = _atak_sid(kind)
    if g.GetGeometryType() in (ogr.wkbPoint, ogr.wkbPoint25D):
        lon, lat = g.GetX(), g.GetY(); geo = f"<Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point>"; ln = None
    else:
        pts = g.GetPoints() or []
        if not pts:
            return ""
        lon, lat = _half_way(pts)
        geo = "<LineString><tessellate>1</tessellate><coordinates>" + " ".join(f"{p[0]:.6f},{p[1]:.6f}" for p in pts) \
              + "</coordinates></LineString>"
        ln = _geo_len_km(pts)
    rows = [("Type", kind), ("Name", name or "-"), ("MGRS", to_mgrs(lat, lon)), ("Lat, lon", f"{lat:.5f}, {lon:.5f}")]
    if ln is not None:
        rows.append(("Length", f"{ln:.2f} km"))
    rows += [(k.replace("_", " "), tags[k]) for k in ATAK_FEAT_TAGS if tags.get(k)]
    if kind == "peak" and tags.get("ele"):
        rows = [r for r in rows if r[0] != "ele"] + [("Height", f"{tags['ele']} m (OSM)")]
    desc = "\n".join(f"{he(a)}: {he(str(b))}" for a, b in rows)      # plain text: OsmAnd's KML->GPX chokes on HTML
    return (f"<Placemark><name>{he(name or kind)}</name><styleUrl>#{sid}</styleUrl>"
            f"<description>{desc}</description>{geo}</Placemark>")


def _kml_txt(s):
    """Text with no XML entities at all (some phone importers re-parse it naively): & -> 'and', <>\"' dropped."""
    return re.sub(r"[<>\"']", "", str(s).replace("&", " and ")).replace("  ", " ")


def _geo_len_km(pts):
    import math
    t = 0.0
    for (x1, y1, *_), (x2, y2, *_) in zip(pts, pts[1:]):
        p1, p2 = math.radians(y1), math.radians(y2)
        a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
        t += 2 * 6371.0088 * math.asin(min(1, math.sqrt(a)))
    return t


def export_atak(ctx, out, bbox, plan):
    """out/atak/ laid out like the device's /sdcard/atak/ folder:
         atak/DTED/e146/s20.dt2      (DTED2 from Copernicus 30m, else DTED1 from 90m)
         atak/imagery/*.mbtiles     (same layers as phone/)
         atak/overlays/poi.kml      (hospitals, fuel, water, police... from OSM)
       plus lmap-<area>.zip, a TAK data package (MANIFEST/manifest.xml) carrying the overlay."""
    import uuid, zipfile
    dst = out / "atak"
    cells = list(dted_cells(bbox))
    if plan:
        return {"would_build": {"dted_cells": len(cells), "imagery": "phone layers", "overlay": "poi.kml"},
                "to": str(dst), "bbox": bbox}
    scratch = ctx.scratch() / "export"; scratch.mkdir(exist_ok=True)
    made, skipped = _dted(ctx, dst, bbox, scratch)
    # imagery: reuse phone/ if built, else build
    img = dst / "imagery"; img.mkdir(parents=True, exist_ok=True)
    ph = out / "phone"; done = set()
    for name, src in imagery_layers(ctx, bbox, scratch):
        if DRAIN.is_set():
            break
        o = img / f"{name}.mbtiles"; p2 = ph / f"{name}.mbtiles"; key = mbtiles_key(src, bbox); done.add(o.name)
        if mbtiles_current(o, key):
            continue
        o.unlink(missing_ok=True)                 # another area / older source: never kept under the same name
        if mbtiles_current(p2, key):
            part = o.with_name(o.name + ".part")
            try:
                try:
                    os.link(p2, part)             # same file, no extra space (if the FS allows)
                except OSError:
                    shutil.copy2(p2, part)
                os.replace(part, o)
            finally:
                part.unlink(missing_ok=True)
        else:
            ctx.log(f"building {o.name} ..."); to_mbtiles(ctx, src, o, bbox)
    if not DRAIN.is_set():
        drop_other_tiles(img, done, imagery_names(ctx))
    # POI overlay from the OSM clip (if present)
    kml, npoi = None, 0
    gp = ctx.path(ctx.sources["osm-australia"]["from"])          # raw .osm.pbf (points keep other_tags)
    if gp.is_file():
        kml = dst / "overlays" / "poi.kml"; kml.parent.mkdir(exist_ok=True); kml.unlink(missing_ok=True)
        w, so, e, n = bbox
        cond = " OR ".join(f"other_tags LIKE '%\"amenity\"=>\"{k}\"%'" for k in ATAK_POI)
        run(["ogr2ogr", "-f", "KML", kml, gp, "points", "-nln", "lmap POI", "-dsco", "NameField=name",
             "-spat", w, so, e, n, "-where", cond, "-select", "name,other_tags"], ctx,
            env={**os.environ, "CPL_TMPDIR": str(ctx.scratch())})
        npoi = kml.read_text(errors="replace").count("<Placemark")
    # tappable features: field points/lines + named peaks (peaks from the area-clipped OSM points if any)
    fk = dst / "overlays" / "features.kml"; fk.parent.mkdir(parents=True, exist_ok=True)
    feats = atak_features_kml(ctx, fk, bbox, _atak_peaks(ctx, bbox, gp))
    if not feats:
        fk.unlink(missing_ok=True); fk = None
    # TAK data package: overlay + manifest (imagery/DTED go via the folder copy: too big for a package)
    pkg = None
    inc = [(p, z_) for p, z_ in ((kml, "poi/poi.kml"), (fk, "features/features.kml")) if p]
    if inc:
        area = "{:.1f}_{:.1f}".format(bbox[0], bbox[1]).replace("-", "m")
        pkg = dst / f"lmap-{area}.zip"
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lmap:{area}"))   # stable: re-export replaces, not duplicates
        man = ('<MissionPackageManifest version="2"><Configuration>'
               f'<Parameter name="uid" value="{uid}"/><Parameter name="name" value="lmap {area}"/>'
               '<Parameter name="onReceiveDelete" value="true"/></Configuration><Contents>'
               + "".join(f'<Content ignore="false" zipEntry="{z_}"/>' for _, z_ in inc)
               + '</Contents></MissionPackageManifest>')
        part = pkg.with_name(pkg.name + ".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MANIFEST/manifest.xml", man)
            for p, z_ in inc:
                z.write(p, z_)
        os.replace(part, pkg)
    return {"atak_dir": str(dst), "dted": len(made), "dted_missing_cells": skipped, "poi": npoi,
            "features": feats, "data_package": str(pkg) if pkg else None}


def _atak_peaks(ctx, bbox, pbf):
    """Named peaks in bbox as (name, lon, lat, tags), one per name within ~2 km (OSM maps some twice).
    From an area/map osm.gpkg covering bbox if one exists (fast), else nothing (the raw pbf scan is minutes)."""
    from osgeo import ogr
    ogr.UseExceptions()
    for kind in ("maps", "areas"):
        for j in lib_jsons(ctx, kind):
            try:
                b = json.loads(j.read_text()).get("bbox")
            except (OSError, ValueError):
                continue
            g = j.parent / "osm.gpkg"
            if not (b and g.is_file() and b[0] <= bbox[0] and b[1] <= bbox[1] and b[2] >= bbox[2] and b[3] >= bbox[3]):
                continue
            try:
                ds = ogr.Open(str(g)); ly = ds.GetLayerByName("points")
            except RuntimeError:
                continue
            if ly is None:
                continue
            ly.SetSpatialFilterRect(*bbox)
            ly.SetAttributeFilter("other_tags LIKE '%\"natural\"=>\"peak\"%' AND name IS NOT NULL")
            out = []
            for f in ly:
                p = f.GetGeometryRef(); nm = f.GetField("name"); t = _hstore(f.GetField("other_tags"))
                dup = [i for i, o in enumerate(out) if o[0] == nm and abs(o[1] - p.GetX()) < .02 and abs(o[2] - p.GetY()) < .02]
                if dup:
                    if t.get("ele") and not out[dup[0]][3].get("ele"):
                        out[dup[0]] = (nm, p.GetX(), p.GetY(), t)    # keep the copy that has a height
                    continue
                out.append((nm, p.GetX(), p.GetY(), t))
            ds = None
            return out
    return []


ENC_README = """These .zip files are AES-256 encrypted. You need the passphrase.

  Windows : 7-Zip (7-zip.org) -> right-click -> 7-Zip -> Extract (Windows 11 Explorer also opens them).
  Linux   : 7z x copy.zip        (package p7zip-full / 7zip; plain `unzip` can't do AES)
  Android : ZArchiver (free) -> tap the zip -> passphrase -> extract, then import into ATAK/OsmAnd.

File names inside are visible without the passphrase; the contents are not.
Once extracted, the files are NOT encrypted: delete them when done, or keep them on an encrypted device.
SHA256SUMS lists the zips: `lmap export verify <dir>` or `sha256sum -c SHA256SUMS`.
"""

OFFLINE_README = """lmap offline export
====================
Made {when} by lmap {ver}. Area for phone/ATAK: {bbox}.

CHECK IT'S INTACT
  cd <this folder> && sha256sum -c --quiet SHA256SUMS      (no output = all good)
  or: lmap export verify <this folder>

maps/        Full copy of the map library. Open any .qgz in QGIS (paths are relative).
  data/australia/        australia.qgz (national overview), 90m elevation, OSM (.osm.pbf + .gpkg), GA maps.
  data/australia/queensland/  qld.qgz (whole state), 30m elevation, satellite, topo, Govt layers, tides.
  areas/<country>/<state>/<name>/  Detailed maps of smaller areas: <name>.qgz, MGA grid, mils. Self-contained.
  apps/osmand/*.obf.zip  OsmAnd maps: unzip, copy the .obf into Android/obb/net.osmand/ (or the app's
                         maps folder), or open the .zip with OsmAnd.

phone/       *.mbtiles imagery. Opens in OsmAnd (Online maps plugin > import), Locus,
             Alpine Quest, Avenza, ATAK. Copy to the phone and open with the app.

atak/        Copy the CONTENTS onto the device's  /sdcard/atak/  (merge folders):
  DTED/eXXX/sYY.dt2      Elevation (ATAK 3D, line of sight, route profiles).
  imagery/*.mbtiles      Offline basemaps (ATAK: Maps & Favorites > imagery).
  overlays/poi.kml       Hospitals, fuel, water, police... (Import Manager > Local SD).
  overlays/features.kml  Tappable: water, huts, fords, gates, tracks, power lines, peaks. Tap one for its
                         type, name, MGRS, length, surface/access (Import Manager > Local SD).
  lmap-*.zip             TAK data package of both overlays (Import Manager, or send to team).

lmap-tool/   The lmap tool + source catalog. Needs python3 + gdal-bin. bin/lmap --help

Nothing here needs the internet. Keep 2 copies, one off-site. Re-run the export monthly.
"""


def read_passphrase(ctx):
    """Passphrase from LMAP_PASSPHRASE_FILE (first line) or typed twice on the terminal. Never from argv/env text."""
    pf = os.environ.get("LMAP_PASSPHRASE_FILE")
    if pf:
        pw = Path(pf).read_text().splitlines()[0] if Path(pf).is_file() else ""
    else:
        if not sys.stdin.isatty():
            raise CliError(E_USAGE, "--encrypt needs a passphrase", "run in a terminal, or set LMAP_PASSPHRASE_FILE=<file>")
        import getpass
        pw = getpass.getpass("Export passphrase: ")
        if pw != getpass.getpass("Again: "):
            raise CliError(E_USAGE, "passphrases differ")
    if len(pw) < 12:
        raise CliError(E_USAGE, "passphrase too short (min 12 characters)", "use 4+ random words")
    return pw


def aes_zip_check(ctx, z, pw, src_dir=None):
    """Prove an AE-2 zip opens with pw: per entry, the password verifier + HMAC over the stored bytes, and (given
    src_dir) the decrypted bytes equal the source file. The passphrase never goes on a command line (7z only reads
    one from argv or a terminal: argv is visible to every user via ps). If 7-Zip is here it must also list the
    archive as AES (no passphrase needed for that)."""
    import hmac, hashlib, struct, zipfile
    import numpy as np
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    bad = lambda why: CliError(E_FAIL, f"encrypted zip check failed: {why}", "re-run the export")
    try:
        infos = zipfile.ZipFile(z).infolist()
    except (zipfile.BadZipFile, OSError) as e:
        raise bad(f"not a readable zip ({e})")
    with open(z, "rb") as fh:
        for zi in infos:
            say_stop(ctx)
            if ABORT.is_set():
                raise Aborted()
            fh.seek(zi.header_offset)
            h = fh.read(30)
            if h[:4] != b"PK\x03\x04":
                raise bad(f"{zi.filename}: bad local header")
            nl, xl = struct.unpack("<HH", h[26:30]); fh.seek(nl + xl, 1)
            salt, pv = fh.read(16), fh.read(2)
            k = hashlib.pbkdf2_hmac("sha1", pw.encode(), salt, 1000, 66)
            if not hmac.compare_digest(k[64:], pv):
                raise bad(f"{zi.filename}: passphrase doesn't open it")
            ecb = Cipher(algorithms.AES(k[:32]), modes.ECB()).encryptor()
            mac = hmac.new(k[32:64], digestmod=hashlib.sha1)
            left, ctr = zi.compress_size - 28, 1
            src = open(Path(src_dir) / zi.filename, "rb") if src_dir else None
            try:
                while left > 0:
                    c = fh.read(min(left, 1 << 22)); left -= len(c)
                    if not c:
                        raise bad(f"{zi.filename}: truncated")
                    mac.update(c)
                    if src:
                        nb = (len(c) + 15) // 16
                        b = np.zeros((nb, 2), dtype="<u8"); b[:, 0] = np.arange(ctr, ctr + nb, dtype="<u8"); ctr += nb
                        ks = np.frombuffer(ecb.update(b.tobytes()), dtype=np.uint8)[:len(c)]
                        if (np.frombuffer(c, dtype=np.uint8) ^ ks).tobytes() != src.read(len(c)):
                            raise bad(f"{zi.filename}: decrypts to different bytes")
                if src and src.read(1):
                    raise bad(f"{zi.filename}: shorter than its source")
            finally:
                if src:
                    src.close()
            if not hmac.compare_digest(mac.digest()[:10], fh.read(10)):
                raise bad(f"{zi.filename}: authentication code mismatch")
    if shutil.which("7z"):
        r = subprocess.run(["7z", "l", "-slt", str(z)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=600)
        n_aes = len(re.findall(r"^Method = .*AES-?256", r.stdout, re.M))
        if r.returncode or n_aes != len(infos):          # advisory: our own check above is the proof; 7-Zip versions
            ctx.log(f"  note: 7-Zip lists {n_aes} of {len(infos)} entries as AES-256 (rc {r.returncode}); "
                    "lmap's own check passed")             # word the listing differently
    return len(infos)


def encrypt_zip(ctx, src_dir, out_zip, pw):
    """WinZip AES-256 zip (AE-2), written here with `cryptography` (7z only takes a passphrase from a terminal).
    Opens with 7-Zip (Linux/Windows), Windows 11 Explorer, ZArchiver (Android). Stored, not compressed (maps are
    already compressed). File NAMES stay visible (zip can't hide them); contents are encrypted + authenticated."""
    import hmac, hashlib, struct, secrets
    import numpy as np
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    part = Path(str(out_zip) + ".part"); part.unlink(missing_ok=True)
    files = sorted(f for f in Path(src_dir).rglob("*") if f.is_file())
    cen, off = [], 0
    try:
        with open(part, "wb") as z:
            for f in files:
                say_stop(ctx)
                if ABORT.is_set():
                    raise Aborted()
                name = f.relative_to(src_dir).as_posix().encode()
                salt = secrets.token_bytes(16)
                k = hashlib.pbkdf2_hmac("sha1", pw.encode(), salt, 1000, 66)   # AE-2: 32 enc + 32 mac + 2 verify
                ek, mk, pv = k[:32], k[32:64], k[64:]
                # WinZip AES = AES-CTR with a LITTLE-endian counter from 1 (library CTR mode is big-endian, so the
                # keystream is made here: ECB over counter blocks, XORed in)
                ecb = Cipher(algorithms.AES(ek), modes.ECB()).encryptor(); ctr = 1
                mac = hmac.new(mk, digestmod=hashlib.sha1)
                def crypt(data):
                    nonlocal ctr
                    nb = (len(data) + 15) // 16
                    blocks = np.zeros((nb, 2), dtype="<u8"); blocks[:, 0] = np.arange(ctr, ctr + nb, dtype="<u8")
                    ks = np.frombuffer(ecb.update(blocks.tobytes()), dtype=np.uint8)[:len(data)]
                    ctr += nb
                    return (np.frombuffer(data, dtype=np.uint8) ^ ks).tobytes()
                size = f.stat().st_size; csize = 16 + 2 + size + 10
                t = time.localtime(f.stat().st_mtime)
                dtime = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
                ddate = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
                extra = struct.pack("<HHH2sBH", 0x9901, 7, 2, b"AE", 3, 0)   # AE-2, AES-256, stored
                z64 = size >= 0xFFFFFFFF or csize >= 0xFFFFFFFF or off >= 0xFFFFFFFF
                lx = extra + (struct.pack("<HHQQ", 1, 16, size, csize) if z64 else b"")
                hdr = struct.pack("<IHHHHHIIIHH", 0x04034B50, 45 if z64 else 51, 1 | 0x800, 99, dtime, ddate, 0,
                                  0xFFFFFFFF if z64 else csize, 0xFFFFFFFF if z64 else size, len(name), len(lx))
                z.write(hdr + name + lx + salt + pv)
                with open(f, "rb") as fh:
                    while chunk := fh.read(1 << 22):           # 4 MiB = whole 16-byte blocks: counter stays aligned
                        c = crypt(chunk); mac.update(c); z.write(c)
                z.write(mac.digest()[:10])
                cen.append((name, extra, size, csize, off, dtime, ddate))
                off += len(hdr) + len(name) + len(lx) + csize
            cd_start = off; cd = b""
            for name, extra, size, csize, o, dtime, ddate in cen:
                big = size >= 0xFFFFFFFF or csize >= 0xFFFFFFFF or o >= 0xFFFFFFFF
                cx = extra + (struct.pack("<HHQQQ", 1, 24, size, csize, o) if big else b"")
                cd += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x033F, 45 if big else 51, 1 | 0x800, 99,
                                  dtime, ddate, 0, 0xFFFFFFFF if big else csize, 0xFFFFFFFF if big else size,
                                  len(name), len(cx), 0, 0, 0, 0o100644 << 16, 0xFFFFFFFF if big else o) + name + cx
            z.write(cd); end = cd_start + len(cd)
            n = len(cen)
            if n >= 0xFFFF or end >= 0xFFFFFFFF:           # zip64 end records
                z.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, len(cd), cd_start))
                z.write(struct.pack("<IIQI", 0x07064B50, 0, end, 1))
            z.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF),
                                min(len(cd), 0xFFFFFFFF), min(cd_start, 0xFFFFFFFF), 0))
        aes_zip_check(ctx, part, pw, src_dir)
        part.rename(out_zip)
    except BaseException:
        part.unlink(missing_ok=True); raise
    return out_zip

LMAP_SCRATCH_PREFIX = ("area-", "map-", "print-", "image-", "verify-", "sheet-", "lidar-z", "contours-", "au-", "qld-",
                       "nsw-", "mgrs-", "sheets-", "auc.", "lmap-tides-", "clip-", "moon-qgis", "hillshade", "export",
                       "blank-rgba", "tiles")
RESUME_SUFFIX = (".part.gpkg", ".resume.json", ".tiles", ".cache", ".stac", ".files", ".ranges")


def _lmap_pid(n):
    """n is a running lmap process (its cmdline names the lmap script)."""
    try:
        return n != os.getpid() and any(Path(a.decode(errors="replace")).name == "lmap"
                                        for a in Path(f"/proc/{n}/cmdline").read_bytes().split(b"\0")[:3])
    except (OSError, ValueError):
        return False


def _empty_dir(y):
    try:
        return not any(y.iterdir())
    except OSError:
        return False


def _held_locks():
    """Names of every lmap lock held now: STATE/locks/*.lock plus top-level STATE/*.lock (export.lock)."""
    out = set()
    for d in (STATE / "locks", STATE):
        try:
            out |= {p.stem for p in d.glob("*.lock") if lock_held(p)}
        except OSError:
            pass
    return out


def _hot_action(f, days=14):
    """clean's verdict on one <scratch>/hot entry: in use (a reader's shared lock) -> keep; a stray .part/.lock or a
    copy unused for `days` -> delete; else keep (reused by the next map). The per-source .lock files (bytes) are
    never deleted: a copier could hold one. A .part is judged by its source's .lock (held = a copy in progress)."""
    if f.name.endswith(".lock"):
        return "keep", "lock for local copies"
    try:
        o = os.open(f.parent / (f.name.split("-")[0] + ".lock") if f.name.endswith(".part") else f, os.O_RDONLY)
        try:
            fcntl.flock(o, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(o, fcntl.LOCK_UN)
        finally:
            os.close(o)
    except OSError:
        return "keep", "local copy in use by a running lmap"
    if f.name.endswith(".part"):
        return "delete", "leftover of a local copy"
    if time.time() - f.stat().st_mtime > days * 86400:
        return "delete", f"local copy of a network-share input, unused for {days} days"
    return "keep", "local copy of a network-share input (re-made if deleted)"


def _hot_unlink(f):
    """Delete a hot/ copy (or .part) only while holding its source's copy lock (no copier, no reader between open
    and its shared lock) and, for a copy, an exclusive lock on it (no reader). False = in use, left alone."""
    lk = os.open(f.parent / (f.name.split("-")[0] + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if f.name.endswith(".part"):
            f.unlink(); return True
        o = os.open(f, os.O_RDONLY)
        try:
            fcntl.flock(o, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(o).st_ino != os.stat(f).st_ino:
                return False
            f.unlink(); return True
        finally:
            os.close(o)
    except BlockingIOError:
        return False
    finally:
        os.close(lk)


STATE_DAYS = 30
STATE_KEEP = {"queue", "export", "au-contours"}        # lmap-wide STATE files that are not per-source
LOCK_NEVER = ("build.", "cog-build")          # build slots / rebuild markers: not Lock-class files


def _state_plan(ctx, days=STATE_DAYS, mine=()):
    """clean rows for lmap's state dir: old refresh logs, files of sources no longer in the catalog, and lock files
    unused for `days`. Queue, tier logs, start gates, build slots and markers are never listed."""
    out, old = [], time.time() - days * 86400
    def age_ok(f):
        try:
            return f.stat().st_mtime < old
        except OSError:
            return False
    rd = STATE / "refresh"
    try:
        runs = sorted(x for x in rd.iterdir() if x.is_dir())
    except OSError:
        runs = []
    if "refresh" in mine or not lock_held(STATE / "locks" / "refresh.lock"):
        for x in runs[:-5]:                              # the newest 5 runs' logs are always kept
            if age_ok(x):
                out.append((x, du_tree(x), "delete", f"refresh log older than {days} days"))
    try:
        top = sorted(STATE.iterdir())
    except OSError:
        top = []
    for f in top:
        m = re.fullmatch(r"(.+?)\.(json|log|log\.\d+|stac\.json)", f.name)
        if not m or not f.is_file() or m.group(1) in ctx.sources or m.group(1) in STATE_KEEP \
                or m.group(1).startswith("qld-"):
            continue
        if age_ok(f):
            out.append((f, f.stat().st_size, "delete", f"{m.group(1)} is no longer in the catalog"))
    try:
        locks = sorted((STATE / "locks").glob("*.lock"))
    except OSError:
        locks = []
    for f in locks:
        if f.name.startswith(LOCK_NEVER) or f.stem in ("queue", "refresh") or not age_ok(f) or lock_held(f):
            continue
        out.append((f, 0, "delete", f"lock file unused for {days} days"))
    return out


ROOT_LOCKS = ("area-", "map-", "area-image-", "area-export-", "area-phone-")    # + NAME.lock: someone is writing that folder
# exactly what lmap writes there: install_file/atomic_write .<name>.<pid>[.<tid>].tmp, <out>.part, x.part.tif/.gpkg
ROOT_TMP = re.compile(r"\..+\.\d+(?:\.\d+)?\.tmp|.+\.part(?:\.(?:tif|gpkg|vrt|mbtiles))?|.+\.\d+\.part\.(?:tif|gpkg)")


def _root_busy(d):
    return any(lock_held(STATE / "locks" / f"{p}{d.name}.lock") for p in ROOT_LOCKS)


def _root_still(f):
    """Delete-time re-check of a root row (the plan may be minutes old): folder not being written, an orphan
    signature still has no file."""
    if _root_busy(f.parent):
        return False
    return not (f.name.endswith(".lmap-sig") and f.with_name(f.name[:-len(".lmap-sig")]).exists())


def _root_plan(ctx):
    """clean rows for the area/map folders on the data root (the only root folders lmap leaves debris in; a full
    root walk over a network-share takes minutes): half-written .part / .<name>.<pid>.tmp files of dead runs, and
    signatures (<file>.lmap-sig) whose file is gone. 'EMPTY' signatures (nothing in the box) have no file by design."""
    out, day = [], time.time() - 86400
    for kind in ("areas", "maps"):
        for aj in lib_jsons(ctx, kind):
            d = aj.parent
            if _root_busy(d):
                continue                                 # a create/map/image/export of it is running
            try:
                ents = sorted(d.iterdir())
            except OSError:
                continue
            for f in ents:
                n = f.name
                try:
                    st = f.lstat()
                except OSError:
                    continue
                if n.endswith(".lmap-sig") and not n.endswith(".qgz.lmap-sig"):
                    if (d / n[:-len(".lmap-sig")]).exists():
                        continue
                    try:
                        empty = f.read_text(errors="replace").startswith("EMPTY")
                    except OSError:
                        continue
                    if not empty:
                        out.append((f, st.st_size, "delete", "signature of a file no longer there"))
                elif ROOT_TMP.fullmatch(n) and f.is_file() and not f.is_symlink():
                    pids = [int(m) for m in re.findall(r"(?:^|[.-])(\d{3,8})(?=[.-]|$)", n)]
                    if any(_lmap_pid(p_) for p_ in pids) or st.st_mtime > day:
                        out.append((f, st.st_size, "keep", "may be in use (newer than a day or its run is alive)"))
                    else:
                        out.append((f, st.st_size, "delete",
                                    "half-written file of a dead run"))
    return out


def _lock_unlink(f):
    """Delete a lock file only while holding it, so no job holds it; Lock re-checks the inode after locking, so a
    job that opened it just before is not left on the orphan. False = in use now (kept)."""
    try:
        fd = os.open(f, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # re-checked under the lock: every take/release bumps mtime, so untouched for 30 days = no waiter left
        if not _same_inode(f, fd) or os.fstat(fd).st_mtime >= time.time() - STATE_DAYS * 86400:
            return False
        f.unlink(); return True
    except (BlockingIOError, PermissionError):
        return False
    finally:
        os.close(fd)


def clean_plan(ctx, mine=()):
    """[(path, bytes, action, why)] for everything in lmap's scratch dir: action delete | resume | keep.
    mine: lock names the caller itself holds (refresh.lock at the end of a refresh): they don't mean 'busy'."""
    sc = ctx.scratch(); held = _held_locks() - set(mine)
    sids = sorted(ctx.sources, key=len, reverse=True)
    # queue/refresh supervisors hold a lock for life but write no scratch themselves (their jobs take their own
    # locks); source locks are handled per source below. Anything else held = a job that may own unnamed files.
    busy = bool(held - {"queue", "refresh"} - set(sids))
    cur_auc = None
    try:
        dem = best_raster(ctx, "dem-au-30m")
        cur_auc = hashlib.sha1(au_contours_key(dem).encode()).hexdigest()[:10] if dem else None
    except Exception:
        pass
    try:
        ents = [x for x in sc.iterdir()]
    except OSError:
        ents = []
    tl = ctx.tiles()
    if tl.resolve() != (sc / "tiles").resolve() and tl.is_dir():
        ents.append(tl)
    out = []
    def size(x):
        try:
            return du_tree(x) if x.is_dir() else x.stat().st_size
        except OSError:
            return 0
    for x in sorted(ents):
        n = x.name
        if n == "hot" and x.is_dir():                 # local copies of network-share inputs (hot_copy)
            for y in sorted(x.iterdir()):
                out.append((y, size(y), *_hot_action(y)))
            continue
        if n.endswith(".ckpt") and x.is_dir():         # build checkpoint (overviews): kept while its source exists
            try:
                k_ = json.loads((x / "key.json").read_text())
                live = Path(k_["src"]).exists() and time.time() - (x / "key.json").stat().st_mtime < 7 * 86400
            except (OSError, ValueError, KeyError):
                live = False
            out.append((x, size(x), "resume" if live else "delete",
                        "build checkpoint (overviews), reused by the next build" if live else "build checkpoint, source gone"))
            continue
        if n == "kept" and x.is_dir():                # verified builds whose network-share copy failed: the next run installs them
            for y in sorted(x.glob("*.json")):
                f_ = y.with_suffix("")
                try:
                    o_ = Path(json.loads(y.read_text())["out"])
                    stale = not f_.is_file() or (o_.is_file() and o_.stat().st_mtime > f_.stat().st_mtime)
                except (OSError, ValueError, KeyError):
                    stale = True
                if stale:
                    try:
                        p_ = Path(str(o_) + ".part")
                        if p_.is_file():
                            out.append((p_, size(p_), "delete", "partial network-share copy of a superseded kept build"))
                    except NameError:
                        pass
                for z in (f_, y):
                    out.append((z, size(z), "delete" if stale else "resume",
                                "kept build, superseded" if stale else "verified build waiting to be copied to the root"))
            continue
        sid = next((i for i in sids if n.startswith(i + ".") or n.startswith(i + "-")), None)
        rest = n[len(sid):] if sid else n         # a source id may hold a year (sat-qld-s2-2025): not a pid
        pids = [int(m) for m in re.findall(r"(?:^|[.-])(\d{3,8})(?=[.-]|$)", rest)]
        if any(_lmap_pid(p_) for p_ in pids):
            out.append((x, size(x), "keep", "in use by a running lmap")); continue
        if sid and sid in held:
            out.append((x, size(x), "keep", f"{sid} is being fetched now")); continue
        if x in (sc / "tiles", tl) or n == "au-contours":
            try:
                kids = sorted(x.iterdir()) if x.is_dir() else []
            except OSError:
                kids = []
            for y in kids:
                src_ = None
                for i, v in ctx.sources.items():
                    try:
                        if v.get("out") and v.get("dest") and ctx.path(v["dest"]).resolve() == y.resolve():
                            src_ = i; break
                    except (OSError, RuntimeError):
                        pass
                if n != "au-contours" and not src_:
                    out.append((y, size(y), "keep", "not a known source's tile folder: left alone")); continue
                if (src_ and src_ in held) or (not src_ and busy):
                    out.append((y, size(y), "keep", "may be in use (an lmap job is running)"))
                elif n == "au-contours" and cur_auc and y.name != cur_auc:
                    out.append((y, size(y), "delete", "contour cells for an older DEM / settings"))
                elif src_ and ctx.sources[src_].get("out") and ctx.path(ctx.sources[src_]["out"]).is_file() \
                        and _empty_dir(y):
                    out.append((y, size(y), "delete", "empty tile folder"))
                else:
                    out.append((y, size(y), "resume", f"resume point for {src_ or 'Australia contours'}"))
            continue
        if sid and not pids and n.endswith(RESUME_SUFFIX):
            o = ctx.sources[sid].get("out") or (ctx.sources[sid].get("dest") if ctx.sources[sid].get("kind") == "arcgis" else None)
            try:
                done = bool(o) and ctx.path(o).is_file() and ctx.path(o).stat().st_mtime > x.stat().st_mtime
            except OSError:
                done = False
            if done:                             # older than the finished file: from before it was built
                out.append((x, size(x), "delete", f"{sid} finished after this was made")); continue
            out.append((x, size(x), "resume", f"resume point for {sid}")); continue
        if sid or n.startswith(LMAP_SCRATCH_PREFIX) or ".part" in n or (n.startswith(".") and n.endswith(".tmp")):
            if busy and not pids and not sid:
                out.append((x, size(x), "keep", "may be in use (an lmap job is running)")); continue
            out.append((x, size(x), "delete", "leftover from a finished or dead run")); continue
        out.append((x, size(x), "keep", "not made by lmap: left alone"))
    # not at the end of a refresh (it already cleans what it wrote; no network share walk on every refresh)
    return out + _state_plan(ctx, mine=mine) + (_root_plan(ctx) if ctx.root.is_dir() and "refresh" not in mine else [])


def cmd_clean(ctx):
    """Delete lmap's own leftovers in scratch (never a running job's files, never files lmap didn't make).
    --resume also deletes resume points (a stopped download or build then starts from zero)."""
    plan = clean_plan(ctx, getattr(ctx, "own_locks", ())); want = ("delete", "resume") if ctx.a.resume else ("delete",)
    sel = [(x, b, a_, w) for x, b, a_, w in plan if a_ in want]
    row = lambda x, b, a_, w: {"path": str(x), "mb": round(b / 1e6, 1), "why": w}
    res = {"action": "clean", "scratch": str(ctx.scratch()), "state": str(STATE),
           "delete": [row(*r) for r in sel][:200], "delete_mb": round(sum(b for _, b, _, _ in sel) / 1e6),
           "resume_kept": [row(*r) for r in plan if r[2] == "resume" and "resume" not in want][:50],
           "kept": [row(*r) for r in plan if r[2] == "keep"][:50]}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **res}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to delete without --yes", "preview: lmap clean --dry-run")
    freed, failed, busy_ = 0, [], []
    for x, b, a_, w in sel:
        try:
            if x.parent.name == "hot" and x.parent.parent == ctx.scratch():
                if not _hot_unlink(x):                   # in use since the plan was made: keep it
                    busy_.append(str(x)); continue
            elif ctx.root in x.parents:
                if not _root_still(x):
                    busy_.append(str(x)); continue
                x.unlink()
            elif x.parent == STATE / "locks":
                if not _lock_unlink(x):
                    busy_.append(str(x)); continue
            else:
                shutil.rmtree(x) if x.is_dir() and not x.is_symlink() else x.unlink()
            freed += b
        except OSError as e:
            failed.append(f"{x}: {e.strerror}")
    return {"ok": not failed, **res, "freed_mb": round(freed / 1e6), **({"failed": failed[:20]} if failed else {}),
            **({"kept_in_use": busy_[:50]} if busy_ else {})}


def cmd_data_hash(ctx):
    """SHA256SUMS for the whole data root (or one folder under it). Incremental: unchanged files (size+mtime) keep
    their hash, so re-runs are quick. `--check` re-hashes everything against the list."""
    need_root(ctx)
    top = ctx.path(ctx.a.sub).resolve() if ctx.a.sub else ctx.root
    if ctx.root.resolve() not in (top, *top.parents):
        raise CliError(E_USAGE, f"{top} is outside the data root")
    if ctx.a.check:
        c = copy.copy(ctx); c.a = argparse.Namespace(**{**vars(ctx.a), "target": str(top)})
        r = cmd_export_verify(c); r["action"] = "data.hash.check"; return r
    if ctx.a.dry_run:
        n = sum(1 for f in top.rglob("*") if f.is_file())
        return {"ok": True, "dry_run": True, "action": "data.hash", "dir": str(top), "files": n,
                "gb": round(du_tree(top) / 1e9, 1)}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write {top}/SHA256SUMS without --yes", "preview: --dry-run")
    t = time.time(); n, hashed = write_sums(ctx, top)
    return {"ok": True, "action": "data.hash", "dir": str(top), "files": n, "hashed": hashed,
            "seconds": int(time.time() - t), "check": f"lmap data hash{' ' + ctx.a.sub if ctx.a.sub else ''} --check"}


def cmd_export_create(ctx):
    need_root(ctx); t_run0 = time.time()      # files modified after this: always re-hashed
    need_tool("gdalwarp", "gdal_translate", "gdaladdo", "gdalbuildvrt", "ogr2ogr")
    out = Path(ctx.a.target).resolve() / "lmap-export"
    fmts = [f.strip() for f in ctx.a.format.split(",") if f.strip()]
    if "all" in fmts:
        fmts = ["copy", "phone", "atak"]
    bad = [f for f in fmts if f not in ("copy", "phone", "atak")]
    if bad or not fmts:
        raise CliError(E_USAGE, f"unknown --format {bad or fmts}", "use copy, phone, atak or all (comma list)")
    bbox = parse_bbox(ctx.a.bbox) if ("phone" in fmts or "atak" in fmts) else None   # no hidden default area
    if not Path(ctx.a.target).is_dir():
        raise CliError(E_NOTFOUND, f"target {ctx.a.target} is not a directory", "mount the USB first")
    restricted = restricted_in(ctx, fmts, bbox)
    for i, lic in restricted.items():
        ctx.log(f"WARNING: {i} has a restrictive licence, don't give this export to others without checking: {lic}")
    plan = {"copy": export_copy, "phone": lambda c, o, p: export_mobile(c, o, bbox, p),
            "atak": lambda c, o, p: export_atak(c, o, bbox, p)}
    if ctx.a.dry_run:
        free = free_bytes(ctx.a.target, raw=True)
        return {"ok": True, "dry_run": True, "action": "export.create", "target": str(out), "formats": fmts,
                "restricted_licences": restricted, "free_gb": round(free / 1e9, 1), **{f: plan[f](ctx, out, True) for f in fmts}}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write to {out} without --yes",
                       f"preview: lmap export create {ctx.a.target} --format {ctx.a.format} --dry-run")
    need = (du_tree(ctx.root) - (du_tree(out / "maps") if (out / "maps").is_dir() else 0) + 1e9) if "copy" in fmts else 0
    with Reservation(ctx, f"the export to {ctx.a.target}", need, ctx.a.target, raw=True,
                     hint="use a bigger drive, or fewer formats/areas (--dry-run shows sizes)"):
        return _export_create(ctx, fmts, out, plan, bbox, restricted, t_run0)


def _export_create(ctx, fmts, out, plan, bbox, restricted, t_run0):
    """export create after the checks, while its space is reserved."""
    pw = read_passphrase(ctx) if ctx.a.encrypt else None
    install_signals()
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    with Lock(STATE / "export.lock"):
        try:
            for f in fmts:
                say_stop(ctx)
                if DRAIN.is_set():
                    break
                ctx.log(f"== {f}")
                res[f] = plan[f](ctx, out, False)
        except Aborted:
            pass
        finally:
            shutil.rmtree(ctx.scratch() / "export", ignore_errors=True)
        if ABORT.is_set() or DRAIN.is_set():
            return {"ok": False, "action": "export.create", "target": str(out), "stopped": True, **res,
                    "hint": "re-run the same command; finished parts are reused"}
        txt = OFFLINE_README.format(when=time.strftime("%F %H:%M"), ver=VERSION,
                                    bbox=(",".join(f"{x:g}" for x in bbox) + " (w,s,e,n degrees)") if bbox
                                    else "none (full copy only)")
        if restricted:
            txt += "\nSome data here may NOT be shared freely (check before giving this drive to anyone):\n"
            txt += "".join(f"  {i}: {lic}\n" for i, lic in restricted.items())
        (out / "README-OFFLINE.txt").write_text(txt)
        if ctx.a.encrypt:
            ctx.log("== encrypting (AES-256 zip)")
            enc = []
            for sub in sorted(x for x in out.iterdir() if x.is_dir()):
                z = encrypt_zip(ctx, sub, out / f"{sub.name}.zip", pw)
                shutil.rmtree(sub); enc.append(z.name)
            res["encrypted"] = enc
            (out / "README-ENCRYPTED.txt").write_text(ENC_README)
        ctx.log("== checksums")
        nfiles, hashed = write_sums(ctx, out, getattr(ctx, "export_written", ()), since=t_run0)
    return {"ok": True, "action": "export.create", "target": str(out), "formats": fmts, **res,
            "files": nfiles, "hashed": hashed, "gb": round(du_tree(out) / 1e9, 2),
            "verify": f"lmap export verify {ctx.a.target}"}


def cmd_export_verify(ctx):
    top = Path(ctx.a.target).resolve()
    if (top / "lmap-export").is_dir():
        top = top / "lmap-export"
    sums = top / "SHA256SUMS"
    if not sums.is_file():
        raise CliError(E_NOTFOUND, f"no SHA256SUMS in {top}", "run `lmap export create` first")
    bad, missing, n = [], [], 0
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        h, rel = line.split("  ", 1); f = top / rel; n += 1
        if not f.is_file():
            missing.append(rel)
        elif sha256(f) != h:
            bad.append(rel)
        if n % 100 == 0:
            ctx.log(f"  checked {n} ...")
    ok = not bad and not missing
    return {"ok": ok, "action": "export.verify", "target": str(top), "checked": n, "bad": bad, "missing": missing}
