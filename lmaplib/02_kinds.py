# lmap: kinds. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- kinds
def src_boxes(s):
    """A source's lon/lat boxes: bbox = [w,s,e,n] -> [bbox]; bboxes = [[w,s,e,n], ...] -> that list; neither -> [].
    A multi-box source covers the UNION of its boxes (tiles = union of each box's tiles, vector = features in any)."""
    if s.get("bbox"):
        return [list(s["bbox"])]
    return [list(b) for b in s.get("bboxes") or []]


def src_extent(s):
    """Bounding extent [w,s,e,n] of all the source's boxes (None if it has none)."""
    b = src_boxes(s)
    if not b:
        return None
    return [min(x[0] for x in b), min(x[1] for x in b), max(x[2] for x in b), max(x[3] for x in b)]


WMS_CLIP_MAX_DEG2 = 4          # wms-clip with bboxes: one warp over their extent, refused beyond this many sq degrees


def arc_geo(s):
    """ArcGIS spatial filter for the source's boxes. One bbox: the envelope (as always). bboxes: ONE polygon query
    whose rings are the boxes - the server returns each feature once even where boxes overlap or a feature
    crosses two (deduped by object id server-side; OID keyset paging and counts stay exact)."""
    b = src_boxes(s)
    if not b:
        raise CliError(E_USAGE, f"{s.get('id', '?')}: arcgis source has no bbox / bboxes")
    if len(b) == 1:
        w, so, e, n = b[0]
        return {"geometry": f"{w},{so},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects"}
    rings = [[[w, so], [w, n], [e, n], [e, so], [w, so]] for w, so, e, n in b]      # clockwise = outer rings
    return {"geometry": json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}}, separators=(",", ":")),
            "geometryType": "esriGeometryPolygon", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}


def boxes_geojson(boxes):
    """The boxes as one GeoJSON MultiPolygon FeatureCollection (EPSG:4326): a gdalwarp -cutline for multi-box clips."""
    polys = [[[[w, so], [e, so], [e, n], [w, n], [w, so]]] for w, so, e, n in boxes]
    return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                       "geometry": {"type": "MultiPolygon", "coordinates": polys}}]})


def boxes_wkt(boxes):
    return "MULTIPOLYGON(" + ",".join(f"(({w} {so},{e} {so},{e} {n},{w} {n},{w} {so}))" for w, so, e, n in boxes) + ")"


def cop_cells(s):
    """Copernicus 1-degree cells as (degrees SOUTH of the cell's south edge, degrees EAST of its west edge), deduped:
    tiles = [[lat, lon], ...] explicitly, else derived from bbox/bboxes (every cell a box touches)."""
    import math
    if s.get("tiles"):
        return list(dict.fromkeys((int(la), int(lo)) for la, lo in s["tiles"]))
    cells = []
    for w, so, e, n in src_boxes(s):
        for la in range(math.floor(so), math.ceil(n)):
            for lo in range(math.floor(w), math.ceil(e)):
                cells.append((-la, lo))
    return list(dict.fromkeys(cells))


def cop_tiles(s):
    pre = "COG_30" if s.get("res", 90) == 90 else "COG_10"
    if s.get("tiles") or ("lat" not in s and src_boxes(s)):
        for la, lo in cop_cells(s):
            yield f"Copernicus_DSM_{pre}_S{la:02d}_00_E{lo:03d}_00_DEM"
        return
    for la in range(s["lat"][0], s["lat"][1] + 1):
        for lo in range(s["lon"][0], s["lon"][1] + 1):
            yield f"Copernicus_DSM_{pre}_S{la:02d}_00_E{lo:03d}_00_DEM"


def cop_bucket(s):
    if s.get("base"):                                       # a mirror of the bucket layout (<base>/<tile>/<tile>.tif)
        u = urllib.parse.urlsplit(s["base"])                # tiles carry no hash: plain http only to this machine
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise CliError(E_USAGE, f"{s.get('id', 'source')}: base must be https:// (got {mask_url(s['base'])})")
        return s["base"].rstrip("/")
    return "https://copernicus-dem-%s.s3.amazonaws.com" % ("90m" if s.get("res", 90) == 90 else "30m")


COP_REPROBE_DAYS = 180


def cop_missing(miss, fresh_only=False):
    """Tiles the bucket answered 404 for. Lines are 'tile [YYYY-MM-DD]' (older files: tile only, treated as
    old). fresh_only: just those listed within COP_REPROBE_DAYS (the rest get asked again)."""
    out = set()
    try:
        lines = miss.read_text().splitlines()
    except OSError:
        return out
    cut = time.time() - COP_REPROBE_DAYS * 86400
    for ln in lines:
        p = ln.split()
        if not p:
            continue
        if fresh_only:
            try:
                if len(p) < 2 or calendar.timegm(time.strptime(p[1], "%Y-%m-%d")) < cut:
                    continue
            except ValueError:
                continue
        out.add(p[0])
    return out


def cop_status(ctx, s):
    d = ctx.path(s["dest"]); miss = d / "_missing.txt"
    tiles = list(cop_tiles(s))
    have = sum((d / f"{t}.tif").is_file() for t in tiles)
    ocean = len(cop_missing(miss) - {t for t in tiles if (d / f"{t}.tif").is_file()})   # re-probed + got: not a hole
    return {"items_total": len(tiles), "items_have": have, "items_no_data": ocean,
            "items_todo": len(tiles) - have - ocean}


def cop_fetch(ctx, s, plan):
    """Copernicus DEM tiles from the public S3 bucket. Parallel; the Tuner adds connections while they raise
    throughput (host_slot still caps the server). Ocean tiles 404 -> listed in _missing.txt with the date; --refresh
    asks again for ones listed over COP_REPROBE_DAYS ago (a transient 404 must not be a hole forever)."""
    import concurrent.futures as cf
    d = ctx.path(s["dest"]); miss = d / "_missing.txt"
    known = cop_missing(miss, fresh_only=bool(getattr(ctx.a, "refresh", False)))
    todo = [t for t in cop_tiles(s) if not (d / f"{t}.tif").is_file() and t not in known]
    if plan:
        return {"would_fetch": len(todo), "from": cop_bucket(s), "into": str(d)}
    d.mkdir(parents=True, exist_ok=True)
    random.shuffle(todo)                                    # random order: spreads load across the server's cache tiles
    got = gone = 0; lk = threading.Lock()
    prog = Progress(s, len(todo), 0)
    cap = s.get("max_conns", host_cap(urllib.parse.urlsplit(cop_bucket(s)).hostname)) if ctx.jobs == 3 else ctx.jobs
    # far bucket (Frankfurt): +1 a minute from 2 would take hours to reach the useful range, so start at a quarter
    # and double while it helps (the class's own path, min_units=1)
    meter, tun = Meter(), Tuner(cap, window=60, min_units=1 if cap > 16 else 0)
    if cap > 16:
        tun.c = max(2, cap // 4)
    # download threads only download: the full decode check runs on its own pool (sn), so the Tuner times bytes,
    # not CPU. A tile is fetched as <t>.tif.unverified and renamed only after the check passes; a complete one left
    # by a stopped run is checked, not fetched again (tiles are static; --refresh only re-asks no-data tiles).
    # A failed check deletes it; round 2 fetches it again.
    sn = max(1, ctx.shrink)
    def stage(t):
        return d / f"{t}.tif.unverified"
    def one(t):
        if stage(t).is_file():
            return t, "reused", None
        try:
            return t, http_get(f"{cop_bucket(s)}/{t}/{t}.tif", stage(t), meter=meter), None
        except (CliError, Aborted) as e:
            return t, None, e
        except Exception as e:
            return t, None, CliError(E_FAIL, f"{type(e).__name__}: {e}")
    def ver(t):
        try:
            if not gdal_ok(stage(t), deep=True):
                stage(t).unlink(missing_ok=True); return t, CliError(E_FAIL, "integrity check failed")
            stage(t).replace(d / f"{t}.tif"); return t, None
        except Aborted as e:                              # stopped mid-check: the leftover is checked next run
            return t, e
        except OSError as e:
            return t, CliError(E_FAIL, f"{type(e).__name__}: {e}")
    failed = []
    for rnd in (1, 2):
        failed = []; it = iter(todo); run_ = set(); chk = set(); i = 0
        with cf.ThreadPoolExecutor(tun.cap) as ex, cf.ThreadPoolExecutor(sn) as vx:
            while True:
                say_stop(ctx)
                while not DRAIN.is_set() and len(run_) < tun.c:
                    t = next(it, None)
                    if t is None:
                        break
                    run_.add(ex.submit(one, t))
                tun.busy(len(run_)); tun.tick(meter.bytes)
                if not run_ and not chk:
                    break
                fin, _ = cf.wait(run_ | chk, timeout=5, return_when=cf.FIRST_COMPLETED)
                for f in fin:
                    if f in chk:
                        chk.discard(f); t, err = f.result()
                        if isinstance(err, Aborted):
                            continue
                        i += 1
                        if err:
                            failed.append(t); prog.tick(False); ctx.log(f"[{i}/{len(todo)}] FAIL {t}: {err.msg}")
                        else:
                            got += 1; prog.tick(True, conns=tun.c); ctx.log(f"[{i}/{len(todo)}] ok {t}  (x{tun.c})")
                        continue
                    run_.discard(f); t, r, err = f.result()
                    if isinstance(err, Aborted):
                        continue
                    if err:
                        i += 1; failed.append(t); prog.tick(False); tun.unit(False)       # server unhappy: halve
                        ctx.log(f"[{i}/{len(todo)}] FAIL {t}: {err.msg}")
                        continue
                    if r != "reused":
                        tun.unit()                         # a reused leftover moved no bytes
                    if r is None:
                        i += 1
                        with lk, open(miss, "a") as fh:
                            fh.write(f"{t} {time.strftime('%Y-%m-%d', time.gmtime())}\n")
                        gone += 1; prog.tick("skip"); ctx.log(f"[{i}/{len(todo)}] no-data {t}  (x{tun.c})")
                    elif not ABORT.is_set():
                        chk.add(vx.submit(ver, t))
        if not failed or DRAIN.is_set():
            break
        ctx.log(f"retrying {len(failed)} failed tile(s)")
        todo = failed
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    res = {"fetched": got, "no_data": gone, "failed": failed}
    if failed:
        res.update(ok=False, hint="re-run the same command; failed tiles are retried")
    return res

def tcc_list(ctx, s, refresh=False):
    d = ctx.path(s["dest"]); idx = d / "_tiles.txt"
    if idx.is_file() and not refresh:
        return idx.read_text().split()
    names, marker = [], ""
    while True:
        u = f"{TCC_BLOB}?restype=container&comp=list&prefix={s['year']}/Tiles/&maxresults=5000"
        if marker:
            u += "&marker=" + marker
        x = http_get(u).decode()
        names += re.findall(r"<Name>([^<]+\.zip)</Name>", x)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if not m:
            break
        marker = m.group(1)
    if not names:
        raise CliError(E_UPSTREAM, f"TCC listing for {s['year']} is empty")
    d.mkdir(parents=True, exist_ok=True)
    idx.write_text("\n".join(names) + "\n")
    return names


def tcc_status(ctx, s):
    d = ctx.path(s["dest"]); idx = d / "_tiles.txt"
    if not idx.is_file():
        return {"items_total": None, "items_have": 0, "items_todo": None, "note": "no index yet; fetch builds it"}
    names = idx.read_text().split()
    have = sum((d / (Path(n).stem + ".tif")).is_file() for n in names)
    return {"items_total": len(names), "items_have": have, "items_todo": len(names) - have}


def tcc_fetch(ctx, s, plan):
    need_tool("gdal_translate", "unzip")
    d = ctx.path(s["dest"])
    names = tcc_list(ctx, s, refresh=ctx.a.refresh)
    todo = [n for n in names if not (d / (Path(n).stem + ".tif")).is_file()]
    if plan:
        return {"would_fetch": len(todo), "approx_download_gb": round(len(todo) * 0.233, 1),
                "out_res_m": s["out_res"], "into": str(d)}
    tmp = ctx.scratch()
    random.shuffle(todo)                                    # random order: spreads load across the server's cache tiles
    got = 0
    prog = Progress(s, len(names), len(names) - len(todo))
    left = []
    for rnd in (1, 2):
        failed, left = [], []
        got += tcc_pass(ctx, s, d, tmp, todo, failed, prog, left)
        if (not failed) or DRAIN.is_set() or left:
            break
        ctx.log(f"retrying {len(failed)} failed tile(s)")
        todo = failed
    res = {"fetched": got, "failed": failed}
    if left and not DRAIN.is_set():
        res.update(ok=False, not_started=len(left),
                   error=f"scratch dir {tmp} kept below 600MB free; stopped with {len(left)} tiles not started",
                   hint="free space or pass --tmp elsewhere, then re-run")
    elif failed:
        res.update(ok=False, hint="re-run the same command; failed tiles are retried")
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if res.get("ok") is False else "done"))
    return res


