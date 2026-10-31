# lmap: store. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# Two tiers in the data root:
#   CACHE  areas/ and maps/: built from the stored sources, so deleting one only costs a rebuild.
#          `lmap cache list|rm|prune|pin|unpin`. A pinned one (.lmap-pin in its folder) is never pruned and
#          needs --force to rm.
#   STORE  data/ (downloaded sources) and local/ (imported layers): the only copy. `lmap data rm` / `import rm`
#          need --yes AND --confirm <id> (typed again); imported or no-longer-downloadable sources also --force.
# Import: `lmap import add PATH` - a layer (GeoPackage, GeoTIFF, KML/KMZ, GPX, shapefile, GeoJSON) goes into
# local/<id>/ + local/sources.local.toml (in the data root, never the repo); an lmap bundle (.lmapbundle, from
# `lmap export bundle`) becomes an area/map again; an ATAK data package (.zip) is imported layer by layer.
# Areas and maps clip every imported layer that overlaps them into imp-<id>.* (group "Imported" in QGIS).

PIN = ".lmap-pin"
LOCAL_CAT = "local/sources.local.toml"
IMPORT_VEC = (".gpkg", ".kml", ".kmz", ".gpx", ".shp", ".geojson", ".json")
IMPORT_RAS = (".tif", ".tiff")
BUNDLE_EXT = ".lmapbundle"


def confirm(ctx, what, typed=None):
    """Mutations: --yes, or a y/N question on a terminal (never with --json). typed: the user must type this
    exact text (the source id) - via --confirm, or at the prompt."""
    if typed is not None:
        got = getattr(ctx.a, "confirm", None)
        if got is None and sys.stdin.isatty() and not ctx.a.json:
            got = input(f"{what}\nType the id ({typed}) to confirm: ").strip()
            if got == typed and ctx.a.yes:
                return
        if got != typed:
            raise CliError(E_REFUSED, f"refusing: {what}", f"add --yes --confirm {typed} (the id typed again)")
    if ctx.a.yes:
        return
    if sys.stdin.isatty() and not ctx.a.json:
        if input(f"{what} [y/N] ").strip().lower() in ("y", "yes"):
            return
    raise CliError(E_REFUSED, f"refusing without --yes: {what}", "preview with --dry-run")


# ------------------------------------------------------------------------------------------------ cache (tier 1)
def cache_items(ctx):
    """Every built area/map: {kind, name, dir, gb, made, used, pinned}."""
    out = []
    for kind in ("areas", "maps"):
        for j in lib_jsons(ctx, kind):
            try:
                m = json.loads(j.read_text())
            except (OSError, ValueError):
                m = {}
            d = j.parent
            used = max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=j.stat().st_mtime)
            out.append({"kind": kind, "name": d.name, "dir": str(d), "level": m.get("level"),
                        "gb": round(du_tree(d) / 1e9, 2), "made": m.get("made"),
                        "used": time.strftime("%Y-%m-%d %H:%M", time.localtime(used)), "_t": used,
                        "pinned": (d / PIN).is_file()})
    return out


def _pub(i):
    return {k: v for k, v in i.items() if not k.startswith("_")}


def cache_find(ctx, name):
    """NAME, or areas/NAME / maps/NAME when both kinds have one."""
    kind, _, n = name.rpartition("/")
    hits = [i for i in cache_items(ctx) if i["name"] == n and (not kind or i["kind"] == kind)]
    if not hits:
        raise CliError(E_NOTFOUND, f"no area or map named {name}", "lmap cache list")
    if len(hits) > 1:
        raise CliError(E_USAGE, f"{name} is both an area and a map", f"say areas/{n} or maps/{n}")
    return hits[0]


def _cache_safe(ctx, i):
    """Only ever a folder <root>/areas|maps/<country>/<state>/<name> holding area.json."""
    raw = Path(i["dir"]); d = raw.resolve(); r = ctx.root.resolve()
    if d.parent.parent.parent.parent != r or d.parent.parent.parent.name != i["kind"] \
            or not (d / "area.json").is_file() or raw.is_symlink():
        raise CliError(E_REFUSED, f"{d} is not an area/map folder in the data root: not deleting it")
    busy = [f.name for f in d.glob("*.part*") if time.time() - f.stat().st_mtime < 600]
    if busy:
        raise CliError(E_REFUSED, f"{i['name']} is being built right now ({busy[0]})", "wait for it, or `lmap job stop`")
    return d