def tcc_pass(ctx, s, d, tmp, todo, failed, prog, left):
    """Downloads and shrinks run in parallel pools.
    - shrink pool: ctx.shrink gdal_translate processes (CPU bound).
    - download pool: up to ctx.jobs, but the Tuner only adds a download slot if it measurably
      raises throughput, and none start while a full batch of zips is already waiting to be shrunk
      (then the CPU is the bottleneck and more downloads would only fill scratch)."""
    import concurrent.futures as cf
    meter, tun = Meter(), Tuner(ctx.jobs)
    sn = ctx.shrink
    def dl(n):
        z = tmp / f"{s['id']}-{Path(n).stem}.zip"
        try:
            http_get(f"{TCC_BLOB}/{n}", z, timeout=300, meter=meter, check=zip_ok)
            return n, z, None
        except Aborted as e:
            return n, z, e
        except CliError as e:
            Path(str(z) + ".part").unlink(missing_ok=True)
            return n, z, e
        except Exception as e:                                  # never let a worker kill the run
            Path(str(z) + ".part").unlink(missing_ok=True)
            return n, z, CliError(E_FAIL, f"{type(e).__name__}: {e}")
    def shrink(n, z):
        out = d / f"{Path(n).stem}.tif"; part = Path(str(out) + ".part")
        try:
            inner = [l for l in run(["unzip", "-Z1", z], ctx).split() if l.lower().endswith(".tif")]
            if not inner:
                raise CliError(E_UPSTREAM, f"{n}: no .tif inside")
            run(["gdal_translate", "-q", "--config", "GDAL_CACHEMAX", "256", "-tr", s["out_res"], s["out_res"],
                 "-r", "average", "-of", "COG", *photo_co(ctx),
                 photo_src(ctx, f"/vsizip/{z}/{inner[0]}", part), part], ctx)
            if not gdal_ok(part):
                raise CliError(E_FAIL, f"{n}: resampled output failed gdalinfo")
            part.rename(out)
            return n, None
        except (CliError, Aborted, OSError) as e:
            for x in d.glob(part.name + "*"):
                x.unlink(missing_ok=True)
            if isinstance(e, OSError):
                e = CliError(E_FAIL, f"{n}: {e}")
            return n, e
        finally:
            z.unlink(missing_ok=True)
            for x in d.glob(part.name + ".grey3-*"):        # grey source VRT (photo_src)
                x.unlink(missing_ok=True)
    got, total, it = 0, len(todo), iter(todo)
    downloading, shrinking, ready = set(), set(), []
    exhausted = False
    pending = None                                          # tile pulled from `it` but not started
    with cf.ThreadPoolExecutor(ctx.jobs) as dx, cf.ThreadPoolExecutor(sn) as sx:
        while True:
            while ready and len(shrinking) < sn and not ABORT.is_set():
                shrinking.add(sx.submit(shrink, *ready.pop(0)))
            say_stop(ctx)
            low = free_bytes(tmp) <= 600e6
            while (not DRAIN.is_set() and not exhausted and len(downloading) < tun.c
                   and len(ready) < sn and not low):
                n = pending or next(it, None); pending = None
                if n is None:
                    exhausted = True; break
                downloading.add(dx.submit(dl, n))
            tun.busy(len(downloading) if not exhausted else tun.c)
            if ABORT.is_set() and not downloading and not shrinking:
                break
            if not downloading and not shrinking and not ready:
                if low and not exhausted and not DRAIN.is_set():
                    # nothing in flight and no room: report, don't pretend we finished
                    left.extend(([pending] if pending else []) + list(it))
                break
            fin, _ = cf.wait(downloading | shrinking, timeout=5, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                if f in downloading:
                    downloading.discard(f); n, z, err = f.result()
                    if isinstance(err, Aborted) or ABORT.is_set():
                        z.unlink(missing_ok=True)
                    elif err:
                        failed.append(n); prog.tick(False); ctx.log(f"FAIL {Path(n).stem}: {err.msg}")
                    else:
                        ready.append((n, z))
                else:
                    shrinking.discard(f); n, err = f.result()
                    if isinstance(err, Aborted) or ABORT.is_set():
                        continue
                    if err:
                        failed.append(n); prog.tick(False); ctx.log(f"FAIL {Path(n).stem}: {err.msg}")
                    else:
                        got += 1; prog.tick(True)
                        ctx.log(f"[{got}/{total}] ok {Path(n).stem}  (dl x{tun.c}, shrink x{sn})")
            tun.tick(meter.bytes)
            prog.tick(None, dl_slots=tun.c, dl_active=len(downloading), shrink_active=len(shrinking),
                      waiting=len(ready), dl_mbps=round(tun.rate * 8 / 1e6, 1) if tun.rate else None,
                      jobs_cap=ctx.jobs, shrink_cap=sn)
        if ABORT.is_set():
            for n, z in ready:
                z.unlink(missing_ok=True)
    return got


def net_ok(host, port=443, timeout=4):
    """True if host resolves and accepts a TCP connect (cheap internet check before a live read)."""
    import socket
    try:
        socket.create_connection((host, port), timeout).close(); return True
    except OSError:
        return False


def url_list(s):
    if "urls" in s:
        return [(u, Path(u.split("?")[0]).name) for u in s["urls"]]
    return [(s["base"] + f, f) for f in s["files"]]


def url_status(ctx, s):
    d = ctx.path(s["dest"]); fs = url_list(s)
    have = sum((d / n).is_file() for _, n in fs)
    return {"items_total": len(fs), "items_have": have, "items_todo": len(fs) - have}


def url_fetch(ctx, s, plan):
    d = ctx.path(s["dest"]); fs = url_list(s)
    todo = [(u, n) for u, n in fs if ctx.a.refresh or not (d / n).is_file()]
    if plan:
        return {"would_fetch": [n for _, n in todo], "into": str(d)}
    d.mkdir(parents=True, exist_ok=True)
    import concurrent.futures as cf
    # parallel + tuned by default, like the tile/S3 fetchers: files at once up to the server's max (SERVERS /
    # config [servers]; max_conns in sources.toml caps it; --jobs overrides), Tuner hill-climbs on bytes/s and
    # halves on a failure; host_slot keeps the per-server total across lmap processes. A lone big file
    # (>256 MB) is split into parallel Range chunks instead (http_get_parallel).
    host = urllib.parse.urlsplit(fs[0][0]).hostname if fs else ""
    prof = server_profile(host)
    cap = ctx.jobs if ctx.jobs != 3 else min(prof["max"], int(s.get("max_conns", prof["max"])))
    if len(todo) == 1:
        u, n = todo[0]
        chk = file_check(n)
        try:
            r = http_get_parallel(ctx, u, d / n, cap=cap, check=chk)
        except CliError as e:
            ctx.log(f"FAIL {n}: {e.msg}"); r = None
        if r is False:                                # stopped: the caller reports drained/aborted
            return {"fetched": 0, "failed": []}
        if r is None:
            ctx.log(f"FAIL {n}")
            return {"fetched": 0, "failed": [n], "ok": False, "hint": "re-run the same command to retry"}
        return {"fetched": 1, "failed": []}
    meter, tun = Meter(), Tuner(max(1, cap), min_units=2); prog = Progress(s, len(fs), len(fs) - len(todo))
    got, failed, sn = 0, [], max(1, ctx.shrink)
    # download threads only download: the integrity check (unzip -t, osmium, full decode) runs on its own pool, so
    # the Tuner times bytes, not CPU. A file is fetched as <name>.unverified and renamed only after its check
    # passes (--refresh: the old copy stays until then); a complete .unverified left by a dead run is re-checked,
    # not re-downloaded. A failed check deletes it and fetches once more.
    def stage(n):
        return d / (n + ".unverified")
    for _, n in todo:                                # --refresh wants the server's copy now: an old run's leftover
        if ctx.a.refresh:                            # (staged or half-fetched) could be the content being replaced
            for x in (stage(n), Path(str(stage(n)) + ".part")):
                x.unlink(missing_ok=True)
        elif stage(n).is_file():
            ctx.log(f"{n}: checking the download a stopped run left")
    def one(u, n):
        st = stage(n)
        if st.is_file():
            return n, "reused"
        try:
            return n, (None if http_get(u, st, timeout=300, meter=meter) is not None else "404")
        except Aborted:
            return n, "drained"
        except CliError as e:
            return n, e.msg
    def ver(n):
        st, chk = stage(n), file_check(n)
        try:
            if chk and not chk(st):
                st.unlink(missing_ok=True); return n, "integrity check failed"
            st.replace(d / n); return n, None
        except Aborted:                                   # stopped mid-check: the leftover is checked next run
            return n, "drained"
        except OSError as e:
            return n, f"{type(e).__name__}: {e}"
    it, running, checking, tries, again, i = iter(todo), set(), set(), {}, [], 0
    with cf.ThreadPoolExecutor(max(1, cap)) as ex, cf.ThreadPoolExecutor(sn) as vx:
        while True:
            say_stop(ctx)
            while not DRAIN.is_set() and not ABORT.is_set() and len(running) < tun.c:
                nx = again.pop(0) if again else next(it, None)
                if nx is None:
                    break
                running.add(ex.submit(one, *nx))
            tun.busy(len(running)); tun.tick(meter.bytes)
            if not running and not checking:
                break
            fin, _ = cf.wait(running | checking, timeout=5, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                if f in running:
                    running.discard(f); n, err = f.result()
                    if err == "drained":
                        continue
                    if err == "reused":
                        err = None                        # no bytes moved: says nothing to the Tuner
                    else:
                        tun.unit(err in (None, "404"))
                    if not err and not ABORT.is_set():
                        checking.add(vx.submit(ver, n)); continue
                    if not err:
                        continue                          # aborted after the download: the next run checks it
                else:
                    checking.discard(f); n, err = f.result()
                    if err == "drained":
                        continue
                    if err == "integrity check failed" and tries.get(n, 0) < 1 and not DRAIN.is_set() and not ABORT.is_set():
                        tries[n] = 1; ctx.log(f"{n}: integrity check failed; fetching once more")
                        again.append(next((u, m) for u, m in todo if m == n)); continue
                i += 1; prog.tick(not err, conns=tun.c, mbps=round(tun.rate * 8 / 1e6, 1) if tun.rate else None)
                if err:
                    failed.append(n); ctx.log(f"[{i}/{len(todo)}] FAIL {n}: {err}")
                else:
                    got += 1; ctx.log(f"[{i}/{len(todo)}] ok {n} ({tun.c} at once)")
    for _, n in again:                               # stopped before its second fetch
        failed.append(n); ctx.log(f"FAIL {n}: integrity check failed (stopped before fetching again)")
    prog.write(final="stopped" if DRAIN.is_set() or ABORT.is_set() else "failed" if failed else "done")
    res = {"fetched": got, "failed": failed, "conns_end": tun.c}
    if failed:
        res.update(ok=False, hint="re-run the same command to retry")
    return res


def file_status(ctx, s):
    have = ctx.path(s["dest"]).is_file()
    return {"items_total": 1, "items_have": int(have), "items_todo": int(not have)}


def wms_fetch(ctx, s, plan):
    need_tool("gdalwarp")
    out = ctx.path(s["dest"])
    if out.is_file() and not ctx.a.refresh:
        return {"unchanged": True}
    if s.get("bbox"):
        w, so, e, n = s["bbox"]
    else:                                        # bboxes: one warp over their extent - only while that's small
        ext = src_extent(s)
        if not ext:
            raise CliError(E_USAGE, f"{s['id']}: no bbox / bboxes")
        if (ext[2] - ext[0]) * (ext[3] - ext[1]) > WMS_CLIP_MAX_DEG2:
            raise CliError(E_USAGE, f"{s['id']}: bboxes span {ext} (> {WMS_CLIP_MAX_DEG2} sq deg) for a one-file wms-clip",
                           "use kind = \"wms-tiles\" (tile union) or one source per box")
        w, so, e, n = ext
    if plan:
        return {"would_build": str(out), "from": s["xml"], "bbox": s.get("bbox") or [w, so, e, n]}
    part = Path(str(out) + ".part.tif")
    try:
        xml = ctx.scratch() / f"{s['id']}.clip.{os.getpid()}.xml"     # never a failed request as a blank block
        xml.write_text(wms_xml(ctx, s, r"<ZeroBlockOnServerException>.*?</ZeroBlockOnServerException>"))
        try:
            # bilinear, not gdalwarp's default nearest: res_m rarely equals a server zoom level -> nearest drops /
            # doubles whole pixel rows (jagged roads, lines). No -dstalpha: the output stays in the server's CRS
            # (no rotated edges), and 4-band sources already carry their alpha through.
            run(["gdalwarp", "-q", "-r", "bilinear", "-te_srs", "EPSG:4326", "-te", w, so, e, n, "-tr", s["res_m"], s["res_m"],
                 "-of", "COG", *photo_co(ctx), "-overwrite", *gdal_config(gdal_env({"GDAL_HTTP_USERAGENT": UA})), xml, part], ctx)
        finally:
            xml.unlink(missing_ok=True)
        if not gdal_ok(part, deep=True):
            raise CliError(E_FAIL, "gdalwarp output failed gdalinfo")
        part.rename(out)
    except BaseException:
        part.unlink(missing_ok=True); raise
    return {"built": str(out)}


def fileclip_fetch(ctx, s, plan):
    """Ready-made GeoTIFFs (NASA Black Marble, LRO/LOLA...) -> one finished COG at `dest`. Downloads `urls` into local scratch
    (resumable, size-checked), clips to `bbox` (lon/lat; omit = whole file) at the source's own pixels, writes a
    COG in scratch, decode-checks it, then copies it to `out`. Keys: compress (JPEG|WEBP|DEFLATE), quality,
    predictor (checked against the data type), t_srs (default: keep the source projection), srs (override a
    file's CRS, e.g. Moon files with odd metadata), bbox_srs (CRS of bbox, default EPSG:4326), conns (cap
    connections), keep (true = keep the downloads in scratch for a re-clip)."""
    need_tool("gdalwarp")
    out = ctx.path(s["dest"])                          # the finished file (not `out`: that means "COG from tiles")
    if out.is_file() and not ctx.a.refresh:
        if gdal_ok(out):
            return {"unchanged": True}
        ctx.log(f"{out.name} on a network share doesn't open: rebuilding")
    fs = url_list(s)
    if plan:
        return {"would_fetch": [n for _, n in fs], "would_build": str(out), "bbox": s.get("bbox"),
                **({"bboxes": src_boxes(s)} if s.get("bboxes") and not s.get("bbox") else {}),
                **({"rebuild": True} if out.is_file() else {})}
    sd = ctx.scratch() / f"{s['id']}.files"; sd.mkdir(parents=True, exist_ok=True)
    urls_f = sd / ".urls"                              # url list changed since the last run: old downloads are junk
    if urls_f.is_file() and urls_f.read_text() != "\n".join(u for u, _ in fs):
        ctx.log("url list changed: dropping the old downloads"); shutil.rmtree(sd); sd.mkdir(parents=True)
    urls_f.write_text("\n".join(u for u, _ in fs))
    need = 1.6 * float(s.get("est_gb") or 0) * 1e9 + 2e9 - du(sd)          # download + COG + temp
    if need > 0 and free_bytes(ctx.scratch()) < need:
        raise CliError(E_FAIL, f"scratch too small for {s['id']}: needs ~{need / 1e9:.0f} GB, "
                       f"{room(ctx.scratch())}", f"free space in {ctx.scratch()} or set [paths] tmp =")
    for i, (u, n) in enumerate(fs, 1):
        say_stop(ctx)
        if DRAIN.is_set():
            return {"stopped": "drained", "hint": f"resume: lmap data fetch {s['id']} --yes"}
        if (sd / n).is_file() and gdal_ok(sd / n):    # renamed only after size + check passed: complete
            continue
        ctx.log(f"[{i}/{len(fs)}] {n}")
        r = http_get_parallel(ctx, u, sd / n, cap=s.get("conns"), check=lambda f: gdal_ok(f, deep=True))
        if r is None:
            raise CliError(E_FAIL, f"{n}: 404 at {u}")
        if r is False:
            return {"stopped": "drained", "hint": f"resume: lmap data fetch {s['id']} --yes (finished chunks kept)"}
    srcs = [str(sd / n) for _, n in fs]
    part = ctx.scratch() / f"{s['id']}.{os.getpid()}.part.tif"
    info = raster_info(srcs[0])
    comp = s.get("compress", "DEFLATE").upper()
    if comp in ("JPEG", "WEBP"):                     # a photo: the webp= setting decides, not the catalog
        comp = "WEBP" if ctx.webp else "JPEG"
    if comp in ("JPEG", "WEBP") and info["type"] != "Byte":
        raise CliError(E_USAGE, f"{s['id']}: {comp} needs 8-bit data, the file is {info['type']}",
                       "use compress = \"DEFLATE\" (or ZSTD) for heights / floats")
    if s.get("predictor"):                            # 2 = integers, 3 = floats; a wrong one errors or does nothing
        good = 3 if info["type"].startswith("Float") else 2
        if int(s["predictor"]) != good:
            ctx.log(f"predictor {s['predictor']} doesn't suit {info['type']}: using {good}"); s = {**s, "predictor": good}
    # same layout as every other COG: 512 blocks, average (photos) / bilinear (values) overviews, not nearest
    co = ["-co", f"COMPRESS={comp}", "-co", "BIGTIFF=IF_SAFER", "-co", "NUM_THREADS=ALL_CPUS", "-co", "BLOCKSIZE=512",
          "-co", f"OVERVIEW_RESAMPLING={'AVERAGE' if comp in ('JPEG', 'WEBP') else 'BILINEAR'}"]
    if comp in ("JPEG", "WEBP"):
        co += ["-co", f"QUALITY={photo_q(ctx, s.get('quality', 85)) if comp == 'WEBP' else s.get('quality', 85)}"]
    if s.get("predictor"):
        co += ["-co", f"PREDICTOR={s['predictor']}"]
    if comp == "WEBP" and info["bands"] == 1:         # WEBP needs RGB: grey goes in as 3 equal bands (shows the same)
        srcs = [grey3(ctx, f, part, i) for i, f in enumerate(srcs)]
    boxes = src_boxes(s)
    warp = bool(boxes or s.get("t_srs") or s.get("srs") or len(fs) > 1)
    # whole file, same projection: gdal_translate (keeps scale/offset, e.g. LOLA heights = value * 0.5 m)
    cmd = (["gdalwarp", "-q", "-overwrite", "-of", "COG", "-multi", "-wo", "NUM_THREADS=ALL_CPUS"] if warp
           else ["gdal_translate", "-q", "-of", "COG"]) + co
    if s.get("srs"):
        cmd += ["-s_srs", s["srs"]]
    if s.get("t_srs"):
        cmd += ["-t_srs", s["t_srs"]]
    if s.get("bbox"):
        w, so, e, n = s["bbox"]
        cmd += ["-te_srs", "EPSG:4326" if not s.get("bbox_srs") else s["bbox_srs"], "-te", w, so, e, n]
    elif boxes:                                       # bboxes: their extent, cut to the boxes; the space between
        w, so, e, n = src_extent(s)                   # stays nodata and SPARSE_OK leaves those blocks unwritten
        cut = ctx.scratch() / f"{s['id']}.{os.getpid()}.cut.geojson"; cut.write_text(boxes_geojson(boxes))
        cmd += ["-te_srs", "EPSG:4326", "-te", w, so, e, n, "-cutline", cut, "-co", "SPARSE_OK=TRUE"]
    if warp:                                          # outside the source must not become height/value 0
        if info["nodata"] is not None:
            cmd += ["-dstnodata", info["nodata"]]
        elif info["type"] == "Byte" and info["bands"] in (1, 3):
            cmd += ["-dstalpha"]
        else:
            cmd += ["-dstnodata", "-32768" if info["type"].startswith("Int") else "nan" if info["type"].startswith("Float") else "0"]
    try:
        with BuildSlot(ctx, out.name):                # all-cores build: share the machine with other builds
            ctx.log(f"building {out.name} in local scratch")
            run(cmd + srcs + [part], ctx, env={**os.environ, "GDAL_CACHEMAX": "1024", "CPL_TMPDIR": str(ctx.scratch())},
                watch=lambda: "scratch nearly full" if free_bytes(ctx.scratch()) < 1e9 else None)
        got = raster_info(part)
        if (got["scale"], got["offset"]) != (info["scale"], info["offset"]):   # e.g. LOLA: heights = value x 0.5
            run(["gdal_edit.py", "-scale", info["scale"] or 1, "-offset", info["offset"] or 0, part], ctx)
            if raster_info(part)["scale"] != info["scale"]:
                raise CliError(E_FAIL, f"{out.name}: lost the value scale/offset {info['scale']}/{info['offset']}")
        bad = deep_decode(ctx, part)                  # every block, all cores: a cut-off COG never reaches the network share
        if bad:
            raise CliError(E_FAIL, f"{out.name}: built file doesn't decode: {bad}")
        ctx.log(f"copying {out.name} to {out.parent}")
        install_file(part, out)
    finally:
        for x in part.parent.glob(part.name + "*"):
            x.unlink(missing_ok=True)
    if not s.get("keep"):
        shutil.rmtree(sd, ignore_errors=True)
    return {"built": str(out), "gb": round(out.stat().st_size / 1e9, 3)}


def vzip_fetch(ctx, s, plan):
    """A zipped shapefile (e.g. OSM water polygons) -> one indexed GeoPackage at `dest`. Keys: urls (one .zip),
    shp (file name inside the zip), layer (output table), bbox / bboxes (lon/lat clip; omit = everything).
    The zip is downloaded into scratch (resumable), converted there, then installed; the zip is deleted after."""
    import zipfile
    need_tool("ogr2ogr")
    out = ctx.path(s["dest"])
    if out.is_file() and not ctx.a.refresh:
        return {"unchanged": True}
    (u, n), = url_list(s)
    if plan:
        return {"would_fetch": [n], "would_build": str(out), "bbox": src_boxes(s) or None,
                "note": "converting the world file takes several minutes"}
    sd = ctx.scratch() / s["id"]; sd.mkdir(parents=True, exist_ok=True); z = sd / n
    if not z.is_file():
        say_stop(ctx)
        r = http_get_parallel(ctx, u, z, check=zipfile.is_zipfile)
        if r is None:
            raise CliError(E_FAIL, f"{n}: 404 at {u}")
        if r is False or DRAIN.is_set():
            return {"stopped": "drained", "hint": f"resume: lmap data fetch {s['id']} --yes"}
    for f in ctx.scratch().glob(f"{s['id']}.*.part.gpkg"):    # left by a killed run
        f.unlink(missing_ok=True)
    part = ctx.scratch() / f"{s['id']}.{os.getpid()}.part.gpkg"
    try:
        src = f"/vsizip/{z}/{s['shp']}"
        b = src_extent(s)       # several boxes: one -spat over their union extent (no polygon twice; a bit extra is fine)
        run(["ogr2ogr", "-q", "-f", "GPKG", *(["-spat", *b] if b else []),
             "-nln", s["layer"], "-nlt", "MULTIPOLYGON", "-lco", "GEOMETRY_NAME=geom", "-lco", "SPATIAL_INDEX=YES", part, src], ctx)
        if not readable(part):
            raise CliError(E_FAIL, f"{out.name}: converted file doesn't open")
        out.parent.mkdir(parents=True, exist_ok=True)
        install_file(part, out)
    finally:
        part.unlink(missing_ok=True)
    z.unlink(missing_ok=True)
    return {"built": str(out), "mb": round(out.stat().st_size / 1e6, 1)}


def osm_fetch(ctx, s, plan):
    """.osm.pbf -> GeoPackage. layers = ["lines", ...] copies whole OSM layers, or
    layers = {out_table = {from = "lines", where = "...", select = "a,b"}} filters them.
    bbox optional (omit = everything). indexes = ["table(col)", ...] optional."""
    need_tool("ogr2ogr")
    out, src = ctx.path(s["dest"]), ctx.path(s["from"])
    if out.is_file() and not ctx.a.refresh:
        return {"unchanged": True}
    if not src.is_file():
        raise CliError(E_NOTFOUND, f"{src} missing", "run `lmap data fetch osm-au --yes` first")
    lay = s["layers"]
    lay = {l: {"from": l} for l in lay} if isinstance(lay, list) else lay
    for name in lay:                                   # goes into SQL text below
        sql_ident(name, f"{s['id']}: layer name")
    if plan:
        return {"would_build": str(out), "from": str(src), "layers": list(lay)}
    raw = ctx.scratch() / f"{s['id']}.{os.getpid()}.raw.gpkg"; raw.unlink(missing_ok=True)
    part = ctx.scratch() / f"{s['id']}.{os.getpid()}.part.gpkg"; part.unlink(missing_ok=True)
    spat = ["-spat"] + [str(v) for v in s["bbox"]] if s.get("bbox") else []
    if not spat and src_boxes(s):                      # bboxes: read their extent, keep only what lies in a box
        spat = ["-spat"] + [str(v) for v in src_extent(s)] + ["-clipsrc", boxes_wkt(src_boxes(s))]
    env = {**os.environ, "OGR_SQLITE_SYNCHRONOUS": "OFF",
           "CPL_TMPDIR": str(ctx.scratch()),       # OSM driver temp (~1 GB) -> scratch, not cwd
           "SQLITE_TMPDIR": str(ctx.scratch())}    # the ORDER BY sort spills here, not /tmp
    # raw + sorted copy + sort temp, each ~ the last build's size (first build: ~2x the pbf; a bbox clip: small)
    old = out.stat().st_size if out.is_file() else (0 if src_boxes(s) else 2 * src.stat().st_size)
    need = 3 * old + 1e9
    if free_bytes(ctx.scratch()) < need:
        raise CliError(E_FAIL, f"scratch too small for {s['id']}: needs ~{need / 1e9:.0f} GB, "
                               f"{room(ctx.scratch())}", "free space or set [paths] tmp = to a bigger disk")
    full = lambda: "scratch nearly full" if free_bytes(ctx.scratch()) < 1e9 else None
    try:
        for i, (name, d) in enumerate(lay.items()):
            cmd = ["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + spat + ["-nln", name]
            if d.get("where"):
                cmd += ["-where", d["where"]]
            if d.get("select"):
                cmd += ["-select", d["select"]]
            run(cmd + [raw, src, d["from"]], ctx, env=env, watch=full)
        for i, name in enumerate(lay):                 # same rows, stored by place (see SPATIAL_ORDER)
            run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if i else []) + ["-unsetFid", "-nln", name, "-sql",
                 f'SELECT * FROM "{name}" ORDER BY {SPATIAL_ORDER}', part, raw], ctx, env=env, watch=full)
        raw.unlink(missing_ok=True)
        for ix in s.get("indexes", []):
            m = re.fullmatch(r"(\w+)\((\w+)\)", ix)
            if not m:
                raise CliError(E_USAGE, f"{s['id']}: bad index {ix!r}", 'use "table(column)"')
            t, col = m.groups()
            run(["ogrinfo", "-q", part, "-sql",
                 f"CREATE INDEX IF NOT EXISTS {t}_{col} ON {t}({col})"], ctx)
        install_file(part, out)
    finally:
        part.unlink(missing_ok=True); raw.unlink(missing_ok=True)
    return {"built": str(out)}


# Rows in .osm.pbf order scatter one town's roads over the whole 1.6 GB file: a cold zoom-in over a network share
# then makes thousands of small reads (Gold Coast 21 s for the roads layer). Ordering rows by 1-degree block,
# then 0.05-degree cell inside it, puts a view's features in a few contiguous runs of the file.
SPATIAL_ORDER = ("CAST(ST_MinY(geom) + 90 AS INTEGER), CAST(ST_MinX(geom) + 180 AS INTEGER), "
                 "CAST((ST_MinY(geom) + 90) * 20 AS INTEGER), CAST((ST_MinX(geom) + 180) * 20 AS INTEGER)")


def arcgis_query(url, params, meter=None):
    import urllib.parse
    q = url + "/query?" + urllib.parse.urlencode(params)
    for i in range(4):     # a truncated / HTML 200 (proxy, gateway page) is a network fault: retry, never a crash
        raw = http_get(q, timeout=120, retries=3)
        if raw is not None and meter is not None:
            meter.add(len(raw))
        if raw is None:
            raise CliError(E_UPSTREAM, f"404: {url}")
        try:
            d = json.loads(raw); break
        except ValueError:
            if i == 3:
                raise CliError(E_UPSTREAM, f"ArcGIS sent a non-JSON answer ({len(raw)} bytes: {raw[:60]!r})")
            RetryPolicy(4, 2.5, 15).wait(i)       # ~2.5/6/15 s, jittered (threads don't retry in step); raises Aborted
    if "error" in d:
        raise CliError(E_UPSTREAM, f"ArcGIS error {d['error'].get('code')}: {d['error'].get('message')}")
    return d


def layer_url(s, lid):
    """url + layer id. lid may be a number or a path ("Svc/MapServer/1", "../Folder/Svc/MapServer/3")."""
    import posixpath
    u = urllib.parse.urlsplit(s["url"])
    path = posixpath.normpath(posixpath.join(u.path, str(lid)))
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, "", ""))


ARC_CHUNKS = 8                                          # ArcGIS keyset ranges in flight per layer, at most (Tuner)
ARC_Q = '{"mode":"edit","originPosition":"upperLeft","tolerance":1e-7}'   # ~1 cm: 4x smaller JSON, exact enough


def arcgis_dequantize(d):
    """Quantized ESRI JSON -> real coordinates (GDAL reads the integers as-is: garbage geometry).
    Per ring/path: first vertex absolute, the rest deltas; x = tx + ix*sx, y = ty - iy*sy (originPosition upperLeft)."""
    tr = d.pop("transform", None)
    if not tr:
        return d
    (sx, sy), (tx, ty) = tr["scale"][:2], tr["translate"][:2]     # some servers add z, m: [sx, sy, sz, sm]
    def fix(parts):
        out = []
        for pth in parts:
            x = y = 0; q = []
            for i, v in enumerate(pth):
                x += v[0]; y += v[1]
                q.append([tx + x * sx, ty - y * sy] + list(v[2:]))
            out.append(q)
        return out
    for f in d.get("features") or []:
        gm = f.get("geometry") or {}
        for k in ("paths", "rings"):
            if k in gm:
                gm[k] = fix(gm[k])
        if "points" in gm:
            gm["points"] = fix([gm["points"]])[0]
        if "x" in gm and "y" in gm:                  # points are absolute
            gm["x"] = tx + gm["x"] * sx; gm["y"] = ty - gm["y"] * sy
    return d