def _cache_delete(ctx, sel, verb):
    res = {"action": f"cache.{verb}", "delete": [_pub(i) for i in sel], "gb": round(sum(i["gb"] for i in sel), 2),
           "note": "cache only: rebuilt on demand from the stored sources (data/ and local/ are never touched)"}
    if ctx.a.dry_run or not sel:
        return {"ok": True, **({"dry_run": True} if ctx.a.dry_run else {"unchanged": True}), **res}
    dirs = [_cache_safe(ctx, i) for i in sel]
    confirm(ctx, f"delete {len(sel)} area/map folder(s), {res['gb']} GB: {', '.join(i['name'] for i in sel[:8])}")
    failed = []
    for d in dirs:
        try:
            shutil.rmtree(d)
        except OSError as e:
            failed.append(f"{d}: {e.strerror}")
    return {"ok": not failed, **res, **({"failed": failed} if failed else {})}


def cmd_cache_list(ctx):
    need_root(ctx)
    it = cache_items(ctx)
    if getattr(ctx.a, "kind", None):
        it = [i for i in it if i["kind"] == ctx.a.kind]
    return [_pub(i) for i in sorted(it, key=lambda i: (i["kind"], i["name"]))]


def cmd_cache_rm(ctx):
    need_root(ctx)
    sel = [cache_find(ctx, n) for n in ctx.a.names]
    pinned = [i["name"] for i in sel if i["pinned"]]
    if pinned and not ctx.a.force:
        raise CliError(E_REFUSED, f"pinned: {', '.join(pinned)}", "lmap cache unpin NAME --yes, or add --force")
    return _cache_delete(ctx, sel, "rm")


def cmd_cache_prune(ctx):
    """Delete unpinned areas/maps not used for --older DAYS, and/or all but the --keep N newest."""
    need_root(ctx)
    if ctx.a.older is None and ctx.a.keep is None:
        raise CliError(E_USAGE, "say what to prune: --older DAYS and/or --keep N")
    it = [i for i in cache_items(ctx) if not i["pinned"] and (not ctx.a.kind or i["kind"] == ctx.a.kind)]
    sel = {}
    if ctx.a.older is not None:
        sel.update({i["dir"]: i for i in it if time.time() - i["_t"] > ctx.a.older * 86400})
    if ctx.a.keep is not None:
        sel.update({i["dir"]: i for i in sorted(it, key=lambda i: -i["_t"])[max(0, ctx.a.keep):]})
    return _cache_delete(ctx, list(sel.values()), "prune")


def cmd_cache_pin(ctx, on=True):
    need_root(ctx)
    i = cache_find(ctx, ctx.a.name); p = Path(i["dir"]) / PIN
    res = {"action": "cache.pin" if on else "cache.unpin", "name": i["name"], "kind": i["kind"], "pinned": on}
    if p.is_file() == on:
        return {"ok": True, "unchanged": True, **res}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **res}
    confirm(ctx, f"{'pin' if on else 'unpin'} {i['name']}")
    p.write_text("pinned: lmap cache prune skips this; rm needs --force\n") if on else p.unlink()
    return {"ok": True, **res}


def cmd_cache_unpin(ctx):
    return cmd_cache_pin(ctx, on=False)


# ------------------------------------------------------------------------------------------------ store (tier 2)
def local_sources(ctx):
    p = ctx.root / LOCAL_CAT
    try:
        return tomllib.loads(p.read_text()).get("source", {}) if p.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CliError(E_FAIL, f"{p} is damaged: {e}", "fix or move it; imported files are in local/<id>/")


def _tq(v):
    """A TOML value (str/num/bool/list) - JSON syntax is TOML for these once strings are clean ASCII."""
    if isinstance(v, str):
        return json.dumps(v.encode("utf-8", "replace").decode("utf-8"), ensure_ascii=True)
    if isinstance(v, float) and not math.isfinite(v):
        raise CliError(E_USAGE, "layer extent is not finite (bad CRS?)")
    if isinstance(v, list):
        return "[" + ", ".join(_tq(x) for x in v) + "]"
    return json.dumps(v)


def _local_write(ctx, srcs):
    p = ctx.root / LOCAL_CAT; p.parent.mkdir(parents=True, exist_ok=True)
    txt = "# lmap: layers imported with `lmap import add` (the ONLY copy - `lmap import rm` needs --force)\n"
    for k in sorted(srcs):
        txt += f"\n[source.{_tq(k)}]\n" + "".join(f"{a} = {_tq(v)}\n" for a, v in srcs[k].items() if v is not None)
    tomllib.loads(txt)                                # never write a file the next read rejects
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f_:
        f_.write(txt); f_.flush(); os.fsync(f_.fileno())
    os.replace(tmp, p)


@contextlib.contextmanager
def local_lock(ctx):
    """Read-modify-write of sources.local.toml: one import/rm at a time."""
    (ctx.root / "local").mkdir(parents=True, exist_ok=True)
    with open(ctx.root / "local" / ".lock", "w") as f_:
        fcntl.flock(f_, fcntl.LOCK_EX)
        yield


def irreplaceable(s):
    return bool(s.get("irreplaceable")) or s.get("kind") in ("manual", "imported")


def store_files(ctx, s):
    """The stored files of a catalog source: its out file (+ sidecars) and its dest folder, in the data root."""
    fs = []
    for k in ("out", "dest", "vrt"):
        if s.get(k):
            p = ctx.path(s[k])
            for x in (p, p.with_name(p.name + ".aux.xml"), p.with_name(p.name + ".lmap-sig")):
                if x.exists() and x not in fs:
                    fs.append(x)
    r = ctx.root.resolve()
    tiers = {r / n for n in ("data", "local", "areas", "maps", "apps")} | {r / "data" / "australia"} \
        | {p_ for p_ in (r / "data" / "australia").glob("*") if p_.is_dir()}
    others = [ctx.path(o[k]).resolve() for sid_, o in ctx.sources.items() if o is not s
              for k in ("out", "dest", "vrt") if o.get(k)]
    for x in fs:
        xr = x.resolve()
        if r not in xr.parents or xr == r or xr in tiers:
            raise CliError(E_REFUSED, f"{x} is a shared/top folder or outside the data root: not deleting it")
        if x.is_dir() and any(xr == o_ or xr in o_.parents for o_ in others):
            raise CliError(E_REFUSED, f"{x} also holds another source's files: not deleting it")
    return fs


def _store_rm(ctx, sid, s, action):
    fs = store_files(ctx, s)
    gb = round(sum(du_tree(f) if f.is_dir() else f.stat().st_size for f in fs) / 1e9, 2)
    irr = irreplaceable(s)
    res = {"action": action, "id": sid, "files": [str(f) for f in fs], "gb": gb, "irreplaceable": irr,
           "after": "gone for good: no download can bring it back" if irr else f"re-download: lmap data fetch {sid} --yes"}
    if not fs:
        return {"ok": True, "unchanged": True, **res}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **res}
    if irr and not ctx.a.force:
        raise CliError(E_REFUSED, f"{sid} is the only copy (imported or not downloadable)", "add --force as well")
    confirm(ctx, f"DELETE the stored source {sid} ({gb} GB, {len(fs)} item(s)). {res['after']}", typed=sid)
    for f in fs:
        shutil.rmtree(f) if f.is_dir() and not f.is_symlink() else f.unlink()
    return {"ok": True, **res}


def cmd_data_rm(ctx):
    need_root(ctx)
    s = ctx.sources.get(ctx.a.id)
    if not s:
        raise CliError(E_NOTFOUND, f"unknown source {ctx.a.id}", "lmap source list (imported layers: lmap import rm)")
    return _store_rm(ctx, ctx.a.id, s, "data.rm")