def arc_statewide(s):
    """Whole-state pull: no spatial filter (explicit flag, or the legacy whole-QLD bbox)."""
    return bool(s.get("statewide")) or list(s.get("bbox") or []) == [137.9, -29.2, 153.6, -9.0]


def _ogr_scalar(gpkg, table, sql):
    """One number from an OGR SQL query on a GeoPackage (same dialect as ogrinfo -sql, no text parsing).
    None = the table doesn't exist yet or the query has no row. Any other failure RAISES: guessing 0 here
    would restart a layer over rows already stored."""
    from osgeo import gdal, ogr
    _gx, _ox = gdal.GetUseExceptions(), ogr.GetUseExceptions()     # restore after: other code relies on None returns
    gdal.UseExceptions(); ogr.UseExceptions()
    ds = None
    try:
        try:                                       # update: a killed run leaves a hot journal that SQLite must
            ds = ogr.Open(str(gpkg), 1)            # roll back first; read-only it fails ("readonly database")
        except RuntimeError as e:
            ds = None; why = str(e)
        else:
            why = "not a readable GeoPackage"
        if ds is None:
            raise CliError(E_FAIL, f"{Path(gpkg).name}: can't open the partial download ({why})",
                           f"delete {gpkg} and its checkpoint, then fetch again")
        if ds.GetLayerByName(table) is None:
            return None
        lyr = ds.ExecuteSQL(sql)
        try:
            f = lyr.GetNextFeature() if lyr is not None else None
            return None if f is None or not f.IsFieldSetAndNotNull(0) else f.GetField(0)
        finally:
            if lyr is not None:
                ds.ReleaseResultSet(lyr)
    except RuntimeError as e:
        raise CliError(E_FAIL, f"{table}: can't read the partial download ({e})",
                       f"delete {gpkg} and its checkpoint, then fetch again")
    finally:
        ds = None
        if not _gx:
            gdal.DontUseExceptions()
        if not _ox:
            ogr.DontUseExceptions()