# ------------------------------------------------------------------------------------------------ import
def _ll_box(path, vector):
    """lon/lat box of a layer file (None if it has no usable extent/CRS)."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    try:
        ws = osr.SpatialReference(); ws.ImportFromEPSG(4326); ws.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        boxes = []
        if vector:
            ds = ogr.Open(str(path))
            for i in range(ds.GetLayerCount()):
                ly = ds.GetLayer(i)
                if not ly.GetFeatureCount() or not ly.GetSpatialRef():
                    continue
                e = ly.GetExtent(); sr = ly.GetSpatialRef(); sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                ct = osr.CoordinateTransformation(sr, ws)
                boxes.append(ct.TransformBounds(e[0], e[2], e[1], e[3], 5))
            ds = None
        else:
            ds = gdal.Open(str(path)); gt = ds.GetGeoTransform(); sr = ds.GetSpatialRef()
            if sr:
                sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                x1, y1 = gt[0] + ds.RasterXSize * gt[1], gt[3] + ds.RasterYSize * gt[5]
                boxes.append(osr.CoordinateTransformation(sr, ws).TransformBounds(gt[0], min(y1, gt[3]), x1, max(y1, gt[3]), 5))
            ds = None
    except RuntimeError:
        return None
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes), 6), round(min(b[1] for b in boxes), 6),
            round(max(b[2] for b in boxes), 6), round(max(b[3] for b in boxes), 6)]


def _import_layer(ctx, src, sid, title):
    """One layer file -> local/<sid>/<sid>.gpkg|.tif (+ catalog entry). Returns the result row."""
    ext = src.suffix.lower(); vector = ext in IMPORT_VEC
    if not vector and ext not in IMPORT_RAS:
        raise CliError(E_USAGE, f"{src.name}: not a layer lmap imports",
                       "GeoPackage, GeoTIFF, KML/KMZ, GPX, shapefile, GeoJSON, an .lmapbundle or an ATAK .zip")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", sid):
        raise CliError(E_USAGE, f"bad id {sid!r}", "lower-case letters, digits, - (use --as ID)")
    srcs = local_sources(ctx)
    if sid in srcs or sid in ctx.sources:
        raise CliError(E_REFUSED, f"id {sid} is taken", "choose another with --as ID (or `lmap import rm` the old one)")
    box = _ll_box(src, vector)
    if box is None:
        raise CliError(E_USAGE, f"{src.name}: no features with a known position (no CRS or empty)")
    rel = f"local/{sid}/{sid}{'.gpkg' if vector else '.tif'}"
    row = {"id": sid, "file": rel, "type": "vector" if vector else "raster", "bbox": box, "from": src.name}
    if ctx.a.dry_run:
        return row
    out = ctx.root / rel; out.parent.mkdir(parents=True, exist_ok=True)
    part = ctx.scratch() / f"import-{sid}.{os.getpid()}.part{out.suffix}"; part.unlink(missing_ok=True)
    try:
        if vector:
            run(["ogr2ogr", "-f", "GPKG", "-t_srs", "EPSG:4326", "-skipfailures", part, src.resolve()], ctx)
        else:
            run(["gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", src.resolve(), part], ctx)
        if not (part.is_file() and (vector or gdal_ok(part))):
            raise CliError(E_FAIL, f"{src.name}: conversion produced no readable file")
        install_file(part, out)
    finally:
        part.unlink(missing_ok=True)
    with local_lock(ctx):
        srcs = local_sources(ctx)
        if sid in srcs:
            out.unlink(missing_ok=True)
            raise CliError(E_REFUSED, f"id {sid} was taken meanwhile", "choose another with --as ID")
        _local_add(ctx, srcs, sid, {"title": title or src.stem, "kind": "imported", "type": row["type"], "out": rel, "bbox": box,
                 "from": src.name, "imported": time.strftime("%Y-%m-%d %H:%M"), "irreplaceable": True,
                 "licence": "imported by the user: check the original's terms before sharing"})
    return row


def _local_add(ctx, srcs, sid, entry):
    srcs[sid] = entry; _local_write(ctx, srcs)


def _import_bundle(ctx, src):
    import tarfile
    with tarfile.open(src) as t:
        try:
            man = json.loads(t.extractfile("manifest.json").read())
        except (KeyError, AttributeError, ValueError):
            raise CliError(E_USAGE, f"{src.name}: not an lmap bundle (no manifest.json)")
        if man.get("kind") == "local":
            return _import_bundle_layer(ctx, src, t, man)
        rel = Path(man.get("rel", ""))
        kind = man.get("kind")
        if kind not in ("areas", "maps") or len(rel.parts) != 3 or any(p in ("", ".", "..") or "/" in p for p in rel.parts):
            raise CliError(E_USAGE, f"{src.name}: bad bundle manifest")
        name = getattr(ctx.a, "as_", None) or rel.parts[2]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", name) or name in (".", ".."):
            raise CliError(E_USAGE, f"bad name {name!r}", "letters, digits, - _ . (use --as NAME)")
        d = ctx.root / kind / rel.parts[0] / rel.parts[1] / name
        row = {"bundle": src.name, "kind": kind, "name": name, "dir": str(d), "files": len(man.get("files", []))}
        if d.resolve().parent != (ctx.root / kind / rel.parts[0] / rel.parts[1]).resolve():
            raise CliError(E_REFUSED, f"{name}: would land outside {kind}/")
        if d.exists():
            raise CliError(E_REFUSED, f"{kind}/{name} exists already", "import --as NEWNAME, or `lmap cache rm` it first")
        if ctx.a.dry_run:
            return row
        stage = d.with_name(f".{name}.import.{os.getpid()}")
        try:
            members = [m for m in t.getmembers() if m.name.startswith("files/") and m.isfile()]
            for m in members:
                m.name = m.name[len("files/"):]
            t.extractall(stage, members=members, filter="data")
            if not (stage / "area.json").is_file():
                raise CliError(E_FAIL, f"{src.name}: bundle has no area.json")
            if name != rel.parts[2]:                    # renamed: the project file follows the name
                aj = json.loads((stage / "area.json").read_text()); aj["name"] = name
                (stage / "area.json").write_text(json.dumps(aj, indent=1))
                q = stage / f"{rel.parts[2]}.qgz"
                if q.is_file():
                    q.rename(stage / f"{name}.qgz")
            if man.get("pinned"):                       # pinned at the sender: pinned here too
                (stage / PIN).touch(); row["pinned"] = True
            if d.exists():
                raise CliError(E_REFUSED, f"{kind}/{name} appeared meanwhile")
            os.rename(stage, d)
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    return row


def _import_bundle_layer(ctx, src, t, man):
    """A bundle of one imported layer (`lmap export bundle local/ID`): imported again under its id (or --as)."""
    fl = man.get("files")
    fn = fl[0] if isinstance(fl, list) and len(fl) == 1 and isinstance(fl[0], str) else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(gpkg|tif)", fn):
        raise CliError(E_USAGE, f"{src.name}: bad bundle manifest")
    m = t.getmember(f"files/{fn}") if f"files/{fn}" in t.getnames() else None
    if m is None or not m.isfile():
        raise CliError(E_FAIL, f"{src.name}: bundle has no {fn}")
    sid = getattr(ctx.a, "as_", None) or str(man.get("id", ""))
    if ctx.a.dry_run:                                 # no unpacking: id checks + the manifest only
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", sid):
            raise CliError(E_USAGE, f"bad id {sid!r}", "lower-case letters, digits, - (use --as ID)")
        if sid in _local_or_empty(ctx) or sid in ctx.sources:
            raise CliError(E_REFUSED, f"id {sid} is taken", "choose another with --as ID")
        return {"id": sid, "file": f"local/{sid}/{sid}{Path(fn).suffix}", "from": f"{src.name}:{fn}"}
    tmp = Path(tempfile.mkdtemp(dir=ctx.scratch()))
    try:
        f = tmp / fn
        with t.extractfile(m) as r_, open(f, "wb") as w_:
            shutil.copyfileobj(r_, w_)
        row = _import_layer(ctx, f, sid, man.get("title"))
        row["from"] = f"{src.name}:{fn}"
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _import_atak(ctx, src, base):
    """ATAK data package: every KML/KMZ/GPX/GeoTIFF inside becomes its own imported layer <base>-<n>."""
    import zipfile
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and Path(n).suffix.lower() in (".kml", ".kmz", ".gpx", ".tif", ".tiff")]
        if not names:
            raise CliError(E_USAGE, f"{src.name}: no KML/KMZ/GPX/GeoTIFF inside (ATAK data package?)")
        ids = [f"{base}-{i}" if len(names) > 1 else base for i in range(1, len(names) + 1)]
        taken = [x for x in ids if x in local_sources(ctx) or x in ctx.sources]
        if taken:
            raise CliError(E_REFUSED, f"id(s) taken: {', '.join(taken)}", "choose another base with --as ID")
        big = sum(z.getinfo(n).file_size for n in names)
        if big > free_bytes(ctx.scratch()):
            raise CliError(E_FAIL, f"{src.name}: {big / 1e9:.1f} GB unpacked, not enough scratch")
        if ctx.a.dry_run:
            return [{"id": x, "from": f"{src.name}:{n}"} for x, n in zip(ids, sorted(names))]
        tmp = Path(tempfile.mkdtemp(dir=ctx.scratch()))
        try:
            rows = []
            for i, n in enumerate(sorted(names), 1):
                f = tmp / f"{i}{Path(n).suffix.lower()}"
                with z.open(n) as r_, open(f, "wb") as w_:
                    shutil.copyfileobj(r_, w_)
                rows.append(_import_layer(ctx, f, f"{base}-{i}" if len(names) > 1 else base, Path(n).stem))
                rows[-1]["from"] = f"{src.name}:{n}"
            return rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def cmd_import_add(ctx):
    need_root(ctx)
    src = Path(ctx.a.path).expanduser()
    if not src.is_file():
        raise CliError(E_NOTFOUND, f"{src} not found")
    base = ctx.a.as_ or re.sub(r"[^a-z0-9]+", "-", src.stem.lower()).strip("-")[:50] or "layer"
    if src.name.endswith(BUNDLE_EXT):
        if not ctx.a.dry_run:
            confirm(ctx, f"import bundle {src.name}")
        rows = [_import_bundle(ctx, src)]
    elif src.suffix.lower() == ".zip":
        if not ctx.a.dry_run:
            confirm(ctx, f"import ATAK package {src.name} into the store")
        rows = _import_atak(ctx, src, base)
    else:
        if not ctx.a.dry_run:
            confirm(ctx, f"import {src.name} into the store as {base}")
        rows = [_import_layer(ctx, src, base, ctx.a.title)]
    return {"ok": True, **({"dry_run": True} if ctx.a.dry_run else {}), "action": "import.add", "imported": rows,
            "note": "areas/maps made from now on include imported layers they overlap (group 'Imported')"}


def cmd_import_list(ctx):
    need_root(ctx)
    return [{"id": k, "title": v.get("title"), "type": v.get("type"), "file": v.get("out"), "bbox": v.get("bbox"),
             "from": v.get("from"), "imported": v.get("imported"),
             "present": (ctx.root / v.get("out", "")).is_file()} for k, v in sorted(local_sources(ctx).items())]


def cmd_import_rm(ctx):
    need_root(ctx)
    srcs = local_sources(ctx); s = srcs.get(ctx.a.id)
    if not s:
        raise CliError(E_NOTFOUND, f"no imported layer {ctx.a.id}", "lmap import list")
    if Path(s.get("out", "")).parent != Path("local") / ctx.a.id:
        raise CliError(E_REFUSED, f"{ctx.a.id}: its file is not in local/{ctx.a.id}/ (catalog edited?): not deleting",
                       f"fix {LOCAL_CAT} or delete by hand")
    s = {**s, "dest": f"local/{ctx.a.id}"}
    r = _store_rm(ctx, ctx.a.id, s, "import.rm")
    if r.get("ok") and not r.get("dry_run") and not r.get("unchanged"):
        with local_lock(ctx):
            srcs = local_sources(ctx); srcs.pop(ctx.a.id, None); _local_write(ctx, srcs)
    return r


def import_clip(ctx, d, bbox, crs):
    """Clip every imported layer overlapping bbox into d/imp-<id>.gpkg|.tif (area CRS). Kept while the source is
    unchanged (mtime); a removed import's clip is dropped. Returns {id: file}."""
    try:
        srcs = local_sources(ctx) if (ctx.root / LOCAL_CAT).is_file() else {}
    except CliError as e:
        ctx.log(f"imported layers skipped: {e}"); return {}
    got, want = {}, set()
    for sid, s in srcs.items():
        b, f = s.get("bbox"), ctx.root / s.get("out", "")
        if not (b and f.is_file() and overlaps(b, bbox)):
            continue
        vec = s.get("type") == "vector"; out = d / f"imp-{sid}{'.gpkg' if vec else '.tif'}"; want.add(out.name)
        if out.is_file() and out.stat().st_mtime >= f.stat().st_mtime:
            got[sid] = out.name; continue
        part = ctx.scratch() / f"imp-{d.name}-{sid}.{os.getpid()}.part{out.suffix}"; part.unlink(missing_ok=True)
        try:
            if vec:
                run(["ogr2ogr", "-f", "GPKG", "-t_srs", crs, "-spat", *bbox, "-spat_srs", "EPSG:4326", part, f], ctx)
            else:
                run(["gdalwarp", "-q", "-t_srs", crs, "-te", *bbox, "-te_srs", "EPSG:4326", "-of", "COG",
                     "-co", "COMPRESS=DEFLATE", f, part], ctx)
            if part.is_file():
                install_file(part, out); got[sid] = out.name
        except CliError as e:
            ctx.log(f"imported layer {sid}: not clipped ({e})")
        finally:
            part.unlink(missing_ok=True)
    for x in list(d.glob("imp-*.gpkg")) + list(d.glob("imp-*.tif")):
        sid = x.stem[4:]
        if x.name not in want and (sid not in srcs or not overlaps(srcs[sid].get("bbox") or [0, 0, 0, 0], bbox)):
            x.unlink(missing_ok=True)                 # removed import (a missing source file, e.g. network-share down: kept)
    return got


# ------------------------------------------------------------------------------------------------ bundle export
def cmd_export_bundle(ctx):
    """One area/map as a single file (.lmapbundle: tar of its folder + manifest) for another lmap: `lmap import add`."""
    import io, tarfile
    need_root(ctx)
    to = Path(ctx.a.to).expanduser()
    lid = ctx.a.name[len("local/"):] if ctx.a.name.startswith("local/") else None
    if lid is None and "/" not in ctx.a.name:
        try:
            cache_find(ctx, ctx.a.name)
        except CliError as e:
            if e.code != E_NOTFOUND or ctx.a.name not in _local_or_empty(ctx):
                raise
            lid = ctx.a.name                          # not an area/map but an imported layer
    if lid is not None:                               # one imported layer: file + its title, for `lmap import add`
        s = _local_or_empty(ctx).get(lid)
        if not s or not (ctx.root / s.get("out", "")).is_file():
            raise CliError(E_NOTFOUND, f"no imported layer {lid}", "lmap import list")
        if Path(s["out"]).parent != Path("local") / lid:
            raise CliError(E_REFUSED, f"{lid}: its file is not in local/{lid}/ (catalog edited?): not exporting it")
        files = [ctx.root / s["out"]]; out = to / f"{lid}{BUNDLE_EXT}"
        man = {"lmap_bundle": 1, "kind": "local", "id": lid, "title": s.get("title"),
               "made": time.strftime("%Y-%m-%d %H:%M"), "files": [files[0].name]}
        res = {"action": "export.bundle", "name": lid, "kind": "local", "file": str(out), "files": 1,
               "gb": round(files[0].stat().st_size / 1e9, 2)}
    else:
        i = cache_find(ctx, ctx.a.name); d = Path(i["dir"])
        rel = d.relative_to(ctx.root / i["kind"])
        out = to / f"{d.name}{BUNDLE_EXT}"
        files = sorted(f for f in d.iterdir() if f.is_file() and ".part" not in f.name and f.name != PIN)
        man = {"lmap_bundle": 1, "kind": i["kind"], "rel": str(rel), "made": time.strftime("%Y-%m-%d %H:%M"),
               "files": [f.name for f in files], "pinned": i["pinned"]}
        res = {"action": "export.bundle", "name": d.name, "kind": i["kind"], "file": str(out), "files": len(files),
               "gb": round(sum(f.stat().st_size for f in files) / 1e9, 2), "pinned": i["pinned"]}
    if not to.is_dir():
        raise CliError(E_NOTFOUND, f"--to {to}: no such folder")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, **res}
    if out.exists() and not ctx.a.yes:
        raise CliError(E_REFUSED, f"{out} exists", "add --yes to replace it")
    confirm(ctx, f"write {out} ({res['gb']} GB)")
    part = out.with_name(out.name + f".{os.getpid()}.part")
    try:
        with tarfile.open(part, "w", dereference=True) as t:
            b = json.dumps(man, indent=1).encode(); ti = tarfile.TarInfo("manifest.json"); ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
            for f in files:
                t.add(f, arcname=f"files/{f.name}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return {"ok": True, **res}


def _local_or_empty(ctx):
    return local_sources(ctx) if (ctx.root / LOCAL_CAT).is_file() else {}