def arcgis_layer_keyset(ctx, s, name, lurl, geo, part, tmp, state, ck):
    """One ArcGIS layer -> table `name` in part (GeoPackage), keyset-paged: WHERE (filter) AND oid > last ORDER BY oid.
    Every page costs the same at any depth (offset paging made the server sort ~1M rows per deep page: 58 s vs
    0.7 s). The OID range from outStatistics is split into 3 chunks fetched in parallel; ONE writer appends
    (GeoPackage can't take concurrent writers). Resume = MAX(oid) already in the table per chunk, so a kill
    between write and checkpoint can't duplicate or drop rows. End of a chunk = an EMPTY page (not a short one).
    The OID range is cut into up to ARC_CHUNKS pieces and a Tuner hill-climbs how many run at once (bytes/s;
    halves on a failed page), capped by the source's max_conns / the server's machine-wide limit (host_slot).
    Resume still works across any chunk count: keyset paging fills each range in order, so MAX(oid) per range
    is where that range stopped."""
    import concurrent.futures as cf, queue as qmod
    meta = json.loads(http_get(lurl + "?f=json", timeout=60) or b"{}")
    page = max(50, min(int(meta.get("maxRecordCount") or 1000), 2000))
    oid = meta.get("objectIdField") or next(
        (f["name"] for f in meta.get("fields") or [] if f.get("type") == "esriFieldTypeOID"), None)
    if not oid:
        raise CliError(E_UPSTREAM, f"{name}: layer has no object-id field; can't page it")
    sql_ident(name, "layer name")                      # both go into SQL text
    if not SQL_IDENT.fullmatch(str(oid)):
        raise CliError(E_UPSTREAM, f"{name}: the server's object-id field {oid!r} is not a plain name; can't page it")
    names = {f["name"] for f in meta.get("fields") or []}
    fields = s.get("fields")
    if isinstance(fields, dict):                        # per layer: { table = "a,b", ... }
        if name not in fields:
            raise CliError(E_USAGE, f"{s['id']}: fields has no entry for layer {name}",
                           f"add {name} = \"...\" to its fields table (or drop the table to fetch all fields)")
        fields = fields[name]
    if fields:
        bad = [x for x in fields.split(",") if x.strip() not in names]
        if bad:
            raise CliError(E_USAGE, f"{name}: unknown fields {bad}", f"valid: {', '.join(sorted(names))}")
        out_fields = ",".join(dict.fromkeys([oid] + [x.strip() for x in fields.split(",")]))
    else:
        out_fields = "*"
    statewide = arc_statewide(s)
    g = {} if statewide else {k: v for k, v in geo.items() if k != "where"}   # whole QLD: no spatial test
    base = f"({s.get('where', '1=1')})"
    st = arcgis_query(lurl, {**g, "where": base, "f": "json", "outStatistics": json.dumps(
        [{"statisticType": t, "onStatisticField": oid, "outStatisticFieldName": t} for t in ("min", "max", "count")])})
    at = (st.get("features") or [{}])[0].get("attributes") or {}
    at = {k.lower(): v for k, v in at.items()}
    lo, hi, n = at.get("min"), at.get("max"), at.get("count") or 0
    if not n or lo is None:
        return 0
    # resume: what's already stored (ESRIJSON -> GPKG makes the OID the FID column, so it survives)
    have = 0; done_max = {}
    if part.is_file():
        have = _ogr_scalar(part, name, f"SELECT COUNT(*) FROM {name}")
        have = have or 0
    host = urllib.parse.urlsplit(lurl).hostname or ""
    cap = max(1, min(int(s.get("max_conns", ARC_CHUNKS)), host_cap(host)))
    if ctx.jobs != 3:                                   # --jobs N: an explicit ceiling
        cap = max(1, min(cap, ctx.jobs))
    # the ranges are saved beside the checkpoint: a resume MUST reuse the same cut (MAX(oid) per range is only
    # "where it stopped" for the range that was paged in order; a different cut could skip rows)
    rf = Path(f"{ck}.{name}.ranges")
    chunks = None
    if have and not rf.is_file():                       # rows but no saved cut: MAX(oid) per range would skip rows
        rf.write_text("[]")                             # -> the unreadable-cut path below drops just this table
    if have and rf.is_file():
        try:
            chunks = [(int(a_), int(b_)) for a_, b_ in json.loads(rf.read_text())]
            if not chunks:
                raise ValueError("empty")
        except (ValueError, TypeError, OSError):        # unreadable cut: resuming could skip rows -> start over
            ctx.log(f"  {name}: saved ranges unreadable, fetching this layer again from the start")
            r = subprocess.run(["ogrinfo", "-q", "-sql", f"DROP TABLE {name}", str(part)], capture_output=True, text=True)
            if r.returncode != 0 or subprocess.run(["ogrinfo", "-q", "-so", str(part), name],
                                                   capture_output=True).returncode == 0:
                raise CliError(E_FAIL, f"{name}: can't drop the partial table to start it over ({r.stderr.strip()[:200]})",
                               f"delete {part} and its checkpoint, then fetch again")
            have, chunks = 0, None
    if chunks and chunks[-1][1] < hi:                   # server gained rows since the stop: last range takes them
        chunks[-1] = (chunks[-1][0], hi)
        atomic_write(rf, json.dumps(chunks))
    if chunks is None:
        nchunk = max(1, min(ARC_CHUNKS * 2, n // (20 * page)))   # more ranges than workers: idle ones take the next
        span = (hi - lo + 1) / nchunk
        chunks = [(int(lo - 1 + span * k), int(lo - 1 + span * (k + 1)) if k < nchunk - 1 else hi) for k in range(nchunk)]
        atomic_write(rf, json.dumps(chunks))
    if have:
        for a_, b_ in chunks:
            m = _ogr_scalar(part, name, f"SELECT MAX({oid}) FROM {name} WHERE {oid} > {a_} AND {oid} <= {b_}")
            done_max[(a_, b_)] = int(m) if m is not None else a_     # empty range: from its start
        ctx.log(f"  {name}: resuming, {have:,} of {n:,} stored")
    def table_exists():
        if not part.is_file():
            return False
        r = subprocess.run(["ogrinfo", "-q", "-so", str(part), name], capture_output=True, text=True)
        return r.returncode == 0
    wq = qmod.Queue(maxsize=6); errs = []; got = [have]; first = [not table_exists()]
    def writer():
        while True:
            d = wq.get()
            if d is None:
                return
            try:
                tmp_i = Path(str(tmp) + ".w.json"); tmp_i.write_text(json.dumps(d))
                run(["ogr2ogr", "-f", "GPKG"] + (["-update"] if part.is_file() else [])
                    + ([] if first[0] else ["-append"]) + ["-preserve_fid", "-nln", name, part, f"ESRIJSON:{tmp_i}"], ctx)
                first[0] = False; got[0] += len(d["features"])   # preserve_fid: the server OID stays the FID
                                                                  # (append without it renumbers -> resume breaks)
                if got[0] // 20000 != (got[0] - len(d["features"])) // 20000:
                    ctx.log(f"  {name}: {got[0]:,} / {n:,}")
                state[name] = (got[0], False); ck.write_text(json.dumps(state))
            except BaseException as e:
                errs.append(e)
            finally:
                wq.task_done()
    meter, tun = Meter(), Tuner(cap, window=20, min_units=1)   # start at cap/2, double while it helps
    live, live_lk = [0], threading.Lock()               # ranges actually downloading (for shrinking on errors)
    def chunk(a_, b_):
        try:
            return chunk_(a_, b_)
        except BaseException as e:                      # any failure stops the other ranges now
            if not any(x is e for x in errs):
                errs.append(e)
            with live_lk:
                live[0] -= 1
            raise
    def chunk_(a_, b_):
        last = done_max.get((a_, b_), a_); fails = 0; pg = page
        while not DRAIN.is_set() and not errs:
            if last >= b_:
                return None
            with live_lk:                               # over the tuned limit (errors halved it): hand the rest back
                if live[0] > max(1, int(tun.c)):
                    live[0] -= 1
                    return (last, b_)                   # rows <= last are stored; the saved cut is unchanged

            try:
                d = arcgis_query(lurl, {**g, "where": f"{base} AND {oid} > {last} AND {oid} <= {b_}",
                                        "orderByFields": f"{oid} ASC", "outFields": out_fields, "outSR": 4326,
                                        "resultRecordCount": pg, "quantizationParameters": ARC_Q, "f": "json"},
                                 meter=meter)
            except CliError as e:
                fails += 1; tun.unit(False)
                if fails > 5:
                    errs.append(e)                      # stop the other ranges now, not after they finish
                    raise
                if re.search(r"error 50[04]|HTTP Error 50[04]", e.msg, re.I) and pg > 250:
                    pg = max(250, pg // 2); ctx.log(f"  {name}: server error, page {pg} for this chunk")
                if ABORT.wait(min(60, 2 ** fails) * (0.5 + random.random())):
                    raise Aborted()                     # an abort used to fall through to the next query
                continue
            f = d.get("features") or []
            if not f:
                return None
            fails = 0; tun.unit(True)
            wq.put(arcgis_dequantize(d))
            nl = max(int(x["attributes"].get(oid) or x["attributes"].get(oid.lower()) or 0) for x in f)
            if nl <= last:                              # no OID / not advancing: would loop forever
                raise CliError(E_UPSTREAM, f"{name}: paging stalled at {oid} {last} (server returned no higher {oid})")
            last = nl
            ABORT.wait(0.2 * random.random())               # a little jitter between requests: polite
    th = threading.Thread(target=writer, daemon=True); th.start()
    try:
        todo = [c for c in chunks if done_max.get(c, c[0]) < c[1]]; running = set(); peak = 0
        with cf.ThreadPoolExecutor(cap) as ex:
            while todo or running:
                while todo and len(running) < max(1, int(tun.c)) and not DRAIN.is_set() and not errs:
                    with live_lk:
                        live[0] += 1
                    running.add(ex.submit(chunk, *todo.pop(0)))
                if len(running) > peak:
                    ctx.log(f"  {name}: {len(running)} ranges in parallel")
                peak = max(peak, len(running))
                tun.busy(len(running) + (0 if todo else int(tun.c)))   # out of ranges: not "unsaturated"
                tun.tick(meter.bytes)
                fin, running = cf.wait(running, timeout=2, return_when=cf.FIRST_COMPLETED)
                for fu in fin:
                    rest = fu.result()
                    if rest:
                        todo.insert(0, rest)            # handed back: resumes when the limit allows
                    else:
                        with live_lk:
                            live[0] -= 1
                if (DRAIN.is_set() or errs) and not running:
                    break
        ctx.log(f"  {name}: up to {peak} parallel ranges (cap {cap})")
    finally:
        wq.put(None); th.join()
    if errs:
        raise errs[0]
    if DRAIN.is_set():
        raise Aborted()
    if got[0] < n:     # short: a page lost somewhere (resume only covers the tail of each range) - never install it
        now = None         # unless the layer really shrank while we paged (live edits): count again now
        for k in range(3):
            try:
                now = arcgis_query(lurl, {**g, "where": base, "f": "json", "returnCountOnly": "true"}).get("count")
                if now is not None:
                    break
            except (CliError, ValueError):
                pass
            RetryPolicy(3, 2.5, 15).wait(k)       # jittered; raises Aborted
        if now is None:    # can't tell: keep what's stored, the next run resumes and counts again
            raise CliError(E_UPSTREAM, f"{name}: stored {got[0]:,} of {n:,} rows and the recount failed - not installed",
                           "fetch again: it resumes and checks the count again")
        if got[0] < int(now):
            r = subprocess.run(["ogrinfo", "-q", "-sql", f"DROP TABLE {name}", str(part)], capture_output=True, text=True)
            if r.returncode != 0 or subprocess.run(["ogrinfo", "-q", "-so", str(part), name],
                                                   capture_output=True).returncode == 0:
                raise CliError(E_FAIL, f"{name}: rows missing and can't drop the partial table to start it over "
                               f"({r.stderr.strip()[:200]})", f"delete {part} and its checkpoint, then fetch again")
            rf.unlink(missing_ok=True)
            raise CliError(E_UPSTREAM, f"{name}: stored {got[0]:,} rows, the server has {int(now):,}"
                           f" - rows missing, not installed", "fetch again: this layer starts over")
        ctx.log(f"  {name}: server now has {int(now):,} rows (was {n:,} at the start): stored {got[0]:,}")
    elif got[0] > n:
        ctx.log(f"  {name}: stored {got[0]:,}, server had {n:,} at the start (rows added while paging)")
    return got[0]


def arcgis_fetch(ctx, s, plan):
    """ArcGIS REST layers -> one GeoPackage (one table per layer), clipped to bbox, paged.
    Catalog: url = ".../MapServer", layers = {table = layer_id, ...}, bbox = [w,s,e,n]."""
    need_tool("ogr2ogr")
    out = ctx.path(s["dest"])
    if out.is_file() and not ctx.a.refresh:
        return {"unchanged": True}
    geo = {**arc_geo(s), "where": s.get("where", "1=1")}
    fl = s.get("fields")
    if isinstance(fl, dict) and set(s["layers"]) - set(fl):   # check up front, not hours in
        raise CliError(E_USAGE, f"{s['id']}: fields has no entry for {sorted(set(s['layers']) - set(fl))}",
                       "add one per layer (or drop the fields table to fetch all fields)")
    for name, lid in s["layers"].items():               # group layers have no data: fail clearly
        meta = json.loads(http_get(f"{layer_url(s, lid)}?f=json", timeout=60) or b"{}")
        if meta.get("type") == "Group Layer":
            subs = [x["id"] for x in meta.get("subLayers", [])]
            raise CliError(E_USAGE, f"{s['id']}: layer {name}={lid} ({meta.get('name')}) is a group layer",
                           f"use its sublayers instead: {subs}")
    if plan:
        counts = {}
        for name, lid in s["layers"].items():
            g = {"where": geo["where"]} if arc_statewide(s) else geo   # same filter the fetch uses
            counts[name] = arcgis_query(layer_url(s, lid), {**g, "returnCountOnly": "true", "f": "json"}).get("count")
        return {"would_build": str(out), "features": counts}
    need = float(s.get("est_gb") or 0) * 1.5e9
    if need and free_bytes(ctx.scratch()) < need:
        raise CliError(E_FAIL, f"scratch {ctx.scratch()} has {room(ctx.scratch())}, "
                               f"{s['id']} needs ~{need / 1e9:.1f} GB to build", "pass --tmp DIR on a local disk with room")
    # SQLite (GeoPackage) locking doesn't work on CIFS: build in local scratch, copy when done.
    # Big (statewide) pulls resume: the scratch .gpkg + a checkpoint survive a stop/crash.
    part = ctx.scratch() / f"{s['id']}.part.gpkg"         # fixed name: it resumes (source Lock guards it)
    tmp = ctx.scratch() / f"{s['id']}-page.json"
    ck = ctx.scratch() / f"{s['id']}.resume.json"
    state = {}
    if part.is_file() and ck.is_file() and not ctx.a.refresh:
        try:
            state = json.loads(ck.read_text())
        except ValueError:
            state = {}
    if not state:
        part.unlink(missing_ok=True); ck.unlink(missing_ok=True)
        for r_ in ck.parent.glob(f"{ck.name}.*.ranges"):
            r_.unlink(missing_ok=True)
    elif state:
        ctx.log(f"resuming: {', '.join(f'{k} {v[0]}' + (' done' if v[1] else '') for k, v in state.items())}")
    stamp = fetch_stamp_path(s["id"])               # when this pull STARTED: data outdated counts edits after it
    pend = stamp.with_suffix(".pending")            # the start, until the file is installed (then tied to it)
    if not state:
        pend.parent.mkdir(parents=True, exist_ok=True)
        pend.write_text(json.dumps({"start": time.time()}))
    elif not pend.is_file():                        # resume, first start unknown: no stamp (2-day fallback)
        pend = None
    total = {}
    finished = False
    try:
        for i, (name, lid) in enumerate(s["layers"].items()):
            lurl = layer_url(s, lid)
            off, done = state.get(name, (0, False))[:2]
            if done:
                total[name] = off; continue
            got = arcgis_layer_keyset(ctx, s, name, lurl, geo, part, tmp, state, ck)
            total[name] = got
            state[name] = (got, True); ck.write_text(json.dumps(state))
        if not total or not any(total.values()):
            if s.get("allow_empty"):                    # area clip: e.g. storm tide for an inland box
                if pend is not None:
                    pend.unlink(missing_ok=True)
                return {"empty": True, "features": total}
            raise CliError(E_UPSTREAM, "no features returned for any layer (bbox right?)")
        for name in total:                              # spatial index for fast local clipping
            run(["ogrinfo", "-q", part, "-sql", f"SELECT CreateSpatialIndex('{name}','geom')"], ctx)
        stamp.unlink(missing_ok=True)
        install_file(part, out)
        finished = True
        if pend is not None and s["dest"] and arc_statewide(s):
            try:                                    # tie the start to THIS file (size + mtime as we read them)
                st_ = json.loads(pend.read_text())["start"]; o = out.stat()
                stamp.write_text(json.dumps({"start": st_, "size": o.st_size, "mtime": o.st_mtime, "dest": s["dest"]}))
            except (OSError, ValueError, KeyError):
                pass
            pend.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True); Path(str(tmp) + ".w.json").unlink(missing_ok=True)   # writer's page file
        if finished or not s.get("resumable", s.get("tier")):  # small (area) pulls: no resume state
            part.unlink(missing_ok=True); ck.unlink(missing_ok=True)
            for r_ in ck.parent.glob(f"{ck.name}.*.ranges"):
                r_.unlink(missing_ok=True)
    return {"built": str(out), "features": total}


NET_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "9p", "ceph", "glusterfs", "fuse.rclone"}


def mount_fstype(p, table="/proc/self/mountinfo"):
    """Kernel filesystem type of the mount holding p ('cifs', 'nfs4', 'ext4'...; '' off Linux or unknown).
    Longest mount point wins; an autofs trigger at the same point is skipped (the real mount sits on it)."""
    try:
        p = os.path.realpath(p); best, typ = "", ""
        for ln in open(table, encoding="utf-8", errors="replace"):
            a, _, b = ln.partition(" - ")
            mp = re.sub(r"\\([0-7]{3})", lambda m_: chr(int(m_[1], 8)), a.split()[4]); t = b.split()[0] if b else ""
            if t == "autofs":
                continue
            if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, typ = mp, t
        return typ
    except (OSError, IndexError):
        return ""


class CopyPace:
    """pace big writes to a network share (cgroup io limits don't apply to CIFS) so the network share stays usable
    for everyone else. Per 64 MB block: time write + fsync. Slow block (over 4x the best seen and over 2 s):
    cap the rate at half what that block managed (AIMD cut, at most once per block). Fast block: raise the
    cap by 8 MB/s; above twice the best rate seen, or after 16 healthy blocks in a row, it is dropped (unpaced).
    Never below 4 MB/s. A pacing wait stops within 0.2 s on stop() / ABORT."""
    BLOCK, FLOOR, STEP = 64 << 20, 4 << 20, 8 << 20

    def __init__(self):
        self.cap, self.best_t, self.best_rate, self.fast = None, None, 0.0, 0

    def sleep(self, s, stop=None):
        """Wait s seconds; a stop (stop() true, or ABORT) cuts it short within 0.2 s and raises Aborted."""
        end = time.monotonic() + s
        while (r := end - time.monotonic()) > 0:
            if (stop is not None and stop()) or ABORT.wait(min(r, 0.2)):
                raise Aborted()

    def block(self, n, secs):
        """A block of n bytes took secs (write + fsync). Returns seconds to wait before the next block."""
        secs = max(secs, 1e-6); rate = n / secs
        self.best_rate = max(self.best_rate, rate)
        self.best_t = secs if self.best_t is None else min(self.best_t, secs)
        if secs > max(4 * self.best_t, 2.0):
            self.fast = 0
            self.cap = max(self.FLOOR, rate / 2 if self.cap is None else min(self.cap, rate / 2))   # never raises it
        elif self.cap is not None:
            self.cap += self.STEP; self.fast += 1
            if self.cap > 2 * self.best_rate or self.fast >= 16:      # 16 healthy blocks (1 GB) in a row: unpaced
                self.cap, self.fast = None, 0
        return 0.0 if self.cap is None else max(0.0, n / self.cap - secs)


def copy_synced(src, dst, stop=None, pace=None, prog=None):
    """src -> dst in 16 MB chunks, fsync'd (a rename after an unflushed SMB write can expose a short file).
    stop(): checked per chunk, raises Aborted (an abort no longer waits out a multi-GB network-share copy).
    On a network filesystem (or pace=CopyPace()) the copy is fsync'd every 64 MB and paced (CopyPace)."""
    if pace is None and mount_fstype(Path(dst).parent) in NET_FS:
        pace = CopyPace()
    allb = 0
    with open(src, "rb") as i, open(dst, "wb") as o:
        n, t = 0, time.monotonic()
        while b := i.read(16 << 20):
            if (stop or ABORT.is_set)():
                raise Aborted()
            o.write(b); n += len(b); allb += len(b)
            if prog is not None:
                prog(allb)
            if pace is not None and n >= pace.BLOCK:
                o.flush(); os.fsync(o.fileno())
                w = pace.block(n, time.monotonic() - t)
                if w > 0:
                    pace.sleep(w, stop)
                n, t = 0, time.monotonic()
        o.flush(); os.fsync(o.fileno())
    if Path(dst).stat().st_size != Path(src).stat().st_size:
        raise CliError(E_FAIL, f"short copy to {dst}")


COPY_CHUNK = 16 << 20


def copy_resumable(src, dst, stop=None, prog=None):
    """src -> dst like copy_synced, but a dst left by a failed copy is CONTINUED: the last whole chunk before its end
    is compared with src (a torn write is redone) and copying resumes there, not from byte 0. fsync'd per 64 MB on
    a network disk, size checked at the end."""
    size = Path(src).stat().st_size
    start = 0
    try:
        have = Path(dst).stat().st_size
    except OSError:
        have = 0
    if 0 < have <= size:
        start = max(0, (have // COPY_CHUNK) - 1) * COPY_CHUNK
        if start:
            with open(src, "rb") as a, open(dst, "rb") as b:
                a.seek(start - COPY_CHUNK); b.seek(start - COPY_CHUNK)
                if a.read(COPY_CHUNK) != b.read(COPY_CHUNK):
                    start = 0
    if start == 0:
        Path(dst).unlink(missing_ok=True)
        return copy_synced(src, dst, stop=stop, prog=prog)
    net = mount_fstype(Path(dst).parent) in NET_FS
    with open(src, "rb") as a, open(dst, "r+b") as b:
        a.seek(start); b.seek(start); b.truncate(start); n, done = 0, start
        while blk := a.read(COPY_CHUNK):
            if (stop or ABORT.is_set)():
                raise Aborted()
            b.write(blk); n += len(blk); done += len(blk)
            if prog is not None:
                prog(done)
            if net and n >= 64 << 20:
                b.flush(); os.fsync(b.fileno()); n = 0
        b.flush(); os.fsync(b.fileno())
    if Path(dst).stat().st_size != size:
        raise CliError(E_FAIL, f"short copy to {dst}")
    # resumed: writes lost before an earlier reconnect could sit anywhere before the resume point (CIFS fsyncs
    # every 64 MB). Compare the whole file once; any difference = copy again from 0.
    with open(src, "rb") as a, open(dst, "rb") as b:
        while True:
            x = a.read(COPY_CHUNK)
            if x != b.read(COPY_CHUNK):
                Path(dst).unlink(missing_ok=True); return copy_synced(src, dst, stop=stop, prog=prog)
            if not x:
                break


def install_file(src, out, link=True):
    """Copy a locally built file to its destination (often a network share): write out.part, then rename.
    Scratch and destination on the same disk: hard-link instead of copying (instant, no 2x space); src stays.
    INVARIANT: finished .gpkg files on the root change ONLY through this rename, never by an in-place edit:
    readers open them with SQLite locking off (ro_gpkg_opts, IMMUTABLE) and would see a half-done edit."""
    out.parent.mkdir(parents=True, exist_ok=True)
    dst = Path(f"{out}.{os.getpid()}.part")             # private: two runs writing one dest can't clash
    try:
        dst.unlink(missing_ok=True)
        try:
            same = link and os.stat(src).st_dev == os.stat(out.parent).st_dev
            if same:
                fd = os.open(src, os.O_RDONLY)
                try:
                    os.fsync(fd)                          # as durable as copy_synced before the rename
                finally:
                    os.close(fd)
                os.link(src, dst)
        except OSError:
            same = False
        if not same:
            copy_synced(src, dst)
        replace_retry(dst, out)
    except BaseException:
        dst.unlink(missing_ok=True); raise


def replace_retry(dst, out, tries=6):
    """os.replace that waits out a CIFS rename refused while another process has `out` open. Reproduced
    on a network share: the rename fails with ENOENT; both dst and the old `out` survive; it works ~1 s after
    the reader closes. Retries only the errnos seen for "open elsewhere", ~25 s in total, then raises (callers
    unlink their part file; the old `out` stays). A reader held for longer (QGIS open all day) still fails."""
    import errno
    busy = {errno.ENOENT, errno.EBUSY, errno.EACCES, errno.EPERM, errno.ETXTBSY}
    for i in range(tries):
        try:
            os.replace(dst, out); return
        except OSError as e:
            if not Path(dst).exists() and Path(out).exists() and e.errno == errno.ENOENT and i > 0:
                return    # an earlier try went through on the server although the client reported an error
            if e.errno not in busy or i == tries - 1 or not Path(dst).exists() or not Path(out).parent.is_dir():
                raise
            if ABORT.wait(1 + 2 * i):
                raise Aborted()


def wmst_tiles(s):
    """Work units on a fixed global grid (so a bbox edit keeps finished units): 1-degree cells, or step= degrees
    (0.001..1, e.g. 0.1 for sub-metre imagery: one warp stays ~1-2 GB). Partial edge cells are included."""
    st = float(s.get("step", 1))
    if not 0.001 <= st <= 1:
        raise CliError(E_USAGE, f"{s.get('id', '?')}: step must be 0.001-1 degrees, got {st}")
    seen = set()                                     # bboxes: the union of each box's units, first box first
    for b in src_boxes(s):
        for k, v in _wmst_box(b, st):
            if k not in seen:
                seen.add(k)
                yield k, v


def _wmst_box(bbox, st):
    import math
    w, so, e, n = bbox
    if st == 1:
        for la in range(math.floor(so), math.ceil(n)):
            for lo in range(math.floor(w), math.ceil(e)):
                yield f"s{-la:02d}e{lo:03d}", (lo, la, lo + 1, la + 1)
        return
    eps = 1e-9; tag = f"{st:g}".replace(".", "p")
    for j in range(math.floor(so / st + eps), math.ceil(n / st - eps)):
        for i in range(math.floor(w / st + eps), math.ceil(e / st - eps)):
            yield f"g{tag}_r{j}_c{i}", (round(i * st, 6), round(j * st, 6), round((i + 1) * st, 6), round((j + 1) * st, 6))


def wmst_manifest(s):
    """Tile-cache identity: changing step/res/xml on an existing cache would mix incompatible tiles."""
    m = {"step": float(s.get("step", 1)), "res_m": float(s["res_m"]), "xml": Path(s["xml"]).name}
    if s.get("stac") and s.get("fetch", "lmap") != "gdal":      # pixels differ slightly from the WMS path: don't mix
        m["stac"] = f"{s['stac']['collection']}@{s['stac']['datetime']}"
    return m


def cache_fmt(d):
    """The tile format a source cache was started with (None = no manifest yet, or an unreadable format)."""
    try:
        return json.loads((d / "_manifest.json").read_text()).get("fmt")
    except (OSError, ValueError, AttributeError):
        return None


def unit_file(d, n, fmt):
    """The file that says unit n is done: its warped tile, or for a raw cache its marker."""
    return d / (f"{n}.done" if fmt == "raw" else f"{n}.tif")


def check_manifest(s, d, original=False, raw_ok=False):
    """Returns the cache's tile format: 'raw' (the server's own tiles in d/tiles, unchanged; the finished COG
    is the only encode) or 'webp'/'webp-ll' (RGBA GTiff units). Fixed per cache: one
    cache never mixes formats. raw_ok: a fresh cache of a plain TMS source becomes raw."""
    m = d / "_manifest.json"; want = wmst_manifest(s)
    if m.is_file():
        have = json.loads(m.read_text()); fmt = have.pop("fmt", None)
        if fmt not in ("raw", "webp", "webp-ll"):
            raise CliError(E_FAIL, f"{d}: tile cache in a format lmap no longer reads ({fmt or 'none recorded'})",
                           f"move/delete {d} to fetch it again")
        if have != want:
            raise CliError(E_FAIL, f"{d} holds tiles made with {have}, catalog now says {want}",
                           f"move/delete {d} to start over (or put the catalog back)")
        if original and fmt not in ("webp-ll", "raw"):             # never mix: "original" must not quietly use lossy tiles
            raise CliError(E_FAIL, f"{d} holds lossy tiles ({fmt}) but quality = \"original\"",
                           f"move/delete {d} to fetch it again lossless, or set quality = \"standard\"")
        return fmt
    if any(d.glob("*.tif")):
        raise CliError(E_FAIL, f"{d} has tiles but no manifest; can't tell what made them", f"move/delete {d} to start over")
    fmt = "raw" if raw_ok else "webp-ll" if original else "webp"
    d.mkdir(parents=True, exist_ok=True); m.write_text(json.dumps({**want, "fmt": fmt}))
    return fmt


def tile_co(fmt):
    """Scratch-tile format. webp: plain tiled GTiff, no per-tile overviews (the finished COG builds its own),
    RGBA WEBP (keeps alpha; YCbCr JPEG can't) at high quality so the final encode is the only real loss."""
    if fmt == "webp-ll":                              # quality=original: lossless (WEBP_LEVEL=100 is NOT lossless)
        return ["-of", "GTiff", "-co", "TILED=YES", "-co", "BLOCKXSIZE=512", "-co", "BLOCKYSIZE=512",
                "-co", "COMPRESS=WEBP", "-co", "WEBP_LOSSLESS=YES", "-co", "BIGTIFF=IF_SAFER"]
    return ["-of", "GTiff", "-co", "TILED=YES", "-co", "BLOCKXSIZE=512", "-co", "BLOCKYSIZE=512",
            "-co", "COMPRESS=WEBP", "-co", "WEBP_LEVEL=92", "-co", "BIGTIFF=IF_SAFER"]


def wmst_status(ctx, s):
    d = ctx.path(s["dest"]); t = [n for n, _ in wmst_tiles(s)]; fmt = cache_fmt(d)
    have = sum(unit_file(d, n, fmt).is_file() for n in t)
    return {"items_total": len(t), "items_have": have, "items_todo": len(t) - have}


# ---- per-server tile downloader: plain TMS ${z}/${y}/${x} sources are downloaded by lmap itself (keep-alive
# connections, per-server limits measured with a probe, back-off on every request), then warped from local files.
# GDAL used to do both, which left connections idle while it warped and only noticed trouble when a whole unit failed.
# Per-server limits: SERVERS / server_profile / host_cap in 00_base (one table).
THROTTLE = {403, 429, 503, 509}         # "slow down" (EOX answers 403 when pushed past ~24 connections)




class HostShare:
    """How many lmap processes are downloading from one server right now (flock'd presence files), so each
    takes max // n and the server total stays under max."""
    def __init__(self, host):
        self.d = STATE / "hosts" / host; self.d.mkdir(parents=True, exist_ok=True)
        # created under a private name, locked, then renamed into view: no one can see it unlocked
        tag = f"{os.getpid()}-{id(self):x}"; tmp = self.d / f".new-{tag}"
        self.fd = os.open(tmp, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(self.fd, fcntl.LOCK_EX)
        self.f = self.d / f"tms-{tag}"; os.rename(tmp, self.f)
        self.n, self.t, self.busy = 1, 0, 0

    def publish(self, lim):
        """Tell the GDAL path (HostBudget) how many connections this process uses on the server now."""
        if lim != getattr(self, "_pub", None):
            self._pub = lim; os.pwrite(self.fd, f"{lim:<8d}".encode(), 0)

    def gdal_busy(self):
        """Connections the old GDAL path holds on this server (locked slot files)."""
        n = 0
        for f in self.d.glob("slot*"):
            try:
                fd = os.open(f, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                n += 1
            finally:
                os.close(fd)
        return n

    def count(self):
        if time.time() - self.t < 10:
            return self.n
        self.busy = self.gdal_busy()
        n = 1
        for f in self.d.glob("tms-*"):
            if f == self.f:
                continue
            try:
                fd = os.open(f, os.O_RDWR)
            except OSError:
                continue                                       # removed meanwhile
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:                                           # stale: its process is gone (same inode = same file)
                    if os.fstat(fd).st_ino == os.stat(f).st_ino:
                        f.unlink()
                except OSError:
                    pass
            except BlockingIOError:
                n += 1
            finally:
                os.close(fd)
        self.n, self.t = n, time.time()
        return n

    def close(self):
        self.f.unlink(missing_ok=True); os.close(self.fd)


class TileGate:
    """Per-server congestion control on every request (AIMD, as TCP does):
    - ok: window += 1/window (about +1 connection per round of answers), up to the server's max
    - throttle (403/429/503/509) or error (other 5xx, timeout, reset): window halves (at most once per 2 s,
      so one burst of refusals is one cut) and new requests pause: Retry-After if sent, else 1 s doubling
      to 5 min while refusals keep coming; errors only pause after 3 in a row
    - a refusal also sets a ceiling one below the level that was refused, so it settles just under the
      server's limit instead of climbing back into it; the ceiling rises by 1 after each 5 clean minutes
    - latency: if answers get 3x slower than this server's normal (and >1 s), window x0.75 (once per 15 s), never
      below the start value; if a cut didn't make answers faster, that slowness is the server's new normal
      (not our load) and no more cuts follow for it
    Requests already in flight finish; the next ones wait."""
    def __init__(self, host, prof, share, log):
        self.host, self.cap, self.c = host, prof["max"], float(prof["start"])
        self.share, self.log, self.cv = share, log, threading.Condition()
        self.inflight, self.pause_until, self.strikes, self.last_cut, self.last_strike = 0, 0.0, 0, 0.0, 0.0
        self.ewma, self.base, self.base_prev, self.base_t = None, None, None, time.time()
        self.ok_n, self.bad_n, self.cuts, self.ceil, self.ceil_t = 0, 0, 0, prof["max"], 0.0
        self.floor, self.slow_at, self.normal_t = float(max(1, prof["start"] // 4)), None, 0.0
        self.ceil_min, self.burst_t, self.throttled_since = max(1, prof["start"] // 2), 0.0, None

    def limit(self):
        n = self.share.count()
        lim = max(1, min(int(self.c), (self.cap - self.share.busy) // n))
        self.share.publish(lim)
        return lim

    def enter(self):
        with self.cv:
            while True:
                if ABORT.is_set():                         # stop --now; a plain stop lets queued units finish
                    raise Aborted()
                now = time.time()
                if now < self.pause_until:
                    self.cv.wait(min(1.0, self.pause_until - now)); continue
                if self.inflight < self.limit():
                    self.inflight += 1; return
                self.cv.wait(1.0)

    def _cut(self, f, why, timed=False):
        now = time.time()
        if now - self.last_cut < 2:
            return False
        old = self.c; self.c = max(1.0, self.c * f); self.last_cut = now; self.cuts += 1
        if why.startswith("refused") and not timed and now - self.burst_t > 30:   # first cut of a burst: that level was too many
            self.ceil = max(self.ceil_min, int(old) - 1); self.ceil_t = now
        if why.startswith("refused"):
            self.burst_t = now
        self.log(f"{self.host}: {why}: connections {int(old)} -> {int(self.c)}")
        return True

    def leave(self, kind, lat=0.0, retry_after=None, why=None):
        with self.cv:
            self.inflight -= 1; now = time.time()
            if kind == "ok":
                self.ok_n += 1; self.strikes = 0; self.throttled_since = None
                if self.ceil < self.cap and now - self.ceil_t > 120 and now - self.burst_t > 120:
                    self.ceil = min(self.cap, self.ceil + max(1, self.ceil // 10)); self.ceil_t = now
                if self.c < self.ceil:
                    self.c = min(float(self.ceil), self.c + 1.0 / self.c)
                self.ewma = lat if self.ewma is None else self.ewma * 0.98 + lat * 0.02
                if now - self.base_t > 120:                    # normal = best of the last 2-4 min
                    self.base_prev, self.base, self.base_t = self.base, None, now
                if self.normal_t and now - self.normal_t > 600:  # an accepted "new normal" expires after 10 min
                    self.normal_t = 0.0; self.base_prev = None
                self.base = lat if self.base is None else min(self.base, lat)
                ref = min(x for x in (self.base, self.base_prev) if x is not None)
                if self.ok_n > 200 and self.ewma > max(3 * ref, 1.0) and now - self.last_cut >= 15:
                    if self.slow_at is not None and self.ewma > self.slow_at * 0.85:
                        self.base = self.base_prev = self.ewma; self.slow_at = None; self.normal_t = now  # cutting didn't help
                        self.log(f"{self.host}: answers {self.ewma:.2f}s whatever the load: new normal")
                    elif self.c > self.floor:
                        if self._cut(0.75, f"answers slowing ({self.ewma:.2f}s vs {ref:.2f}s normal)"):
                            self.slow_at = self.ewma
                        self.c = max(self.c, self.floor)
            else:
                self.bad_n += 1
                if kind == "throttle" and self.throttled_since is None:
                    self.throttled_since = now
                self._cut(0.5, ("refused" if kind == "throttle" else "errors") + (f" ({why})" if why else ""),
                          timed=retry_after is not None)
                if now - self.last_strike > 2:
                    self.strikes += 1; self.last_strike = now
                    if kind == "throttle" or self.strikes >= 3:
                        p = retry_after if retry_after is not None else min(300, 2 ** (self.strikes - 1)) * random.uniform(0.75, 1.25)
                        if now + p > self.pause_until:
                            self.pause_until = now + p
                            self.log(f"{self.host}: pausing {p:.0f}s (strike {self.strikes})")
            self.cv.notify_all()


def tile_body_ok(b):
    """A complete image, not an error page or a cut-off one: JPEG has its end marker, PNG its IEND, WEBP its
    full RIFF size (a little padding after the end, as some servers add, is fine)."""
    if b[:3] == b"\xff\xd8\xff":
        return b.rfind(b"\xff\xd9") >= len(b) - 64
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return b.rfind(b"IEND") >= len(b) - 64
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return int.from_bytes(b[4:8], "little") + 8 <= len(b)
    if b[:10] == b"CntZImage " or b[:6] == b"Lerc2 ":     # Esri elevation cache (LERC): full decode at assembly
        return len(b) > 64
    return False


class TileFetcher:
    """One per server per run: `max` keep-alive worker threads draining a FIFO of (url, path, unit).
    Units queue all their tiles at once; FIFO keeps the oldest unit first so it can go to warp soonest."""
    def __init__(self, ctx, host, prof, blank):
        import queue
        self.ctx, self.host, self.q, self.blank = ctx, host, queue.Queue(), blank
        self.share = HostShare(host)
        self.gate = TileGate(host, prof, self.share, ctx.log)
        self.lock, self.bytes, self.tiles = threading.Lock(), 0, 0
        self.local = threading.local()
        self.th = [threading.Thread(target=self._worker, daemon=True) for _ in range(prof["max"])]
        for t in self.th:
            t.start()

    def close(self):
        for _ in self.th:
            self.q.put(None)
        for t in self.th:
            t.join(40)
        self.share.close()

    def _http(self, url):
        hdr = {"User-Agent": UA, "Accept": "image/*"}
        if PROXY_SET[0]:
            try:
                with OPENER.open(urllib.request.Request(url, headers=hdr), timeout=30) as r, live(r):
                    return r.status, read_capped(r, 64 << 20), r.headers
            except urllib.error.HTTPError as e:
                return e.code, b"", e.headers
        u = urllib.parse.urlsplit(url); c = getattr(self.local, "conn", None)
        if c is None:
            c = self.local.conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
                u.netloc, timeout=30)
        try:
            try:
                c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=hdr); r = c.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                c.close()                              # the server dropped an idle keep-alive: reconnect once, not an error
                c.request("GET", u.path + ("?" + u.query if u.query else ""), headers=hdr); r = c.getresponse()
            with live(c):
                try:
                    b = read_capped(r, 64 << 20)                # one map tile
                except ValueError:
                    c.close(); self.local.conn = None; raise
            if ABORT.is_set():
                raise Aborted()
            if (r.getheader("Connection") or "").lower() == "close":
                c.close(); self.local.conn = None
            return r.status, b, r
        except BaseException:
            c.close(); self.local.conn = None; raise

    def get(self, url, path, gone=0):
        """-> 'ok' (written atomically), 'missing' (204/404 the 3rd time) or 'again' (204/404: ask again later -
        the worker requeues it, so it doesn't sit idle). Raises CliError after 10 tries."""
        last, i, thr = None, 0, 0
        while i < 10:
            th = self.gate.throttled_since
            if th is not None and time.time() - th > 1800:
                raise CliError(E_UPSTREAM, f"{self.host} has refused for 30 min: stopping this unit ({last})")
            self.gate.enter(); t = time.time(); left = False
            try:
                st, b, h = self._http(url)
            except Exception as x:
                last = f"{type(x).__name__}: {x}"; self.gate.leave("error", why=last[:120]); left = True
                if isinstance(x, (OSError, urllib.error.URLError)) and net_wait(url, self.ctx.log):
                    continue                               # outage waited out: not one of the 10 tries
            else:
                lat = time.time() - t; net_ok_again(url)
                if st == 200 and tile_body_ok(b):
                    BW.take(len(b))                        # the bandwidth cap holds the slot: the gate sees real pace
                    self.gate.leave("ok", time.time() - t); left = True
                    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")   # units share tiles (raw)
                    tmp.write_bytes(b); os.replace(tmp, path)
                    with self.lock:
                        self.bytes += len(b); self.tiles += 1
                    return "ok"
                if st in (204, 404):
                    self.gate.leave("ok", lat); left = True; gone += 1
                    if gone >= 3:                          # asked 3 times over ~10 s: really no imagery there
                        return "missing"                   # (2 answers 1 s apart let a CDN blip become a hole)
                    return "again"
                ra = h.getheader("Retry-After") if hasattr(h, "getheader") else (h.get("Retry-After") if h else None)
                ra = retry_after_s(ra)
                self.gate.leave("throttle" if st in THROTTLE else "error", lat, min(ra, 600) if ra is not None else None,
                                why=f"HTTP {st}" + ("" if st != 200 else f" {len(b)} B not a complete image"))
                left = True
                if st in THROTTLE:                         # "slow down" isn't a broken tile: the gate waits it out
                    last = f"HTTP {st}"; thr += 1
                    if thr < 40:                           # ...unless it's this one tile, forever (EOX 403 on a
                        continue                           # forbidden tile): then it counts as a normal failure
                last = f"HTTP {st}" + ("" if st != 200 else f" (not a complete image: {h.getheader('Content-Type') if hasattr(h, 'getheader') else ''})")
            finally:
                if not left:
                    self.gate.leave("error")
            i += 1
            if ABORT.wait(min(30, 2 ** i) * random.uniform(0.5, 1.0)):
                raise Aborted()
        raise CliError(E_UPSTREAM, f"tile failed after 10 tries: {url}: {last}")

    def _later(self, delay, job):
        """Requeue a tile after delay (a timer, not a sleeping worker). The unit stays open meanwhile, so the
        fetcher isn't closed under it; on abort the tile is answered Aborted instead."""
        def go():
            if ABORT.is_set():
                job[2].finish(Aborted())
            else:
                self.q.put(job)
        t = threading.Timer(delay, go); t.daemon = True; t.start()

    def _worker(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            url, path, unit, gone = (job + (0,))[:4]
            if unit.err is not None or ABORT.is_set():
                unit.finish(None if unit.err is not None else Aborted()); continue
            try:
                r = self.get(url, path, gone)
                if r == "again":                           # empty answer: ask again in ~1 s, then ~8 s, off-thread
                    self._later((1, 8)[gone] * random.uniform(0.8, 1.2), (url, path, unit, gone + 1))
                    continue
                if r == "missing":       # stored as a real blank tile: a never-fetched tile
                    try:                               # never over a tile already there: a late 404 must not
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)   # turn imagery into a hole
                    except FileExistsError:            # (O_EXCL, not os.link: works on FAT/SMB too)
                        fd = None
                    if fd is not None:
                        try:
                            os.write(fd, self.blank); os.close(fd); fd = None
                        except OSError:
                            if fd is not None:
                                os.close(fd)
                            path.unlink(missing_ok=True); raise
            except (CliError, Aborted) as x:
                unit.finish(x)
            except Exception as x:
                unit.finish(CliError(E_FAIL, f"{type(x).__name__}: {x}"))
            else:
                unit.finish()


class TileBatch:
    """The tiles one work unit still needs; wait() blocks until all are answered (or one failed for good)."""
    def __init__(self, n):
        self.left, self.err, self.cv = n, None, threading.Condition()

    def finish(self, err=None):
        with self.cv:
            self.left -= 1
            if err is not None and self.err is None:
                self.err = err
            self.cv.notify_all()

    def wait(self):
        with self.cv:
            while self.left > 0:
                self.cv.wait(2)
        if self.err is not None:
            raise self.err


PROXY_SET = [False]


def zero_tile(bands, size):
    """A blank tile for a place the server confirmed has no imagery (204/404 asked 3 times): all zeros WITH alpha
    so it is transparent, never opaque black that looks like real dark imagery."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("LA" if bands <= 2 else "RGBA", (size, size)).save(buf, format="PNG")
    return buf.getvalue()


def tms_plan(text, res_m):
    """From a GDAL TMS xml: (url template, level to fetch, tile size m, origin x, origin y, yflip, tiles across,
    tiles down). None if it isn't the web-mercator square grid this downloader handles (-> GDAL path)."""
    g = lambda k: re.search(rf"<{k}>([^<]+)</{k}>", text)
    if not g("ServerUrl") or not g("TileLevel") or not g("UpperLeftX") or not g("LowerRightY"):
        return None
    url = g("ServerUrl").group(1).replace("&amp;", "&")
    ulx, uly, lrx = float(g("UpperLeftX").group(1)), float(g("UpperLeftY").group(1)), float(g("LowerRightX").group(1))
    lry = float(g("LowerRightY").group(1))
    maxl = int(g("TileLevel").group(1)); tcx = int(g("TileCountX").group(1)) if g("TileCountX") else 1
    tcy = int(g("TileCountY").group(1)) if g("TileCountY") else 1
    bs = int(g("BlockSizeX").group(1)) if g("BlockSizeX") else 256
    prj = (g("Projection").group(1) if g("Projection") else "").upper()
    if (prj.replace(" ", "") not in ("EPSG:3857", "EPSG:900913") or tcx != 1 or tcy != 1
            or abs(ulx + 20037508.34) > 1 or abs(lrx - 20037508.34) > 1 or abs(uly - 20037508.34) > 1
            or abs(lry + 20037508.34) > 1 or bs != int((g("BlockSizeY") or g("BlockSizeX")).group(1) if g("BlockSizeX") else 256)):
        return None
    yo = (g("YOrigin").group(1) if g("YOrigin") else "top").strip()
    z = maxl                                              # coarsest level still at least as fine as res_m
    for L in range(maxl + 1):
        if (lrx - ulx) / (tcx * 2 ** L * bs) <= float(res_m) * 1.001:
            z = L; break
    tile_m = (lrx - ulx) / (tcx * 2 ** z)
    return url, z, tile_m, ulx, uly, yo == "bottom", tcx * 2 ** z, tcy * 2 ** z


def tms_unit_tiles(plan, w, so, e, n, margin=1):
    """Tiles covering a lon/lat box (+margin tiles for the averaging kernel), as (x, y) in fetch-level numbering."""
    import math
    url, z, tm, ox, oy, flip, nx, ny = plan
    R = 6378137.0
    mx = lambda lo: math.radians(lo) * R
    my = lambda la: math.log(math.tan(math.pi / 4 + math.radians(max(-85.0511, min(85.0511, la))) / 2)) * R
    k = margin
    x0 = max(0, int((mx(w) - ox) // tm) - k); x1 = min(nx - 1, int((mx(e) - ox) // tm) + k)
    y0 = max(0, int((oy - my(n)) // tm) - k); y1 = min(ny - 1, int((oy - my(so)) // tm) + k)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def tms_local_xml(text, tdir, z, flip, bands=None):
    """The same server description, read from the downloaded tiles at the fetch level (no network, no cache)."""
    # no ZeroBlock* at all: every tile the warp reads must be on disk (blank ones are real blank files), so a
    # tile that was never fetched is an error, never a silent hole
    t = re.sub(r"<Cache>.*?</Cache>|<ZeroBlockOnServerException>.*?</ZeroBlockOnServerException>|"
               r"<ZeroBlockHttpCodes>.*?</ZeroBlockHttpCodes>|<MaxConnections>\d+</MaxConnections>", "", text, flags=re.S)
    from xml.sax.saxutils import escape
    su = f"<ServerUrl>file://{escape(str(tdir))}/${{z}}/${{y}}/${{x}}</ServerUrl>"
    t = re.sub(r"<ServerUrl>[^<]*</ServerUrl>", lambda _: su, t)
    t = re.sub(r"<TileLevel>\d+</TileLevel>", f"<TileLevel>{z}</TileLevel>", t)
    if flip:                                               # tiles are stored top-origin
        t = re.sub(r"<YOrigin>[^<]*</YOrigin>", "<YOrigin>top</YOrigin>", t)
    if bands:                                              # read with alpha: a blank (hole) tile is transparent
        t = re.sub(r"<BandsCount>\d+</BandsCount>", f"<BandsCount>{bands}</BandsCount>", t)
    return t


GPU_LIB = [None]   # path to the built kernel, False = no GPU path (checked once per process)


GPU_LOCK = threading.Lock()
GPU_FAILS = [0]    # GPU warps failed in a row; 2 -> GPU off for the rest of this run


def gpu_lib(ctx):
    """The GPU warp kernel (share/gpuwarp.cu), built once with nvcc into STATE/gpu/ (rebuilt when the .cu
    changes). Off with config [build] gpu = "no" / LMAP_GPU=no, or when there's no nvcc / working NVIDIA driver:
    gdalwarp then. Any problem here just turns the GPU path off; it never fails a unit."""
    while not GPU_LOCK.acquire(timeout=1):                 # the first unit builds, the others wait (abort-aware)
        if ABORT.is_set():
            raise Aborted()
    try:
        if GPU_LIB[0] is None:
            try:
                GPU_LIB[0] = _gpu_build(ctx)
            except Aborted:
                raise
            except Exception as x:
                ctx.log(f"GPU warp off: {type(x).__name__}: {x}"); GPU_LIB[0] = False
        return GPU_LIB[0]
    finally:
        GPU_LOCK.release()


def _gpu_build(ctx):
    if str(os.environ.get("LMAP_GPU") or ctx.gpu).lower() in ("0", "no", "false", "off"):
        return False
    cuda = [Path(os.environ[k]) / "bin" / "nvcc" for k in ("CUDA_HOME", "CUDA_PATH") if os.environ.get(k)]
    nvcc = shutil.which("nvcc") or next((str(c) for c in [*cuda, Path("/usr/local/cuda/bin/nvcc")]
                                         if os.access(c, os.X_OK)), None)
    smi = shutil.which("nvidia-smi")
    if not nvcc or not smi:
        return False
    try:
        if subprocess.run([smi, "-L"], capture_output=True, timeout=20).returncode:
            ctx.log("GPU warp off: nvidia-smi finds no working GPU"); return False
    except subprocess.TimeoutExpired:
        ctx.log("GPU warp off: nvidia-smi hangs"); return False
    import hashlib
    cu = HERE / "share" / "gpuwarp.cu"
    ver = subprocess.run([nvcc, "--version"], capture_output=True, text=True, timeout=30).stdout
    d = STATE / "gpu"; d.mkdir(parents=True, exist_ok=True)
    lib = d / f"gpuwarp-{hashlib.sha256(cu.read_bytes() + ver.encode()).hexdigest()[:12]}.so"
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:                                        # one build machine-wide; stop --now still works
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if ABORT.wait(1):
                    raise Aborted()
        if not lib.is_file():
            for old in list(d.glob("gpuwarp-*.so")) + list(d.glob("gpuwarp-*.tmp")):
                old.unlink(missing_ok=True)                # older kernels / builds cut off by a kill
            tmp = lib.with_suffix(f".{os.getpid()}.tmp")
            ctx.log("GPU warp: building the CUDA kernel (once)")
            try:
                _run([nvcc, "-O3", "-fmad=false", "-shared", "-Xcompiler", "-fPIC", "-o", tmp, cu], ctx, timeout=600)
            except CliError as x:
                tmp.unlink(missing_ok=True); ctx.log(f"GPU warp off: nvcc failed: {x.msg[-200:]}"); return False
            os.replace(tmp, lib)
    finally:
        os.close(fd)
    return str(lib)


def gpu_room(ctx, need_mb=1024):
    """[limits] gpu: VRAM used by everything (lmap, desktop, other programs) + need_mb must stay within gpu% of the
    card, and the GPU must not already be busier than that; else this unit warps on the CPU. Unknown: no GPU use."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False
    try:
        r = subprocess.run([smi, "--query-gpu=memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        tot, used, util = (float(x) for x in r.stdout.splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False
    cap = ctx.limits["gpu"] if getattr(ctx, "limits", None) else LIMIT_DEFAULT
    if used + need_mb > tot * cap / 100 or util > cap:
        ctx.vlog(f"GPU over the {cap}% limit ({used:.0f}/{tot:.0f} MB, {util:.0f}% busy): this unit on the CPU")
        return False
    return True


def gpu_warp(ctx, bx, part, tdir, w, so, e, nn, res, fmt):
    """GDAL 'average' warp of a local 3857 tile pyramid on the GPU (share/gpuwarp.py). True = part written and
    fully decodes; False = not possible here (caller runs gdalwarp). Holds one CPU slot; 2 decoder processes +
    4 GDAL (WEBP encode) threads. 2026-10-16, one QLD 2.4 m 1-degree unit alone: 2 threads 238 s, 4 162 s,
    6 (+3 decoders) 153 s. Two failures in a row turn it off for this run."""
    lib = gpu_lib(ctx)
    if not lib or not gpu_room(ctx):
        return False
    tc = tile_co(fmt)
    if tc[:2] != ["-of", "GTiff"]:
        return False
    co = [c for c in tc[2:] if c != "-co"] + ["NUM_THREADS=4"]   # just the GTiff creation options
    ok = False
    try:
        with CpuSlots():
            _run([sys.executable, HERE / "share" / "gpuwarp.py", lib, bx, part, w, so, e, nn, res, 2] + co,
                 ctx, env={**os.environ, "GDAL_CACHEMAX": "512"})
        why = deep_decode(ctx, part, 4) if part.is_file() else "no output"   # 4 procs: 24 s vs gdalinfo 372 s
        if why:
            raise CliError(E_FAIL, f"GPU output failed the decode check: {why}")
        ok = True
    except CliError as x:
        if "LMAP_GPU_SKIP" in x.msg:                       # not a GPU case (edge of the grid, odd source)
            ctx.vlog(f"GPU warp skipped: {x.msg[-120:]}")
            return False
        bad = tms_scrub(tdir)                               # a damaged tile: re-fetch it, don't warp it again
        if bad:
            raise CliError(E_FAIL, f"{bad} damaged tile(s) deleted, fetching again")
        with GPU_LOCK:
            GPU_FAILS[0] += 1
            if GPU_FAILS[0] >= 2 and GPU_LIB[0]:
                GPU_LIB[0] = False; ctx.log("GPU warp off for this run (2 failures in a row); gdalwarp from now on")
        ctx.log(f"GPU warp failed, using gdalwarp: {x.msg[-200:]}")
    finally:
        Path(f"{part}.gpu.tmp").unlink(missing_ok=True)    # left behind when the warp is killed (stop --now)
        if not ok:
            part.unlink(missing_ok=True)
    if not ok:
        return False
    with GPU_LOCK:
        GPU_FAILS[0] = 0
    return True


def tms_scrub(tdir, files=None, delete=True):
    """Tiles that don't fully decode are deleted (so a retry fetches them again) and counted. files: only these
    (default: all under tdir). delete=False: only count (`data verify` without --yes)."""
    from PIL import Image, features
    webp, bad = features.check("webp"), 0
    for f in (Path(tdir).glob("*/*/*") if files is None else files):
        if f.name.endswith(".tmp"):
            if delete:
                f.unlink(missing_ok=True)
            continue
        try:
            with Image.open(f) as im:
                im.load()
        except Exception:
            with open(f, "rb") as fh:
                head = fh.read(12)
            if head[:4] == b"RIFF" and not webp:
                continue                                   # Pillow can't read WEBP here: can't judge it
            if delete:
                f.unlink(missing_ok=True)
            bad += 1
    return bad


def tms_fetch(ctx, s, d, fmt, todo, text):
    """wms-tiles for a plain TMS server: lmap downloads each unit's tiles (TileFetcher), a warp pool turns
    them into the unit's tile from local files while the next units download. Same unit names, tile format,
    verify and install as the GDAL path, so caches and finish_out are shared."""
    import concurrent.futures as cf
    plan = tms_plan(text, s["res_m"]); url, z, tm, ox, oy, flip, nx, ny = plan
    host = urllib.parse.urlsplit(url).hostname or ""; prof = server_profile(host)
    ctx.log(f"{s['id']}: {len(todo)} unit(s), zoom {z} tiles from {host} (start {prof['start']}, max {prof['max']} connections)")
    bm = re.search(r"<BandsCount>(\d+)</BandsCount>", text); bsz = re.search(r"<BlockSizeX>(\d+)</BlockSizeX>", text)
    nb4 = bool(re.search(r"<BandsCount>4</BandsCount>", text))     # PNG with its own alpha: keep it, don't add one
    for c in own_glob(ctx, ctx.scratch(), s['id'], "*.cache"):         # old GDAL-path caches: this path can't use them
        shutil.rmtree(c, ignore_errors=True)
    nbands, bs = int(bm.group(1)) if bm else 3, int(bsz.group(1)) if bsz else 256
    tf = TileFetcher(ctx, host, prof, zero_tile(nbands, bs))
    meter, prog, failed = Meter(), Progress(s, len(todo), 0), []
    U = max(2, ctx.shrink + 2)                             # units in flight: downloads keep going while others warp
    raw = fmt == "raw"
    if raw:                                                # keep the server's tiles; finish_out reads them
        lx = d / "tiles.xml"; tmp = lx.with_name("tiles.xml.tmp")
        tmp.write_text(tms_local_xml(text, d / "tiles", z, flip, bands=nbands + 1 if nbands in (1, 3) else None))
        os.replace(tmp, lx)                                # 1/3-band: read with alpha, a no-imagery tile is a hole

    infl, infl_lock = {}, threading.Lock()                 # raw: a tile in flight, asked by one or more units

    class Fan:
        """One download answering every unit that needs this tile (units share border tiles)."""
        def __init__(self, p):
            self.p, self.bs = p, []

        @property
        def err(self):                                     # give up only if every waiting unit already failed
            return Aborted() if self.bs and all(b.err is not None for b in self.bs) else None

        def finish(self, err=None):
            with infl_lock:
                infl.pop(self.p, None); bs = list(self.bs)
            for b in bs:
                b.finish(err)

    def one(n, w, so, e, nn):
        part = ctx.scratch() / f"{s['id']}-{n}.part.tif"; bx = ctx.scratch() / f"{s['id']}-{n}.local.xml"
        tdir = d / "tiles" if raw else ctx.scratch() / f"{s['id']}-{n}.tiles"   # raw: one tree, shared by units
        t0 = time.time()
        try:
            checked = False
            for attempt in range(3):
                try:
                    tdir.mkdir(parents=True, exist_ok=True)
                    need, have = [], []
                    for x, y in tms_unit_tiles(plan, float(w), float(so), float(e), float(nn), margin=0 if raw else 1):
                        p = tdir / str(z) / str(y) / str(x)
                        if p.is_file():
                            have.append(p); continue
                        need.append((x, y, p))
                    random.shuffle(need)                           # random order: spreads load across the server's cache tiles
                    for y in {y for _, y, _ in need}:
                        (tdir / str(z) / str(y)).mkdir(parents=True, exist_ok=True)
                    batch = TileBatch(len(need))
                    for x, y, p in need:
                        sy = ny - 1 - y if flip else y
                        job = url.replace("${z}", str(z)).replace("${x}", str(x)).replace("${y}", str(sy))
                        if raw:
                            with infl_lock:
                                if p in infl:              # another unit is fetching it: wait for that answer
                                    infl[p].bs.append(batch); continue
                                if p.is_file():            # arrived since the scan
                                    batch.finish(); continue
                                fan = infl[p] = Fan(p); fan.bs.append(batch)
                            tf.q.put((job, p, fan))
                        else:
                            tf.q.put((job, p, batch))
                    batch.wait()                           # a stop mid-download raises Aborted; all here -> warp it
                    if ABORT.is_set():
                        raise Aborted()
                    dl = time.time() - t0
                    if raw:                                # every new tile must decode; a bad one is fetched again
                        bad = tms_scrub(tdir, [q for _, _, q in need if q.is_file()])
                        if bad:
                            raise CliError(E_FAIL, f"{bad} damaged tile(s) deleted, fetching again")
                        gone_ = sum(not q.is_file() for q in have + [q for _, _, q in need])
                        if gone_:                          # done only with every tile of its box on disk
                            raise CliError(E_FAIL, f"{gone_} tile(s) of this unit not on disk, fetching again")
                        xs = [x for x, _ in tms_unit_tiles(plan, float(w), float(so), float(e), float(nn), margin=0)]
                        ys = [y for _, y in tms_unit_tiles(plan, float(w), float(so), float(e), float(nn), margin=0)]
                        mk = d / f"{n}.done"; tmp = mk.with_name(mk.name + ".tmp")
                        blank = sum(q.stat().st_size == len(tf.blank) and q.read_bytes() == tf.blank
                                    for q in have + [q for _, _, q in need])
                        tmp.write_text(json.dumps({"z": z, "box": [min(xs), min(ys), max(xs), max(ys)],
                                                   "tiles": len(have) + len(need), "fetched": len(need),
                                                   "blank": blank}))
                        os.replace(tmp, mk)
                        return n, None, f"{len(need)} new of {len(have) + len(need)} tiles in {dl:.0f}s"
                    # a hole (blank tile) in a 1/3-band source: read it with alpha, so the hole stays transparent
                    hole = not nb4 and any(q.stat().st_size == len(tf.blank) and q.read_bytes() == tf.blank
                                           for q in have + [q for _, _, q in need])
                    bx.write_text(tms_local_xml(text, tdir, z, flip, bands=nbands + 1 if hole else None))
                    a4 = nb4 or hole                       # source has its own alpha: keep it, don't add one
                    if hole:                               # (a PNG with tRNS in a 3-band source is only transparent
                        ctx.log(f"  {n}: no-imagery tile(s): CPU warp with alpha")   # here; catalog PNG = 4-band)
                    try:
                        if not a4 and gpu_warp(ctx, bx, part, tdir, w, so, e, nn, s["res_m"], fmt):
                            checked = True                 # gpu_warp already decoded every block
                            break
                        run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, nn, "-t_srs",
                             "EPSG:3857", "-tr", s["res_m"], s["res_m"], "-tap", "-r", "average", "-ovr", "NONE",
                             "-wo", "NUM_THREADS=2", "-multi"] + tile_co(fmt) + ["-srcnodata", "None"] + ([] if a4 else ["-dstalpha"]) + [bx, part],
                            ctx, env={**os.environ, **gdal_env({"GDAL_HTTP_MAX_RETRY": "0"}), "GDAL_CACHEMAX": "512"})
                    except CliError:
                        bad = tms_scrub(tdir)
                        if not bad:
                            raise
                        raise CliError(E_FAIL, f"{bad} damaged tile(s) deleted, fetching again")
                    break
                except CliError as x:
                    if attempt == 2 or ABORT.is_set():
                        raise
                    ctx.log(f"{n}: retry {attempt + 1}/2 in ~45 s ({x.msg[-160:]})")
                if DRAIN.wait(random.uniform(30, 60)):
                    raise Aborted()
            if not checked and deep_decode(ctx, part, 4):
                raise CliError(E_FAIL, "bad tile")
            sz = part.stat().st_size; install_file(part, d / f"{n}.tif"); meter.add(sz)
            mb = sum(f.stat().st_size for f in tdir.glob("*/*/*")) / 1e6
            shutil.rmtree(tdir, ignore_errors=True)          # kept on stop/failure: a re-run resumes from it
            return n, None, f"{len(need)} tiles {mb:.0f} MB in {dl:.0f}s, warp {time.time() - t0 - dl:.0f}s"
        except (CliError, Aborted) as x:
            return n, x, ""
        except Exception as x:
            return n, CliError(E_FAIL, f"{type(x).__name__}: {x}"), ""
        finally:
            part.unlink(missing_ok=True); bx.unlink(missing_ok=True)

    it = iter(todo); run_ = set(); i = 0
    try:                                                   # tiles still to fetch: the ETA is measured on tiles (113)
        tset = {((d / "tiles") if raw else ctx.scratch() / f"{s['id']}-{nb[0]}.tiles", x, y) for nb in todo
                for x, y in tms_unit_tiles(plan, *map(float, nb[1]), margin=0 if raw else 1)}   # raw: shared tiles once
        tiles_need = sum(not (p / str(z) / str(y) / str(x)).is_file() for p, x, y in tset) \
            if len(tset) <= 300_000 else 0                 # bigger: stat-ing all would stall startup; units then
        del tset
    except Exception:
        tiles_need = 0
    try:
        with cf.ThreadPoolExecutor(U) as ex:
            while True:
                say_stop(ctx)
                if tiles_need:
                    prog.work(tf.tiles, tiles_need)
                while not DRAIN.is_set() and len(run_) < U:
                    nb = next(it, None)
                    if nb is None:
                        break
                    run_.add(ex.submit(one, nb[0], *map(str, nb[1])))
                if not run_:
                    break
                fin, _ = cf.wait(run_, timeout=5, return_when=cf.FIRST_COMPLETED)
                prog.tick(None, tiles_fetched=tf.tiles, mb_fetched=round(tf.bytes / 1e6), conns=tf.gate.limit(),
                          refusals=tf.gate.bad_n)
                for f in fin:
                    run_.discard(f); n, err, info = f.result()
                    if isinstance(err, Aborted):
                        continue
                    i += 1
                    g = tf.gate
                    if err:
                        failed.append(n); prog.tick(False)
                        ctx.log(f"[{i}/{len(todo)}] FAIL {n}: {err.msg}")
                    else:
                        prog.tick(True, conns=int(g.c))
                        ctx.log(f"[{i}/{len(todo)}] ok {n}  ({info}; conns {g.limit()}/{prof['max']}, refusals {g.bad_n})")
    finally:
        tf.close()
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    if not failed and not DRAIN.is_set():
        for c in list(own_glob(ctx, ctx.scratch(), s['id'], "*.cache")) + list(own_glob(ctx, ctx.scratch(), s['id'], "*.tiles")):
            shutil.rmtree(c, ignore_errors=True)
    res = {"failed": failed, "ok": not failed}
    if failed or DRAIN.is_set():
        res["hint"] = "re-run the same command; finished tiles are kept"
    return res


# ---- STAC + cloud COGs: the national 30m came from DEA's WMS, which renders every 512 px request on the fly
# (2 s at best, 25-90 s median under load: ~100 units/h, 11 h for Australia). The same product is public as COGs on S3
# (3200 px Albers tiles, one file per band, sha1 sidecar per tile), indexed by a STAC API. lmap downloads the bands,
# applies the WMS style itself (datacube-ows ComponentStyle: clip to scale_range, *255, truncate to uint8) and warps the
# same 1-degree units on the same EPSG:3857 grid, so caches, finish_out and the finished COG are unchanged.
# Catalog: kind="wms-tiles" + [source.X.stac] collection, datetime, bands=[r,g,b], scale=[lo,hi], nodata, href_map.

def stac_index(ctx, s):
    """{tile id: {band: https url}} for the source's collection/datetime/bbox, plus the tiles' lon/lat boxes.
    Cached per source (resume and re-runs don't re-query); --refresh re-queries."""
    st = s["stac"]; cache = STATE / f"{s['id']}.stac.json"
    if cache.is_file() and not getattr(ctx.a, "refresh", False):
        j = json.loads(cache.read_text())
        if j.get("key") == [st["collection"], st["datetime"], stac_key(s)]:
            return j["items"]
    items, pages = {}, 0
    todo = [f"{st['api']}/search?collections={st['collection']}&datetime={st['datetime']}"
            f"&bbox={','.join(map(str, b))}&limit=100" for b in src_boxes(s)]      # bboxes: one search per box
    while todo:
        url = todo.pop(0)
        page = json.loads(http_get(url, timeout=120)); pages += 1
        for f in page.get("features", []):                  # same item from two boxes: one entry (by id)
            hrefs = {}
            for b in st["bands"]:
                h = f["assets"][b]["href"]
                for a, z in st.get("href_map", {}).items():          # s3://bucket/ -> https://bucket.s3.../
                    h = h.replace(a, z)
                hrefs[b] = h
            sha = f["assets"].get("checksum:sha1", {}).get("href", "")
            for a, z in st.get("href_map", {}).items():
                sha = sha.replace(a, z)
            items[f["id"]] = {"bands": hrefs, "sha1": sha, "bbox": f["bbox"]}
        nxt = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
        if nxt:
            todo.insert(0, nxt)
        if pages > 500:
            raise CliError(E_UPSTREAM, "STAC search did not end after 500 pages")
    if not items:
        raise CliError(E_UPSTREAM, f"STAC search found nothing for {st['collection']} {st['datetime']}",
                       "check the [stac] section of the catalog entry")
    STATE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"key": [st["collection"], st["datetime"], stac_key(s)], "items": items}))
    return items


def stac_key(s):
    """Cache key part for the boxes: the bbox itself (unchanged for single-box caches), or the list of boxes."""
    return list(s["bbox"]) if s.get("bbox") else src_boxes(s)


def _sha1_of(p):
    import hashlib
    h = hashlib.sha1()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


class StacTiles:
    """Downloads + styles STAC tiles on demand, shared by all units (a tile feeds up to ~4 units). Each source tile
    becomes one styled 8-bit RGBA GTiff in scratch (<tile>.rgba.tif, ~4 MB); the band files are deleted after.
    Integrity: every band is checked against the published sha1 before use."""
    def __init__(self, ctx, s, items, meter):
        self.ctx, self.s, self.items, self.meter = ctx, s, items, meter
        self.d = ctx.scratch() / f"{s['id']}.stac"; self.d.mkdir(exist_ok=True)
        self.locks, self.lk, self.sums = {}, threading.Lock(), {}

    def _sums(self, tid):
        """{file name: sha1} from the tile's .sha1 sidecar (fetched once)."""
        if tid not in self.sums:
            txt = http_get(self.items[tid]["sha1"], timeout=60) if self.items[tid]["sha1"] else None
            self.sums[tid] = dict((ln.split()[1], ln.split()[0]) for ln in (txt or b"").decode().splitlines()
                                  if len(ln.split()) == 2)
        return self.sums[tid]

    def get(self, tid):
        """Path of the styled RGBA tile, building it if needed (thread-safe, one build per tile)."""
        out = self.d / f"{tid}.rgba.tif"
        with self.lk:
            lock = self.locks.setdefault(tid, threading.Lock())
        with lock:
            if out.is_file():
                return out
            st = self.s["stac"]; sums = self._sums(tid); paths = []
            for b in st["bands"]:
                url = self.items[tid]["bands"][b]; p = self.d / f"{tid}.{b}.tif"; name = url.rsplit("/", 1)[1]
                want = sums.get(name)
                if not want and not getattr(self, "_said_nosum", False):
                    self._said_nosum = True             # no checksum from the catalog: full decode check instead
                    self.ctx.log(f"  {self.s['id']}: no sha1 for {name} (sidecar missing/unreadable): decode-checking instead")
                if p.is_file() and want and _sha1_of(p) != want:
                    self.ctx.log(f"{name}: checksum mismatch in the cache, fetching it again"); p.unlink()
                if not p.is_file():
                    ok = http_get(url, p, timeout=120, meter=self.meter,
                                  check=(lambda q, w=want: _sha1_of(q) == w) if want else (lambda q: gdal_ok(q, deep=True)))
                    if ok is None:
                        raise CliError(E_UPSTREAM, f"{name}: 404 (listed by the STAC index)")
                paths.append(p)
            part = self.d / f"{tid}.rgba.part.tif"
            try:
                stac_style(paths, part, float(st["scale"][0]), float(st["scale"][1]), st.get("nodata"))
                os.replace(part, out)
            finally:
                part.unlink(missing_ok=True)
            for p in paths:
                p.unlink(missing_ok=True)
            return out


def stac_style(paths, out, lo, hi, nodata):
    """3 single-band rasters -> RGBA uint8, like datacube-ows ComponentStyleDef.compress_band:
    clip(lo, hi) -> (v - lo) / (hi - lo) * 255 -> astype(uint8) (truncates). Alpha 0 where any band is nodata.
    Float64 maths, like xarray. Row strips keep memory small."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    src = [gdal_open(p) for p in paths]; w, h = src[0].RasterXSize, src[0].RasterYSize
    o = gdal.GetDriverByName("GTiff").Create(str(out), w, h, 4, gdal.GDT_Byte,
                                             ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BLOCKXSIZE=512", "BLOCKYSIZE=512"])
    o.SetGeoTransform(src[0].GetGeoTransform()); o.SetProjection(src[0].GetProjection())
    o.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    for y in range(0, h, 512):
        n = min(512, h - y)
        a = [x.GetRasterBand(1).ReadAsArray(0, y, w, n).astype(np.float64) for x in src]
        ok = np.ones(a[0].shape, bool)
        if nodata is not None:
            for v in a:
                ok &= v != nodata
        for i, v in enumerate(a):
            o.GetRasterBand(i + 1).WriteArray(((np.clip(v, lo, hi) - lo) / (hi - lo) * 255).astype(np.uint8), 0, y)
        o.GetRasterBand(4).WriteArray(np.where(ok, 255, 0).astype(np.uint8), 0, y)
    o.FlushCache(); o = None


def stac_fetch(ctx, s, d, fmt, todo):
    """wms-tiles units built from STAC cloud COGs (see the comment above stac_index)."""
    import concurrent.futures as cf
    items = stac_index(ctx, s)
    host = urllib.parse.urlsplit(next(iter(items.values()))["bands"][s["stac"]["bands"][0]]).hostname or ""
    lim = host_cap(host)
    ctx.log(f"{s['id']}: {len(todo)} unit(s) from {len(items)} cloud tiles on {host} ({lim} connections)")
    meter, prog, failed = Meter(), Progress(s, len(todo), 0), []
    tiles = StacTiles(ctx, s, items, meter)
    def over(u, t):
        return not (t[2] <= u[0] or t[0] >= u[2] or t[3] <= u[1] or t[1] >= u[3])

    def one(n, w, so, e, nn):
        u = (float(w), float(so), float(e), float(nn))
        part = ctx.scratch() / f"{s['id']}-{n}.part.tif"; vrt = ctx.scratch() / f"{s['id']}-{n}.stac.vrt"
        try:
            need = [tid for tid, it in items.items() if over(u, it["bbox"])]
            srcs = [str(tiles.get(tid)) for tid in need]        # downloads + styles the tiles not yet built
            if ABORT.is_set():
                raise Aborted()
            if srcs:
                run(["gdalbuildvrt", "-q", "-overwrite", vrt] + srcs, ctx)
                run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, nn, "-t_srs", "EPSG:3857",
                     "-tr", s["res_m"], s["res_m"], "-tap", "-r", s["stac"].get("resampling", "average"), "-ovr", "NONE",
                     "-wo", "NUM_THREADS=2", "-multi"] + tile_co(fmt) + [vrt, part],
                    ctx, env={**os.environ, "GDAL_CACHEMAX": "512"})
            else:                                               # no cloud tile here (ocean): a fully transparent unit
                run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, nn, "-t_srs", "EPSG:3857",
                     "-tr", s["res_m"], s["res_m"], "-tap", "-ot", "Byte"] + tile_co(fmt) +
                    [blank_rgba(ctx), part], ctx)
            if not gdal_ok(part, deep=True):
                raise CliError(E_FAIL, "bad tile")
            install_file(part, d / f"{n}.tif"); return n, None, f"{len(need)} cloud tile(s)"
        except (CliError, Aborted) as x:
            return n, x, ""
        except Exception as x:
            return n, CliError(E_FAIL, f"{type(x).__name__}: {x}"), ""
        finally:
            part.unlink(missing_ok=True); vrt.unlink(missing_ok=True)

    # units in flight: a unit's tiles download one after another (3 bands each), so in-flight units ~ parallel
    # downloads. Measured 2026-10-14, 16 units / 1.46 GB from S3: 8 -> 229 s (10.7 MB/s), 16 -> 174 s (13.5 MB/s),
    # 24 -> 183 s (11.9 MB/s). Override: LMAP_STAC_UNITS.
    U = int(os.environ.get("LMAP_STAC_UNITS") or max(2, min(16, lim)))
    it = iter(todo); run_ = set(); i = 0; t0 = time.time()
    with cf.ThreadPoolExecutor(U) as ex:
        while True:
            say_stop(ctx)
            while not DRAIN.is_set() and len(run_) < U:
                nb = next(it, None)
                if nb is None:
                    break
                run_.add(ex.submit(one, nb[0], *map(str, nb[1])))
            if not run_:
                break
            fin, _ = cf.wait(run_, timeout=5, return_when=cf.FIRST_COMPLETED)
            prog.tick(None, mb_fetched=round(meter.bytes / 1e6))
            for f in fin:
                run_.discard(f); n, err, info = f.result()
                if isinstance(err, Aborted):
                    continue
                i += 1
                if err:
                    failed.append(n); prog.tick(False); ctx.log(f"[{i}/{len(todo)}] FAIL {n}: {err.msg}")
                else:
                    prog.tick(True)
                    ctx.log(f"[{i}/{len(todo)}] ok {n}  ({info}; {meter.bytes / 1e6 / max(1, time.time() - t0):.1f} MB/s)")
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    if not failed and not DRAIN.is_set():
        shutil.rmtree(tiles.d, ignore_errors=True)
    res = {"failed": failed, "ok": not failed}
    if failed or DRAIN.is_set():
        res["hint"] = "re-run the same command; finished units and styled tiles are kept"
    return res


_BLANK_LOCK = threading.Lock()


def blank_rgba(ctx):
    """A 1x1 world-covering fully transparent RGBA raster (source for units with no data, e.g. open ocean).
    Locked + a per-process temp name: ocean units start together and all ask for it at once (0.12.6 raced here)."""
    p = ctx.scratch() / "blank-rgba.tif"
    with _BLANK_LOCK:
        if p.is_file():
            return p
        from osgeo import gdal
        gdal.UseExceptions()
        tmp = f"{p}.{os.getpid()}.part"
        o = gdal.GetDriverByName("GTiff").Create(tmp, 1, 1, 4, gdal.GDT_Byte)
        o.SetGeoTransform([-20037508.342789244, 40075016.68557849, 0, 20037508.342789244, 0, -40075016.68557849])
        from osgeo import osr
        sr = osr.SpatialReference(); sr.ImportFromEPSG(3857); o.SetProjection(sr.ExportToWkt())
        o.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
        for b in range(1, 5):
            o.GetRasterBand(b).Fill(0)
        o = None; os.replace(tmp, p)
    return p


def wms_xml(ctx, s, drop):
    """The source's GDAL_WMS xml with `drop` removed and no <UserAgent>: a UserAgent in the xml beats
    GDAL_HTTP_USERAGENT, so without this an LMAP_UA / ua= override would not reach GDAL."""
    return re.sub(drop + r"|<UserAgent>.*?</UserAgent>", "", ctx.path(s["xml"]).read_text(), flags=re.S)


ARCGIS_SVC = re.compile(r"^(https?://[^?#]*?/(?:MapServer|ImageServer))(?:[/?#]|$)", re.I)
_EXPORT_LOCK = threading.Lock()
_EXPORT_TERMS = {}


def arcgis_export_terms(url):
    """An ArcGIS tile service's own export permission (Esri: exportTilesAllowed = the owner's
    "Allow clients to export cache tiles"). Returns None if url is not an ArcGIS Map/ImageServer, else
    (allowed, max_tiles) with allowed True/False, or None when the service didn't say (never blocks a fetch)."""
    m = ARCGIS_SVC.match(url or "")
    if not m:
        return None
    svc = m.group(1)
    if svc not in _EXPORT_TERMS:
        t = (None, None)
        try:     # ONE plain request, 10 s cap: no retries, backoff or outage wait (a log line never delays a fetch)
            rq = urllib.request.Request(svc + "?f=json", headers={"User-Agent": UA})
            with host_slot(svc), OPENER.open(rq, timeout=10) as r:
                j = json.loads(r.read(1 << 20))
            if isinstance(j, dict):
                a = j.get("exportTilesAllowed"); mx = j.get("maxExportTilesCount")
                t = (a if isinstance(a, bool) else None, mx if isinstance(mx, int) and not isinstance(mx, bool) else None)
        except (OSError, ValueError, http.client.HTTPException):
            pass
        with _EXPORT_LOCK:
            _EXPORT_TERMS.setdefault(svc, t)
    return _EXPORT_TERMS[svc]


def log_export_terms(ctx, s, url):
    t = arcgis_export_terms(url)
    if t is None:
        return
    a, mx = t
    if a:
        ctx.log(f"{s['id']}: the ArcGIS service allows offline tile export (exportTilesAllowed, max {mx} per export)")
    elif a is False:
        ctx.log(f"{s['id']}: the ArcGIS service does not enable offline tile export; fetching tiles politely "
                f"(capped connections, backoff). Check the service terms before large pulls")
    else:
        ctx.log(f"{s['id']}: could not read the ArcGIS service's export permission; fetching tiles politely")


def wmst_fetch(ctx, s, plan):
    """Big WMS/TMS pull in 1-degree tiles (resumable: done tiles are skipped), then .vrt + COG.
    Tiles are warped straight from the tile server; the xml's disk cache is stripped (it would double the size)."""
    need_tool("gdalwarp", "gdalbuildvrt", "gdal_translate")
    d = ctx.path(s["dest"]); cf_ = cache_fmt(d)
    todo = [(n, b) for n, b in wmst_tiles(s) if not unit_file(d, n, cf_).is_file()]
    if plan:
        return {"would_fetch_tiles": len(todo), "into": str(d), "approx_gb": round(len(todo) / max(1, sum(1 for _ in wmst_tiles(s))) * float(s["est_gb"]), 1)
                if s.get("est_gb") else round(len(todo) * 0.03, 1)}
    d.mkdir(parents=True, exist_ok=True)
    if s.get("stac") and s.get("fetch", "lmap") != "gdal":
        return stac_fetch(ctx, s, d, check_manifest(s, d, ctx.quality == "original"), todo)            # cloud COGs: no render server in the way
    xml = ctx.scratch() / f"{s['id']}.xml"
    # no disk cache (it would double the size) and never turn a failed request into a silent blank block
    xml.write_text(wms_xml(ctx, s, r"<Cache>.*?</Cache>|<ZeroBlockOnServerException>.*?</ZeroBlockOnServerException>"))
    random.shuffle(todo)                                     # random order: spreads load across the server's cache tiles
    import concurrent.futures as cf
    text = xml.read_text(); m = re.search(r"<ServerUrl>([^<]+)", text)
    if not m:
        raise CliError(E_USAGE, f"{s['xml']}: no <ServerUrl>")
    if "<MaxConnections>" not in text:
        text = text.replace("</GDAL_WMS>", "<MaxConnections>2</MaxConnections></GDAL_WMS>")
    host = urllib.parse.urlsplit(m.group(1)).hostname or ""
    own = (re.search(r'<Service name="TMS"', text) and all(k in m.group(1) for k in ("${z}", "${x}", "${y}"))
           and s.get("fetch", "lmap") != "gdal" and tms_plan(text, s["res_m"]) is not None)
    fmt = check_manifest(s, d, ctx.quality == "original", raw_ok=bool(own))
    if todo:
        log_export_terms(ctx, s, m.group(1))                 # the service owner's export permission
    if fmt == "raw" and not own:
        raise CliError(E_FAIL, f"{d} holds server tiles (raw) but {s['id']} is no longer a plain TMS lmap downloads",
                       f"move/delete {d} to start over (or put the catalog back)")
    if own:
        return tms_fetch(ctx, s, d, fmt, todo, text)     # lmap downloads; raw: kept as is, else warped per unit
    lv = re.search(r"<TileLevel>(\d+)</TileLevel>", text)
    if lv and re.search(r'<Service name="(TMS|TiledWMS)"', text):
        # Reading the BASE level of a TMS, GDAL fetches tiles one at a time (measured: EOX z14 1 deg 120 s);
        # read as an overview of a declared level+1 it fetches in parallel (7 s, identical pixels)
        base = 2 * 20037508.342789244 / 256 / 2 ** int(lv.group(1))
        if float(s["res_m"]) >= base * 0.999:
            text = text.replace(lv.group(0), f"<TileLevel>{int(lv.group(1)) + 1}</TileLevel>", 1)
    # GDAL fetches the tiles itself: each block (gdalwarp) gets `per` parallel requests, the Tuner runs more blocks
    # while that raises throughput, and the host budget (shared with every other lmap job) caps the server total
    lim = host_cap(host); per = lim if lim < 4 else max(2, min(8, lim // 2))
    budget = HostBudget(host, lim)
    meter, tun = Meter(), Tuner(max(1, lim // per), window=90, min_units=2)
    prog = Progress(s, len(todo), 0); failed = []
    http = gdal_config(gdal_env({"GDAL_HTTP_VERSION": "2TLS", "GDAL_HTTP_USERAGENT": UA}))
    def one(n, w, so, e, nn):
        part = ctx.scratch() / f"{s['id']}-{n}.part.tif"; bx = ctx.scratch() / f"{s['id']}-{n}.xml"
        cache = ctx.scratch() / f"{s['id']}-{n}.cache"     # per block: a retry re-uses what already arrived
        done = None
        try:
            if ABORT.wait(random.uniform(0, 3)):               # stagger starts: no synchronised bursts
                raise Aborted()
            for attempt in range(3):
                fds = budget.take(per, ABORT)
                if not fds:
                    raise Aborted()
                try:
                    bx.write_text(re.sub(r"<BandsCount>\s*([13])\s*</BandsCount>", lambda m_: f"<BandsCount>{int(m_.group(1)) + 1}</BandsCount>",
                                         re.sub(r"<MaxConnections>\d+</MaxConnections>",
                                         f"<MaxConnections>{len(fds)}</MaxConnections>", text))
                                  .replace("</GDAL_WMS>", f"<Cache><Path>{cache}</Path></Cache></GDAL_WMS>"))
                    run(["gdalwarp", "-q", "-overwrite", "-te_srs", "EPSG:4326", "-te", w, so, e, nn, "-t_srs",
                         "EPSG:3857", "-tr", s["res_m"], s["res_m"], "-tap", "-r", "average"] + tile_co(fmt) +
                        ["-srcnodata", "None"] + http + [bx, part], ctx,      # alpha comes from the xml (zero block = 0)
                        env={**os.environ, "GDAL_CACHEMAX": "512"})
                    break
                except CliError as x:
                    if attempt == 2 or ABORT.is_set():
                        raise
                    ctx.log(f"{n}: retry {attempt + 1}/2 in ~45 s ({x.msg[-120:]})")
                finally:
                    HostBudget.give(fds)
                if DRAIN.wait(random.uniform(30, 60)):    # `lmap stop` ends a retry wait (as tms_fetch); ABORT sets DRAIN too
                    raise Aborted()
            meter.add(part.stat().st_size)
            done = part; return n, None
        except (CliError, Aborted) as x:
            return n, x
        except Exception as x:
            return n, CliError(E_FAIL, f"{type(x).__name__}: {x}")
        finally:
            bx.unlink(missing_ok=True)
            if done is None:
                part.unlink(missing_ok=True)
    def ver(n):
        # the full decode runs on its own pool: block threads only fetch + warp, so the Tuner times the server
        part = ctx.scratch() / f"{s['id']}-{n}.part.tif"
        try:
            if not gdal_ok(part, deep=True):
                return n, CliError(E_FAIL, "bad tile")
            install_file(part, d / f"{n}.tif")
            shutil.rmtree(ctx.scratch() / f"{s['id']}-{n}.cache", ignore_errors=True)   # kept on stop/failure: resumes
            return n, None
        except (CliError, Aborted) as x:
            return n, x
        except Exception as x:
            return n, CliError(E_FAIL, f"{type(x).__name__}: {x}")
        finally:
            part.unlink(missing_ok=True)
    it = iter(todo); run_ = set(); chk = set(); i = 0
    with cf.ThreadPoolExecutor(tun.cap) as ex, cf.ThreadPoolExecutor(max(1, ctx.shrink)) as vx:
        while True:
            say_stop(ctx)
            # finished blocks wait in scratch for their check: at most 2 per checker, so a slow decode holds back
            # the downloads instead of filling the disk
            while not DRAIN.is_set() and len(run_) < tun.c and len(chk) < 2 * max(1, ctx.shrink):
                nb = next(it, None)
                if nb is None:
                    break
                run_.add(ex.submit(one, nb[0], *nb[1]))
            tun.busy(len(run_)); tun.tick(meter.bytes)
            if not run_ and not chk:
                break
            fin, _ = cf.wait(run_ | chk, timeout=5, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                checked = f in chk
                (chk if checked else run_).discard(f); n, err = f.result()
                if isinstance(err, Aborted):
                    if not checked:
                        continue
                    ctx.scratch().joinpath(f"{s['id']}-{n}.part.tif").unlink(missing_ok=True)
                    continue
                if not checked:
                    tun.unit(not err)
                    if not err:
                        chk.add(vx.submit(ver, n)); continue
                i += 1
                if err:
                    failed.append(n); prog.tick(False)
                    if checked:
                        tun.unit(False)                    # corrupt under load: the server is unhappy too
                    ctx.log(f"[{i}/{len(todo)}] FAIL {n}: {err.msg}  (blocks x{tun.c})")
                else:
                    prog.tick(True, conns=tun.c * per)
                    ctx.log(f"[{i}/{len(todo)}] ok {n}  (blocks x{tun.c}, conns <={min(lim, tun.c * per)}/{lim})")
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    if not failed and not DRAIN.is_set():                  # all units done: drop leftover unit caches
        for c in own_glob(ctx, ctx.scratch(), s['id'], "*.cache"):
            shutil.rmtree(c, ignore_errors=True)
    res = {"failed": failed, "ok": not failed}
    if failed or DRAIN.is_set():
        res["hint"] = "re-run the same command; finished tiles are kept"
    return res


S2WC = "https://esa-worldcover-s2.s3.eu-central-1.amazonaws.com"


def s2wc_tiles(ctx, s):
    """Land tiles in the box, from the bucket listing (ocean tiles don't exist). Cached in dest/_tiles.txt."""
    d = ctx.path(s["dest"]); idx = d / "_tiles.txt"
    if idx.is_file():
        return idx.read_text().split()
    import math
    boxes = src_boxes(s); keys = []
    # tile S{la}E{lo} covers lat -la..-la+1, lon lo..lo+1 (S10 = tile with top edge -9). Same tiles as the old
    # integer-only arithmetic for whole-degree boxes; a fractional box (dev catalog) now finds its tile too.
    lats = sorted({la for w, so, e, n in boxes for la in range(-math.ceil(n) + 1, -math.floor(so) + 1)})
    for la in lats:
        x = http_get(f"{S2WC}/?list-type=2&prefix=rgbnir/2021/S{la:02d}/").decode()
        for k in re.findall(r"<Key>([^<]+_S(\d\d)E(\d\d\d)_S2RGBNIR\.tif)</Key>", x):
            if any(so < -la + 1 and n > -la and w < int(k[2]) + 1 and int(k[2]) < e for w, so, e, n in boxes):
                keys.append(k[0])
    d.mkdir(parents=True, exist_ok=True); idx.write_text("\n".join(keys) + "\n")
    return keys


def s2wc_name(k):
    return re.search(r"_(S\d\dE\d\d\d)_", k).group(1) + ".tif"


def s2wc_status(ctx, s):
    d = ctx.path(s["dest"])
    if not (d / "_tiles.txt").is_file():
        return {"items_total": None, "items_have": 0, "items_todo": None, "note": "fetch lists the tiles first"}
    t = s2wc_tiles(ctx, s); have = sum((d / s2wc_name(k)).is_file() for k in t)
    return {"items_total": len(t), "items_have": have, "items_todo": len(t) - have}


def s2wc_fetch(ctx, s, plan):
    """ESA WorldCover Sentinel-2 2021 composite (10m, 4-band 16-bit) -> 8-bit RGB JPEG tiles, then vrt + COG.
    Only R,G,B are kept: infrared dropped, 16->8 bit, lossless->JPEG (that's the ~15x shrink)."""
    need_tool("gdal_translate", "gdalbuildvrt")
    d = ctx.path(s["dest"]); keys = s2wc_tiles(ctx, s)
    todo = [k for k in keys if not (d / s2wc_name(k)).is_file()]
    if plan:
        return {"would_fetch_tiles": len(todo), "of": len(keys), "into": str(d), "approx_download_gb": round(len(todo) * 0.55)}
    random.shuffle(todo)                                     # random order: spreads load across the server's cache tiles
    import concurrent.futures as cf
    prog = Progress(s, len(keys), len(keys) - len(todo)); failed = []
    meter, tun, sn = Meter(), Tuner(s.get("max_conns", 8) if ctx.jobs == 3 else ctx.jobs), ctx.shrink   # S3 copes; the Tuner stops adding when it stops helping
    def dl(k):
        raw = ctx.scratch() / f"{s['id']}-{s2wc_name(k)[:-4]}-raw.tif"
        try:
            if not http_get(f"{S2WC}/{k}", raw, timeout=300, meter=meter, check=gdal_ok):
                raise CliError(E_FAIL, "404")
            return k, raw, None
        except (CliError, Aborted) as e:
            return k, raw, e
        except Exception as e:
            return k, raw, CliError(E_FAIL, f"{type(e).__name__}: {e}")
    def conv(k, raw):
        part = ctx.scratch() / f"{s['id']}-{s2wc_name(k)[:-4]}.part.tif"
        try:
            # 0 = no data; reflectance 1..3000 -> 1..255, gamma 0.7 (outback soil not blown out) (shadows readable)
            run(["gdal_translate", "-q", "--config", "GDAL_CACHEMAX", "512", "--config", "GDAL_NUM_THREADS", "2",
                 "-b", "1", "-b", "2", "-b", "3",
                 "-ot", "Byte", "-scale", "1", "3000", "1", "255", "-exponent", "0.7", "-a_nodata", "none",
                 "-mask", "1", "-colorinterp", "red,green,blue", "-a_scale", "1", "-a_offset", "0",
                 "-of", "COG", *photo_co(ctx), raw, part], ctx)
            if not gdal_ok(part, deep=True):
                raise CliError(E_FAIL, "bad output")
            install_file(part, d / s2wc_name(k)); return k, None
        except (CliError, Aborted) as e:
            return k, e
        except Exception as e:
            return k, CliError(E_FAIL, f"{type(e).__name__}: {e}")
        finally:
            raw.unlink(missing_ok=True); part.unlink(missing_ok=True)
    it, pend, downloading, converting, ready, n = iter(todo), None, set(), set(), [], 0
    left = 0
    try:
     with cf.ThreadPoolExecutor(tun.cap) as dx, cf.ThreadPoolExecutor(sn) as sx:
        while True:
            while ready and len(converting) < sn and not ABORT.is_set():
                converting.add(sx.submit(conv, *ready.pop(0)))
            say_stop(ctx)
            low = free_bytes(ctx.scratch()) <= 8e9                # each raw tile is up to ~1 GB
            while not DRAIN.is_set() and len(downloading) < tun.c and len(ready) < sn and not low:
                k = pend or next(it, None); pend = None
                if k is None:
                    break
                downloading.add(dx.submit(dl, k))
            tun.busy(len(downloading)); tun.tick(meter.bytes)
            if not downloading and not converting and (not ready or ABORT.is_set()):
                if low and not DRAIN.is_set():               # out of scratch: report, don't fake "done"
                    left = sum(1 for _ in it)
                break
            fin, _ = cf.wait(downloading | converting, timeout=5, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                if f in downloading:
                    downloading.discard(f); k, raw, err = f.result()
                    if err or ABORT.is_set():
                        raw.unlink(missing_ok=True); Path(str(raw) + ".part").unlink(missing_ok=True)
                        if err and not isinstance(err, Aborted):
                            failed.append(k); prog.tick(False); ctx.log(f"FAIL {s2wc_name(k)}: {err.msg}")
                    else:
                        ready.append((k, raw))
                else:
                    converting.discard(f); k, err = f.result(); n += 1
                    if err and not isinstance(err, Aborted):
                        failed.append(k); prog.tick(False); ctx.log(f"FAIL {s2wc_name(k)}: {err.msg}")
                    elif not err:
                        prog.tick(True, conns=tun.c)
                        ctx.log(f"[{n}/{len(todo)}] ok {s2wc_name(k)} ({tun.c} conns, "
                                f"{(tun.rate or 0) / 1e6:.0f} MB/s)")
    finally:
        for x in own_glob(ctx, ctx.scratch(), s['id'], "S*"):          # nothing half-done left in scratch
            x.unlink(missing_ok=True)
    if left:
        prog.write(final="failed")
        return {"ok": False, "not_started": left, "hint": f"scratch {ctx.scratch()} low on space; free some and re-run"}
    prog.write(final="stopped" if DRAIN.is_set() else ("failed" if failed else "done"))
    if not failed and not DRAIN.is_set():                  # all units done: drop leftover unit caches
        for c in own_glob(ctx, ctx.scratch(), s['id'], "*.cache"):
            shutil.rmtree(c, ignore_errors=True)
    res = {"failed": failed, "ok": not failed}
    if failed or DRAIN.is_set():
        res["hint"] = "re-run the same command; finished tiles are kept"
    return res


CKAN = "https://www.data.qld.gov.au/api/3/action"


def tides_status(ctx, s):
    d = ctx.path(s["dest"]); idx = d / "_files.json"
    if not idx.is_file():
        return {"items_total": None, "items_have": 0, "items_todo": None, "note": "fetch lists the files first"}
    fs = json.loads(idx.read_text()); have = sum((d / n).is_file() for n in fs)
    return {"items_total": len(fs), "items_have": have, "items_todo": len(fs) - have}


def tides_fetch(ctx, s, plan):
    """MSQ predicted tides (QLD Open Data, CC BY 4.0): high/low + 10-min CSVs per gauge for the wanted years.
    Station list comes from the open data catalogue; nothing is scraped."""
    d = ctx.path(s["dest"]); d.mkdir(parents=True, exist_ok=True)
    years = [str(y) for y in s.get("years") or [time.localtime().tm_year, time.localtime().tm_year + 1]]
    kinds = s.get("kinds", ["high-low"])
    files = {}
    for k in kinds:
        q = json.loads(http_get(f"{CKAN}/package_search?q=name:*tide-gauge-predicted-{k}-data&rows=500"))["result"]
        for pk in q["results"]:
            for r in pk["resources"]:
                u = r.get("url") or ""
                m = re.search(r"_(20\d\d)_(hilo|10min)s?\.csv$", u, re.I)
                if m and m.group(1) in years:
                    files[Path(urllib.parse.urlsplit(u).path).name.lower()] = (u, r.get("id"))
    if not files:
        raise CliError(E_UPSTREAM, "no tide files found in the QLD open data catalogue")
    (d / "_files.json").write_text(json.dumps(sorted(files)))
    todo = {n: v for n, v in files.items() if not (d / n).is_file()}
    if plan:
        return {"would_fetch": len(todo), "of": len(files), "years": years, "kinds": kinds, "into": str(d)}
    failed = []
    for i, (n, (u, rid)) in enumerate(sorted(todo.items()), 1):
        say_stop(ctx)
        if DRAIN.is_set():
            break
        try:
            if http_get(u, d / n, check=lambda p: p.read_bytes()[:40].startswith(b"Tidal readings")) is None:
                raise CliError(E_UPSTREAM, "404")
        except CliError as e:
            # 2026-10: the 2027 files' download links are 404 but the same rows are in the catalogue's datastore
            try:
                tides_from_datastore(d, n, rid)
                ctx.log(f"{n}: download link broken ({e.msg}); rebuilt from the data.qld datastore")
            except (CliError, OSError, ValueError, KeyError) as e2:
                failed.append(n); ctx.log(f"FAIL {n}: {e.msg}; datastore: {getattr(e2, 'msg', e2)}")
        if i % 20 == 0:
            ctx.log(f"  {i}/{len(todo)}")
    res = {"files": len(files), "fetched": len(todo) - len(failed), "failed": failed}
    if not failed and not DRAIN.is_set():
        res["index"] = str(tides_index(d, ctx.scratch()))
    return res


def tides_from_datastore(d, n, rid):
    """Write MSQ CSV n from the CKAN datastore copy of resource rid. The station header (name, position, datum)
    comes from the same gauge's file for another year - the datastore has only the readings."""
    if not rid:
        raise ValueError("no resource id")
    j = json.loads(http_get(f"{CKAN}/datastore_search?resource_id={rid}&limit=32000"))["result"]
    recs = j["records"]
    if not recs or len(recs) < j.get("total", 0):
        raise ValueError(f"datastore gave {len(recs)} of {j.get('total')} rows")
    gauge = n.split("_")[0]
    sib = next((f for f in sorted(d.glob(f"{gauge}_*_hilo.csv")) if f.name != n), None)
    if not sib:
        raise ValueError("no other year of this gauge to take the header from")
    head = []
    for line in sib.read_text(errors="replace").splitlines():
        if re.match(r"\d\d/\d\d/\d{4}", line):
            break
        if "=," in line and line.split("=,")[0].strip() in ("Tidal Station Number", "Tidal Station Name",
                "Geographical Quadrant North/East", "Latitude  Degrees Minutes", "Longitude Degrees Minutes",
                "Tidal Heights", "Time Zone", "Station Datum"):
            head.append(line)
    rows = [f"{r['Date']} , {r['Time']} , {int(r['Ind']):2d} , {float(r['Reading']):7.3f}" for r in recs]
    part = d / f".{n}.part"
    part.write_text("Tidal readings rebuilt by lmap from the data.qld.gov.au datastore (download link was broken)\n"
                    "IDENTIFICATION\n" + "\n".join(head) + "\nREADINGS\nData Type is Prediction   =,P\n"
                    + "\n".join(rows) + "\n")
    if len(tide_rows(part)) != len(recs):
        part.unlink(missing_ok=True); raise ValueError("rebuilt file did not parse")
    part.replace(d / n)


def tides_index(d, tmp=None):
    """stations.gpkg: one point per gauge with name, file and the next few high/low tides as text. Built in tmp
    (lmap scratch; GeoPackage can't lock on CIFS), then installed."""
    import csv
    feats = []
    for f in sorted(d.glob("*_hilo.csv")):
        meta = {}
        for line in f.read_text(errors="replace").splitlines()[:40]:
            if "=," in line:
                k, _, v = line.partition("=,"); meta[k.strip()] = v.strip(" ,\r")
        rows = [(t.strftime("%Y-%m-%d %H:%M"), h) for t, h, _ in tide_rows(f)]
        try:
            la = meta["Latitude  Degrees Minutes"].split(); lo = meta["Longitude Degrees Minutes"].split()
            lat = float(la[0]) - float(la[1]) / 60 if la[0].startswith("-") else float(la[0]) + float(la[1]) / 60
            lon = float(lo[0]) + float(lo[1]) / 60
        except (KeyError, ValueError, IndexError):
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"station": meta.get("Tidal Station Name", f.stem).title(), "file": f.name,
                                     "datum": "LAT (lowest astronomical tide)", "time_zone": "AEST (UTC+10), no DST",
                                     "readings": len(rows), "first": rows[0][0] if rows else None,
                                     "last": rows[-1][0] if rows else None}})
    gj = d / "_stations.geojson"; gj.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    out = d / "stations.gpkg"; part = Path(tmp or tempfile.mkdtemp()) / f"lmap-tides-{os.getpid()}.gpkg"
    part.unlink(missing_ok=True)
    try:
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-nln", "tide_stations", str(part), str(gj)], check=True,
                       capture_output=True)
        install_file(part, out)
    finally:
        part.unlink(missing_ok=True); gj.unlink(missing_ok=True)
    return out


def tide_rows(f):
    """MSQ hilo CSV rows: 'dd/mm/yyyy , hh:mm , ind , height' (ind 1 = high water, -1 = low)."""
    import datetime as dt
    out = []
    for line in Path(f).read_text(errors="replace").splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 4 and re.fullmatch(r"\d\d/\d\d/\d{4}", p[0]):
            try:
                out.append((dt.datetime.strptime(p[0] + " " + p[1], "%d/%m/%Y %H:%M"), float(p[3]), int(p[2])))
            except ValueError:
                pass
    return out


def tide_table(f, days=7, start=None):
    """Next high/low tides: [(time text, height m, 'HIGH'/'low')]. Times are AEST (station local, no DST)."""
    import datetime as dt
    t0 = start or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=10)
    return [(t.strftime("%a %d %b %H:%M"), h, "HIGH" if ind > 0 else "low")
            for t, h, ind in tide_rows(f) if t0 <= t <= t0 + dt.timedelta(days=days)]

def cmd_tides(ctx):
    """Offline tide table for the nearest gauge (or a named one)."""
    need_root(ctx)
    s = ctx.src("qld-tides"); d = ctx.path(s["dest"])
    files = sorted(d.glob("*_hilo.csv"))
    if not files:
        raise CliError(E_NOTFOUND, "no tide files yet", "lmap data fetch qld-tides --yes")
    import datetime as dt
    start = dt.datetime.strptime(ctx.a.date, "%Y-%m-%d") if ctx.a.date else None
    yr = str(start.year if start else time.localtime().tm_year)
    stations = {}
    for f in files:
        txt = f.read_text(errors="replace")[:1500].replace("\r", "")
        nm = re.search(r"Tidal Station Name\s*=,(.*)", txt); la = re.search(r"Latitude  Degrees Minutes\s*=,(.*)", txt)
        lo = re.search(r"Longitude Degrees Minutes\s*=,(.*)", txt)
        if not (nm and la and lo):
            continue
        a, b = la.group(1).split()[:2]; c, e = lo.group(1).split()[:2]
        lat = float(a) - float(b) / 60; lon = float(c) + float(e) / 60
        stations.setdefault(nm.group(1).strip().title(), []).append((f, lat, lon))
    if ctx.a.station:
        hit = [k for k in stations if ctx.a.station.lower() in k.lower()]
        if not hit:
            raise CliError(E_NOTFOUND, f"no station matching {ctx.a.station!r}", "lmap tides --list")
        # prefer gauges with this year's file, then plain names over offshore/storm-surge ones
        name = min(hit, key=lambda k: (not any(f"_{yr}_" in x[0].name for x in stations[k]),
                                       any(w in k.lower() for w in ("storm", "offshore", "beacon")), len(k)))
    elif ctx.a.near:
        lat, lon = [float(v) for v in ctx.a.near.split(",")]
        import math
        cand = [k for k in stations if any(f"_{yr}_" in x[0].name for x in stations[k])] or list(stations)
        name = min(cand, key=lambda k: math.hypot(stations[k][0][1] - lat,
                                                  (stations[k][0][2] - lon) * math.cos(math.radians(lat))))
    elif ctx.a.list:
        return {"stations": sorted(stations)}
    else:
        raise CliError(E_USAGE, "say where", "--near=LAT,LON or --station NAME (or --list)")
    fs = sorted(stations[name], key=lambda x: x[0].name)
    f = next((x[0] for x in fs if f"_{yr}_" in x[0].name), fs[-1][0])
    rows = tide_table(f, ctx.a.days, start)
    if len(rows) < ctx.a.days and len(fs) > 1:      # spill into next year's file
        nxt = next((x[0] for x in fs if f"_{int(yr) + 1}_" in x[0].name), None)
        if nxt:
            rows += tide_table(nxt, ctx.a.days, start)
    if not rows:
        have = sorted({re.search(r"_(20\d\d)_", x[0].name).group(1) for x in fs})
        raise CliError(E_NOTFOUND, f"no {name} predictions for that date (have: {', '.join(have)})",
                       "MSQ publishes next year's high/low files late in the year: re-run "
                       "`lmap data fetch qld-tides --yes` then")
    return {"station": name, "datum": "metres above LAT (lowest astronomical tide)", "time": "AEST UTC+10",
            "tides": [{"when": w, "m": h, "type": t} for w, h, t in rows],
            "source": "Maritime Safety Queensland predictions (CC BY 4.0). Predictions only: wind/pressure/floods "
                      "change real levels; storm surge can add metres."}


def do_fetch(ctx, s, plan):
    """Fetch a source. Sources with `out`: tiles into the tile cache, then one COG at out on a network share."""
    o = ctx.path(s["out"]) if s.get("out") else None
    if o and o.is_file() and not getattr(ctx.a, "refresh", False):
        return {"unchanged": True, "out": str(o)}
    res = KINDS[s["kind"]][1](ctx, s, plan) or {}
    if o and plan:
        res["out"] = str(o)
    elif o and not res.get("failed") and res.get("ok") is not False and not DRAIN.is_set() and not ABORT.is_set():
        res.update(finish_out(ctx, s))
    return res


WM = 20037508.342789244                                   # web-mercator half width (the tile grid origin)


def raw_vrt(ctx, s, d, units, out):
    """A raw cache's done units as one VRT over d/tiles.xml (the server tiles, read in place): each unit is a
    pixel window of the fetch level, so pixels are the server's own, on the server's own grid (no resampling).
    When res_m is clearly coarser than that grid, one warp (average) to res_m on the usual -tap grid; the
    COG encode after it stays the only lossy step. Returns the path to build from."""
    lx = d / "tiles.xml"
    from xml.sax.saxutils import escape
    su = f"<ServerUrl>file://{escape(str(d / 'tiles'))}/${{z}}/${{y}}/${{x}}</ServerUrl>"   # the root may have moved
    text = re.sub(r"<ServerUrl>[^<]*</ServerUrl>", lambda _: su, lx.read_text())
    tmp = lx.with_name("tiles.xml.tmp"); tmp.write_text(text); os.replace(tmp, lx)
    z = int(re.search(r"<TileLevel>(\d+)</TileLevel>", text).group(1)); nx = 2 ** z
    bs = int(re.search(r"<BlockSizeX>(\d+)</BlockSizeX>", text).group(1))
    nb = int(re.search(r"<BandsCount>(\d+)</BandsCount>", text).group(1))
    box = raw_boxes(d, units, z)
    X0, Y0 = min(b[0] for b in box.values()), min(b[1] for b in box.values())
    X1, Y1 = max(b[2] for b in box.values()), max(b[3] for b in box.values())
    W, H, px = (X1 - X0 + 1) * bs, (Y1 - Y0 + 1) * bs, 2 * WM / (nx * bs)
    ci = {1: ["Gray"], 2: ["Gray", "Alpha"], 3: ["Red", "Green", "Blue"], 4: ["Red", "Green", "Blue", "Alpha"]}[nb]
    o_ = [f'<VRTDataset rasterXSize="{W}" rasterYSize="{H}"><SRS>EPSG:3857</SRS>',
          f"<GeoTransform>{-WM + X0 * bs * px!r}, {px!r}, 0, {WM - Y0 * bs * px!r}, 0, {-px!r}</GeoTransform>"]
    for b in range(1, nb + 1):
        o_.append(f'<VRTRasterBand dataType="Byte" band="{b}"><ColorInterp>{ci[b - 1]}</ColorInterp>')
        for n, (x0, y0, x1, y1) in sorted(box.items()):
            w_, h_ = (x1 - x0 + 1) * bs, (y1 - y0 + 1) * bs
            o_.append(f'<SimpleSource><SourceFilename relativeToVRT="0">{escape(str(lx))}</SourceFilename>'
                      f"<SourceBand>{b}</SourceBand><SrcRect xOff=\"{x0 * bs}\" yOff=\"{y0 * bs}\" xSize=\"{w_}\" "
                      f'ySize="{h_}"/><DstRect xOff="{(x0 - X0) * bs}" yOff="{(y0 - Y0) * bs}" xSize="{w_}" ySize="{h_}"/>'
                      "</SimpleSource>")
        o_.append("</VRTRasterBand>")
    o_.append("</VRTDataset>")
    out.write_text("\n".join(o_))
    if abs(px - float(s["res_m"])) <= float(s["res_m"]) * 1e-3:   # the server's grid is the asked one: as is
        return out
    # one resample to res_m (average, like before) into a LOSSLESS GTiff (the COG encode stays the only lossy
    # step). Not a warped VRT: the COG build then loses the mask overviews (measured)
    wv = ctx.scratch() / f"{s['id']}.warp.part.tif"
    est = W * H * nb * (float(px) / float(s["res_m"])) ** 2 * 0.6     # lossless WEBP, generous
    if free_bytes(ctx.scratch()) < est:                           # free_bytes already keeps the disk cap
        raise CliError(E_FAIL, f"not enough scratch space to resample {s['id']} (~{est / 1e9:.0f} GB)",
                       "`lmap clean --dry-run`, or point scratch ([paths] tmp) at a bigger local disk")
    ctx.log(f"resampling {px:.4f} m server grid to {s['res_m']} m (lossless intermediate)")
    run(["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857", "-tr", s["res_m"], s["res_m"], "-tap", "-r", "average",
         "-ovr", "NONE", "-multi", "-wo", f"NUM_THREADS={max(1, cpu_cap())}"] + tile_co("webp-ll") + [out, wv], ctx,
        env={**os.environ, "GDAL_CACHEMAX": "512"})
    return wv


def finish_out(ctx, s):
    """Tiles -> one Cloud-Optimised GeoTIFF (built-in overviews) at s['out'], then delete the tiles."""
    need_tool("gdalbuildvrt", "gdal_translate")
    d, o = ctx.path(s["dest"]), ctx.path(s["out"])
    if s["kind"] == "wms-tiles" and cache_fmt(d) == "raw":
        return finish_raw(ctx, s, d, o)
    if s["kind"] == "wms-tiles":               # only the units the catalog expects (no strays from an old step)
        want = [d / f"{n}.tif" for n, _ in wmst_tiles(s)]; have = [f for f in want if f.is_file()]
        miss = len(want) - len(have); allow = int(getattr(ctx.a, "allow_missing", 0) or 0)
        if miss > allow:
            raise CliError(E_FAIL, f"{miss} of {len(want)} tiles missing in {d}",
                           f"re-run `lmap data fetch {s['id']} --yes`, or --allow-missing {miss} to build with holes")
        tiles = sorted(str(f) for f in have)
        # no full decode of every tile here: each was decoded when fetched (gdal_ok deep / sha1), and the
        # build reads every block anyway. A tile that broke since then fails the build; _bad_tiles finds it.
    else:
        tiles = sorted(str(f) for f in d.glob("*.tif") if ".part" not in f.name)
    if not tiles:
        raise CliError(E_FAIL, f"no tiles in {d}", f"lmap data fetch {s['id']} --yes")
    v = d.with_name(d.name + ".vrt"); lst = d.with_name(d.name + "-tiles.txt"); lst.write_text("\n".join(tiles))
    run(["gdalbuildvrt", "-q", "-overwrite", "-input_file_list", lst, v], ctx); lst.unlink()
    ctx.log(f"building {o} from {len(tiles)} tiles")
    need = sum(Path(t).stat().st_size for t in tiles)
    try:
        label = build_cog(ctx, v, o, need)      # tiles untouched on any failure: a re-run rebuilds from them
    except CliError as e:
        if s["kind"] != "wms-tiles" or "failed" not in e.msg:
            raise
        bad = _bad_tiles(ctx, tiles)            # the build failed reading: find + delete the broken tile(s)
        if not bad:
            raise
        raise CliError(E_FAIL, f"{len(bad)} corrupt tile(s) deleted: {', '.join(Path(b).stem for b in bad[:5])}",
                       f"re-run `lmap data fetch {s['id']} --yes` to fetch them again")
    if not o.is_file() or o.stat().st_size == 0:
        raise CliError(E_FAIL, f"{o} missing after install; tiles kept in {d}")
    shutil.rmtree(d); v.unlink(missing_ok=True)   # only now: the finished file is verified and in place
    freed = drop_source_scratch(ctx, s)
    return {"built": str(o), "gb": round(o.stat().st_size / 1e9, 2), "compress": label, "tiles_removed": len(tiles),
            **({"scratch_freed_mb": round(freed / 1e6)} if freed else {})}


def raw_boxes(d, units, z):
    """{unit: (x0, y0, x1, y1)} tile boxes the fetch recorded in each unit's .done marker (the exact tiles it
    checked are on disk). A marker without a box or from another zoom level is an error, not a guess."""
    out = {}
    for n, _ in units:
        try:
            m = json.loads((d / f"{n}.done").read_text())
        except (OSError, ValueError):
            m = {}
        if m.get("z") != z or len(m.get("box") or []) != 4:
            raise CliError(E_FAIL, f"{d}/{n}.done doesn't match the tile folder (zoom {m.get('z')} vs {z})",
                           f"delete {d}/{n}.done and re-run `lmap data fetch` (only missing tiles are fetched)")
        out[n] = tuple(m["box"])
    return out


def raw_missing(d, units):
    """{unit: missing tile count} for done units whose box has a tile not on disk (an unreadable/old marker
    counts as the whole unit missing: -1)."""
    z = int(re.search(r"<TileLevel>(\d+)</TileLevel>", (d / "tiles.xml").read_text()).group(1))
    out, boxes = {}, {}
    for u in units:
        try:
            boxes.update(raw_boxes(d, [u], z))
        except CliError:
            out[u[0]] = -1
    for n, (x0, y0, x1, y1) in boxes.items():
        k = sum(not (d / "tiles" / str(z) / str(y) / str(x)).is_file() for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
        if k:
            out[n] = k
    return out


def finish_raw(ctx, s, d, o):
    """finish_out for a raw cache: server tiles -> (one warp only if res_m isn't the server's grid) -> COG."""
    units = list(wmst_tiles(s)); have = [(n, b) for n, b in units if (d / f"{n}.done").is_file()]
    miss = len(units) - len(have); allow = int(getattr(ctx.a, "allow_missing", 0) or 0)
    if miss > allow:
        raise CliError(E_FAIL, f"{miss} of {len(units)} units missing in {d}",
                       f"re-run `lmap data fetch {s['id']} --yes`, or --allow-missing {miss} to build with holes")
    if not have:
        raise CliError(E_FAIL, f"no tiles in {d}", f"lmap data fetch {s['id']} --yes")
    # every tile "no imagery here" = the server was empty (an outage), not a real place without imagery: never
    # build that into a valid-looking black/clear COG. Drop the markers + tiles so the next fetch asks again.
    ms = []
    for n, _ in have:
        try:
            ms.append(json.loads((d / f"{n}.done").read_text()))
        except (OSError, ValueError):
            ms.append({})                              # unreadable marker: not evidence of an outage
    if all(m.get("blank", 0) >= m.get("tiles", 1) > 0 for m in ms) and not getattr(ctx.a, "allow_missing", 0):
        for n, _ in have:
            (d / f"{n}.done").unlink(missing_ok=True)
        for f in d.rglob("*"):
            if f.is_file() and f.suffix != ".done":
                f.unlink(missing_ok=True)
        raise CliError(E_FAIL, f"the server returned no imagery at all for {s['id']} (every tile empty): "
                               "likely a server outage; nothing built, cached tiles dropped",
                       f"try later: lmap data fetch {s['id']} --yes (a box truly without imagery: add --allow-missing 1)")
    v = d.with_name(d.name + ".vrt")
    try:
        return _finish_raw(ctx, s, d, o, units, have, v)
    finally:
        (ctx.scratch() / f"{s['id']}.warp.part.tif").unlink(missing_ok=True); v.unlink(missing_ok=True)


def _finish_raw(ctx, s, d, o, units, have, v):
    miss_ = raw_missing(d, have)                # cheap file checks: never start an hours-long build with a gap
    if miss_:
        for n in miss_:
            (d / f"{n}.done").unlink(missing_ok=True)
        raise CliError(E_FAIL, f"{sum(max(k, 1) for k in miss_.values())} tile(s) missing in {len(miss_)} unit(s)",
                       f"re-run `lmap data fetch {s['id']} --yes` (only those tiles are fetched)")
    src = raw_vrt(ctx, s, d, have, v)
    if ABORT.is_set():
        raise Aborted()
    need = Path(src).stat().st_size if Path(src).suffix == ".tif" else du_tree(d / "tiles")
    ctx.log(f"building {o} from {len(have)} units of server tiles ({need / 1e9:.1f} GB)")
    try:
        label = build_cog(ctx, src, o, need)    # tiles untouched on any failure: a re-run rebuilds from them
    except CliError as e:
        if "failed" not in e.msg:
            raise
        ctx.log("build failed: checking the tiles for a missing or corrupt one")
        bad = tms_scrub(d / "tiles"); miss_ = raw_missing(d, have)
        if not bad and not miss_:
            raise
        bad += sum(max(k, 1) for k in miss_.values())
        for n, _ in units:                      # the units re-check: only the deleted tiles are fetched again
            (d / f"{n}.done").unlink(missing_ok=True)
        raise CliError(E_FAIL, f"{bad} missing/corrupt tile(s) (corrupt ones deleted)", f"re-run `lmap data fetch {s['id']} --yes` to fetch them again")
    if not o.is_file() or o.stat().st_size == 0:
        raise CliError(E_FAIL, f"{o} missing after install; tiles kept in {d}")
    shutil.rmtree(d); v.unlink(missing_ok=True); Path(str(src)).unlink(missing_ok=True)
    freed = drop_source_scratch(ctx, s)
    return {"built": str(o), "gb": round(o.stat().st_size / 1e9, 2), "compress": label, "tiles_removed": len(have),
            **({"scratch_freed_mb": round(freed / 1e6)} if freed else {})}


def drop_source_scratch(ctx, s):
    """After s's finished file is installed (source lock held): its unit caches, styled tiles, tile-server copies
    in scratch are no longer needed for a resume. Returns bytes freed."""
    sc = ctx.scratch(); n = 0
    for x in [sc / f"{s['id']}.xml", sc / f"{s['id']}.stac", *own_glob(ctx, sc, s["id"], "*.cache"),
              *own_glob(ctx, sc, s["id"], "*.tiles"), *own_glob(ctx, sc, s["id"], "*.xml")]:
        if x.exists():
            n += du_tree(x) if x.is_dir() else x.stat().st_size
            shutil.rmtree(x, ignore_errors=True) if x.is_dir() else x.unlink(missing_ok=True)
    return n


def _bad_tiles(ctx, tiles):
    """Full decode of each tile, 8 at a time: which ones are broken (only after a failed build now)."""
    from concurrent.futures import ThreadPoolExecutor
    ctx.log(f"build failed: checking {len(tiles)} tiles for a corrupt one")
    with ThreadPoolExecutor(max(2, min(8, cpu_cap()))) as ex:
        bad = [t for t, ok in zip(tiles, ex.map(lambda t: gdal_ok(t, deep=True), tiles)) if not ok]
    for b in bad:
        Path(b).unlink(missing_ok=True)
    return bad


def verify_cog(ctx, f, ref):
    """Before tiles are deleted: every block decodes (all levels + mask, in parallel) and size/georef/overviews
    match the source vrt."""
    ctx.log(f"verifying {f.name} (decodes every block) {time.strftime('%H:%M')}")
    why = deep_decode(ctx, f)
    if why:
        raise CliError(E_FAIL, f"{f} failed a full decode check ({why}); tiles kept")
    ctx.vlog(f"verify {f}")
    a, b = (json.loads(subprocess.run(["gdalinfo", "-json", str(x)], capture_output=True, text=True).stdout or "{}")
            for x in (f, ref))
    ga, gb = a.get("geoTransform") or [], b.get("geoTransform") or []
    if a.get("size") != b.get("size") or len(ga) != 6 or any(abs(x - y) > 1e-6 * max(1, abs(y)) for x, y in zip(ga, gb)):
        raise CliError(E_FAIL, f"{f} size/georef {a.get('size')} {ga} != source {b.get('size')} {gb}; tiles kept")
    b1 = (a.get("bands") or [{}])[0]
    n_img, w = len(b1.get("overviews", [])), max(a.get("size") or [0])
    small = min((max(o.get("size") or [w]) for o in b1.get("overviews", [])), default=w)
    if w > 2048 and small > 1024:
        raise CliError(E_FAIL, f"{f}: smallest overview is {small} px (slow to open zoomed out); tiles kept")
    if "mask" in b1 and b1.get("mask", {}).get("flags") != ["ALL_VALID"] and \
            len(b1["mask"].get("overviews", [])) != n_img:
        raise CliError(E_FAIL, f"{f}: {n_img} image overviews but {len(b1['mask'].get('overviews', []))} mask "
                               "overviews (edges would break zoomed out); tiles kept")


def manual_fetch(ctx, s, plan):
    raise CliError(E_FAIL, f"{s['id']} is manual-download only", f"get it from {s['home']}")


KINDS = {
    "copernicus": (cop_status, cop_fetch, True),
    "tcc-aerial": (tcc_status, tcc_fetch, True),
    "url":        (url_status, url_fetch, False),
    "wms-clip":   (file_status, wms_fetch, False),
    "file-clip":  (file_status, fileclip_fetch, False),
    "wms-tiles":  (wmst_status, wmst_fetch, False),
    "s2-worldcover": (s2wc_status, s2wc_fetch, False),
    "qld-tides":  (tides_status, tides_fetch, False),
    "osm-clip":   (file_status, osm_fetch, False),
    "vector-zip": (file_status, vzip_fetch, False),
    "arcgis":     (file_status, arcgis_fetch, False),
    "manual":     (file_status, manual_fetch, False),
    "build-input": (lambda c, s: {"note": "downloaded by the project build that uses it"},
                    lambda c, s, p: (_ for _ in ()).throw(CliError(E_USAGE, f"{s['id']} is fetched by its project build",
                                                                     "lmap au project --yes")), False),
}
