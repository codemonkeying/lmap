# lmap: data. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- commands
def status_of(ctx, s):
    dest = ctx.path(s.get("dest", "."))
    o = ctx.path(s["out"]) if s.get("out") else None
    if o and o.is_file():
        return {"id": s["id"], "kind": s["kind"], "dest": str(o), "items_total": 1, "items_have": 1, "items_todo": 0,
                "bytes": o.stat().st_size, "running": bool(job_pid(ctx, s)), "complete": True}
    if s["kind"] in ("manual", "build-input"):
        st = {"note": "manual source; not tracked per file" if s["kind"] == "manual" else
              "downloaded by the project build that uses it (lmap au project)"}
    else:
        st = KINDS[s["kind"]][0](ctx, s)
    st.update({"id": s["id"], "kind": s["kind"], "dest": str(dest),
               "bytes": (du_tree(dest) if s["kind"] == "build-input" else du(dest)) if s.get("dest", ".") != "." else None,
               "running": bool(job_pid(ctx, s))})
    if s.get("vrt"):
        st["vrt"] = ctx.path(s["vrt"]).is_file()
    todo = st.get("items_todo")
    st["complete"] = (todo == 0) if todo is not None else None
    return st


def cmd_source_list(ctx):
    return [{"id": k, "kind": v["kind"], "title": v["title"], "est_gb": v.get("est_gb")}
            for k, v in ctx.sources.items()]


def cmd_source_get(ctx):
    return ctx.src(ctx.a.id)


def cmd_data_status(ctx):
    need_root(ctx)
    ids = [ctx.a.id] if ctx.a.id else [k for k, v in ctx.sources.items() if not v.get("template")]
    return [status_of(ctx, ctx.src(i)) for i in ids]



ARC_DATE_FIELDS = ("lastupdate", "last_edited_date", "featuremoddate", "objectmoddate", "modifieddate", "feature_date",
                   "attribute_date")   # first one a layer has = when a feature was last changed (NSW / QLD services)
OUTDATED_SLACK = 2 * 86400             # look back 2 days before our file's time (= end of the pull): edits made while a
                                       # long pull ran may be missing. Longest pull today ~12 h. Errs to "outdated".


def _iso(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) + "Z" if t else None


def outdated_url(ctx, s):
    """url sources: HEAD every file (parallel): newer Last-Modified than our copy, or a different size."""
    import concurrent.futures as cf, email.utils
    d = ctx.path(s["dest"]); fs = [(u, d / n) for u, n in url_list(s)]
    have = [(u, f) for u, f in fs if f.is_file()]
    if not have:
        return {"status": "missing"}
    def one(uf):
        u, f = uf; st = f.stat()
        for method, hdr in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):   # some servers refuse HEAD
            try:
                rq = urllib.request.Request(u, method=method, headers={"User-Agent": UA, **hdr})
                with host_slot(u), OPENER.open(rq, timeout=60) as r:
                    h = r.headers; lm = h.get("Last-Modified")
                    m = re.match(r"bytes 0-0/(\d+)", h.get("Content-Range", ""))
                    size = int(m.group(1)) if m else 0 if method == "GET" or h.get("Content-Encoding") else \
                        int(h.get("Content-Length") or 0)
                up = email.utils.parsedate_to_datetime(lm).timestamp() if lm else None
                return f.name, up, size, st.st_mtime, st.st_size, None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError, ValueError) as x:
                err = f"{type(x).__name__}: {x}"[:120]
        return f.name, None, 0, st.st_mtime, st.st_size, err
    with cf.ThreadPoolExecutor(min(8, len(have))) as ex:
        rs = list(ex.map(one, have))
    new = [r[0] for r in rs if (r[2] and r[2] != r[4]) or (r[1] and r[1] > r[3])]
    blind = [r[0] for r in rs if not r[1] and not r[2]]   # no date, no size (or an error): can't tell
    errs = [f"{r[0]}: {r[5]}" for r in rs if r[5]]
    ups = [r[1] for r in rs if r[1]]
    st = "outdated" if new else "error" if errs else "unknown" if blind else "current"
    return {"status": st, "have": _iso(min(r[3] for r in rs)), "upstream": _iso(max(ups)) if ups else None,
            **({"changed": new[:10], "changed_n": len(new)} if new else {}),
            **({"errors": errs[:5]} if errs else {}),
            **({"note": f"{len(blind)} file(s): the server gives no date or size"} if blind and not errs else {})}


FETCH_STAMP_SLACK = 2 * 3600            # server clock skew: look back 2 h before our start (QLD servers: UTC, checked)


def fetch_stamp_path(sid):
    return STATE / "fetched" / f"{sid}.json"


def outdated_since(sid, out):
    """Epoch to count server edits after: our last pull's START (edits during the pull may be missing) less 2 h,
    only if its stamp was written for THIS file (same size + mtime, recorded right after install: no clock
    comparison between us and the network share); else the file time less 2 days."""
    o = out.stat()
    try:
        j = json.loads(fetch_stamp_path(sid).read_text())
        if int(j["size"]) == o.st_size and abs(float(j["mtime"]) - o.st_mtime) < 2 and float(j["start"]) <= time.time():
            return float(j["start"]) - FETCH_STAMP_SLACK, "pull start"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return o.st_mtime - OUTDATED_SLACK, "file time"


def outdated_arcgis(ctx, s):
    """ArcGIS layers: the server's feature count vs ours (adds/deletes), and features edited since our file
    (a date field, when the layer has one). Both cheap: count-only queries, nothing downloaded."""
    from osgeo import ogr
    out = ctx.path(s["dest"])
    if not out.is_file():
        return {"status": "missing"}
    mt = out.stat().st_mtime
    since_t, since_from = outdated_since(s["id"], out)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since_t))
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 30 * 86400))   # skip FUTURE dates: data-entry
    # errors (QLD watercourses carry 2031 feature_dates) would count as 'edited' forever; adds/deletes still show in counts
    geo = arc_geo(s)
    base = s.get("where", "1=1")
    ds = ro_ogr(out); layers = {}; unknown = []
    try:
        for name, lid in s["layers"].items():
            lu = layer_url(s, lid)
            g = {} if arc_statewide(s) else geo
            L = ds.GetLayerByName(name) if ds else None
            mine = L.GetFeatureCount() if L is not None else None
            theirs = arcgis_query(lu, {**g, "where": base, "returnCountOnly": "true", "f": "json"}).get("count")
            meta = json.loads(http_get(f"{lu}?f=json", timeout=60, retries=3) or b"{}")
            dates = {f["name"].lower(): f["name"] for f in meta.get("fields") or [] if f.get("type") == "esriFieldTypeDate"}
            df = next((dates[k] for k in ARC_DATE_FIELDS if k in dates), None)
            tz = str((meta.get("dateFieldsTimeReference") or {}).get("timeZone") or "UTC")
            # no time reference = UTC (checked on the QLD servers); AU local time is AHEAD of UTC, so 14 h covers it
            s_ = since if tz.upper() in ("UTC", "COORDINATED UNIVERSAL TIME") else \
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since_t - 14 * 3600))   # local-time server: widen
            edited = None
            if df:
                try:
                    edited = arcgis_query(lu, {**g, "where": f"({base}) AND {df} > timestamp '{s_}' AND {df} < timestamp '{until}'",
                                               "returnCountOnly": "true", "f": "json"}).get("count")
                except CliError:                       # server can't filter on dates: count-only for this layer
                    df = None
            if df is None or edited is None:
                df = None; unknown.append(name)
            layers[name] = {"ours": mine, "server": theirs, **({"edited_since": edited, "date_field": df} if df else {})}
    finally:
        ds = None
    changed = [k for k, v in layers.items() if v["ours"] != v["server"] or (v.get("edited_since") or 0) > 0]
    # "current" only if EVERY layer had its edits checked: an equal count can hide edits or adds + deletes
    st = "outdated" if changed else ("unknown" if unknown else "current")
    return {"status": st, "have": _iso(mt), "edits_since": f"{_iso(since_t)} ({since_from})", "layers": layers,
            **({"changed": changed} if changed else {}),
            **({"note": f"no edit date on {', '.join(unknown)}: only the feature count could be compared"} if unknown else {})}


def outdated_of(ctx, s):
    """{id, kind, status: outdated|current|unknown|missing|error, ...} for one source, without downloading it."""
    k = s["kind"]; r = {"id": s["id"], "kind": k}
    try:
        if k == "url":
            r.update(outdated_url(ctx, s))
        elif k == "arcgis":
            r.update(outdated_arcgis(ctx, s))
        elif k == "osm-clip":                         # cut from a local file: stale if that file is newer
            o, f = ctx.path(s["dest"]), ctx.path(s["from"])
            if not o.is_file():
                r["status"] = "missing"
            else:
                r.update(status="outdated" if f.is_file() and f.stat().st_mtime > o.stat().st_mtime else "current",
                         have=_iso(o.stat().st_mtime), follows=s["from"])
        else:
            o = ctx.path(s["out"]) if s.get("out") else ctx.path(s.get("dest", "."))
            r.update(status="unknown" if o.exists() else "missing",
                     note="this kind of source can't be asked cheaply for a version (tiles / imagery / manual)")
    except Aborted:
        raise
    except Exception as x:                            # one bad server must not hide the rest of the report
        r.update(status="error", error=f"{type(x).__name__}: {getattr(x, 'msg', x)}"[:200])
    return r


def outdated_report(ctx, ids=None):
    """Check every stored source (or ids) at once; a source cut from another (osm-clip) is outdated too when its
    input is (it is rebuilt after it)."""
    import concurrent.futures as cf
    ids = ids or [k for k, v in ctx.sources.items() if not v.get("template")]
    with cf.ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(lambda i: outdated_of(ctx, ctx.src(i)), ids))
    by = {r["id"]: r for r in rows}
    for r in rows:
        if r["kind"] == "osm-clip" and r.get("status") == "current":
            up = [x for x in by.values() if x.get("status") == "outdated" and x["kind"] == "url"
                  and ctx.path(ctx.sources[x["id"]]["dest"]) == ctx.path(ctx.sources[r["id"]]["from"]).parent]
            if up:
                r.update(status="outdated", why=f"its input {up[0]['id']} is outdated")
    return rows


def cmd_data_outdated(ctx):
    need_root(ctx)
    rows = outdated_report(ctx, [ctx.a.id] if ctx.a.id else None)
    for r in rows:
        if r.get("status") in ("outdated", "error"):
            ctx.log(f"  {r['status']:9} {r['id']}" + (f"  ({r.get('error') or r.get('why') or ', '.join(map(str, r.get('changed', [])))[:80]})"))
    n = {st: sum(r.get("status") == st for r in rows) for st in ("outdated", "current", "unknown", "missing", "error")}
    return {"ok": True, "action": "data.outdated", "counts": n,
            "outdated": [r["id"] for r in rows if r.get("status") == "outdated"], "sources": rows,
            **({"hint": "lmap refresh --outdated --dry-run, then --yes"} if n["outdated"] else {})}

def cmd_data_fetch(ctx):
    need_root(ctx)
    s = ctx.src(ctx.a.id); fetch = lambda c, s_, p: do_fetch(c, s_, p)
    if s.get("template"):
        raise CliError(E_USAGE, f"{s['id']} is an area template (fetched per area)", "lmap area create ... or its -state copy")
    if s.get("restricted"):                     # fine for your own use; the licence limits passing it on
        ctx.log(f"note: {s['id']} licence limits sharing: {s.get('licence', '?')}")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "data.fetch", "id": s["id"], **fetch(ctx, s, True)}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to fetch {s['id']} without --yes",
                       f"preview: lmap data fetch {s['id']} --dry-run")
    install_signals()
    scratch = ctx.scratch()
    with Lock(lock_path(ctx, s)):               # first: a job waiting for space is visible to `job list/stop`
        try:
            space_wait(ctx, s)                 # waits while other jobs hold the space; alone and short: refuse
        except Aborted:
            return {"ok": True, "action": "data.fetch", "id": s["id"], "stopped": "drained"}
        cleaned = clean_stale(ctx, s)          # leftovers from a crash/SIGKILL/power loss
        if cleaned:
            ctx.log(f"removed {cleaned} stale partial file(s) from an earlier crash")
        try:
            res = fetch(ctx, s, False)
        except Aborted:
            res = {}
        finally:
            for z in own_glob(ctx, scratch, s['id'], "*.zip*"):     # only OUR scratch files
                z.unlink(missing_ok=True)
    say_stop(ctx)
    if ABORT.is_set():
        res.update(stopped="aborted", ok=False)
    elif DRAIN.is_set():
        res["stopped"] = "drained"
    if res.get("stopped"):
        res["hint"] = f"resume: lmap job start {s['id']} --yes"
        ctx.log(f"stopped ({res['stopped']}); resume with: lmap job start {s['id']} --yes")
    return {"ok": True, "action": "data.fetch", "id": s["id"], **res}  # res may override ok


def clean_stale(ctx, s):
    """Delete *.part* left by a dead run. Only call while holding the source lock."""
    dest = ctx.path(s["dest"]); n = 0
    pats = [(dest, "*.part*")] if dest.is_dir() else [(dest.parent, dest.name + ".part*"), (dest.parent, dest.name + ".*.part")]
    if s["dest"] == ".":
        pats = [(dest, f"{Path(u).name}.part*") for _, u in url_list(s)] if s["kind"] == "url" else []
    resumable = s["kind"] in ("copernicus", "url")       # their *.part are raw downloads: keep for Range resume
    for d, pat in pats:
        for x in d.glob(pat):
            try:
                if resumable and x.name.endswith(".part") and x.stat().st_size > 0:
                    continue
                x.unlink(); n += 1
            except OSError:
                pass
    return n


def lock_path(ctx, s):
    """Local lock per source. Lock files on a network share were unreliable (flock and
    reading the pid back both misbehaved), so they live under ~/.local/state/lmap/locks/."""
    return STATE / "locks" / f"{s['id']}.lock"


def lock_held(p):
    """True if someone holds the flock on p (a stale pid left by kill -9 is not a running job)."""
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); return False
    except (BlockingIOError, PermissionError):
        return True
    finally:
        os.close(fd)


def job_pid(ctx, s, queue_ok=True):
    """pid of the live fetch for this source, or None. The lock must be held, then the pid is verified via
    /proc. queue_ok=False: a source run by the queue supervisor doesn't count (job stop must not stop it)."""
    if not lock_held(lock_path(ctx, s)):
        return None
    try:
        pid = int(lock_path(ctx, s).read_text().strip() or 0)
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    args = [x.decode(errors="replace") for x in argv]
    if (pid != os.getpid() and any(Path(a).name == "lmap" for a in args[:3]) and "queue" in args
            and "run" in args):                       # the queue supervisor runs it in a thread
        return pid if queue_ok else None
    try:
        i = args.index("fetch")
    except ValueError:
        return None
    if (any(Path(a).name == "lmap" for a in args[:i]) and args[i - 1:i] == ["data"]
            and args[i + 1:i + 2] == [s["id"]] and pid != os.getpid()):
        return pid
    return None


def job_log(s):
    return STATE / f"{s['id']}.log"


def cmd_job_start(ctx):
    need_root(ctx)
    s = ctx.src(ctx.a.id)
    if s["kind"] in ("manual", "build-input"):
        raise CliError(E_USAGE, f"{s['id']} is not fetched on its own ({s['kind']})")
    pid = job_pid(ctx, s)
    if pid:
        return {"ok": True, "unchanged": True, "action": "job.start", "id": s["id"], "pid": pid, "log": str(job_log(s))}
    argv = [sys.executable, str(Path(__file__).resolve()), "data", "fetch", s["id"], "--yes",
            "--root", str(ctx.root), "--jobs", str(ctx.jobs), "--shrink", str(ctx.shrink)]
    if ctx.tmp:
        argv += ["--tmp", str(ctx.tmp)]
    if getattr(ctx.a, "refresh", False):
        argv += ["--refresh"]
    if ctx.a.catalog:
        argv += ["--catalog", ctx.a.catalog]
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "job.start", "id": s["id"], "argv": argv, "log": str(job_log(s))}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to start {s['id']} without --yes", f"preview: lmap job start {s['id']} --dry-run")
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    lf = job_log(s)
    gate = os.open(STATE / f"{s['id']}.start", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(gate, fcntl.LOCK_EX)              # local: two `job start`s can't both spawn
        pid = job_pid(ctx, s)
        if pid:
            return {"ok": True, "unchanged": True, "action": "job.start", "id": s["id"], "pid": pid, "log": str(lf)}
        rotate_log(lf)
        with open(lf, "a") as log:
            os.chmod(lf, 0o600)
            log.write(f"\n=== {time.strftime('%F %T')} start jobs={ctx.jobs} ===\n"); log.flush()
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):                          # up to 20s for it to take the lock (or die)
            time.sleep(0.2)
            if p.poll() is not None:
                tail = lf.read_text().strip().splitlines()[-1:] or ["?"]
                raise CliError(E_FAIL, f"fetch exited immediately: {tail[0]}", f"see {lf}")
            if job_pid(ctx, s) == p.pid:
                return {"ok": True, "action": "job.start", "id": s["id"], "pid": p.pid, "jobs": ctx.jobs, "log": str(lf)}
        return {"ok": False, "action": "job.start", "id": s["id"], "pid": p.pid, "pending": True, "log": str(lf),
                "hint": "started but has not taken its lock yet (slow network share?); check `lmap job list`"}
    finally:
        fcntl.flock(gate, fcntl.LOCK_UN); os.close(gate)


def rotate_log(lf, keep=3, max_bytes=5_000_000):
    if lf.is_file() and lf.stat().st_size > max_bytes:
        for i in range(keep, 0, -1):
            src = lf.with_suffix(f".log.{i - 1}") if i > 1 else lf
            if src.exists():
                src.rename(lf.with_suffix(f".log.{i}"))


def cmd_job_stop(ctx):
    need_root(ctx)
    if str(ctx.a.id).isdigit():
        return run_stop_cmd(ctx, int(ctx.a.id))
    s = ctx.src(ctx.a.id)
    pid = job_pid(ctx, s)
    mode = "now" if ctx.a.now else "drain"
    if pid and not job_pid(ctx, s, queue_ok=False):
        raise CliError(E_REFUSED, f"{s['id']} is running inside the queue (pid {pid}); stopping it would stop every "
                       "queued source", "lmap queue stop --yes (resume: lmap queue run --yes)")
    if not pid:
        return {"ok": True, "unchanged": True, "action": "job.stop", "id": s["id"], "note": "not running"}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "job.stop", "id": s["id"], "pid": pid, "mode": mode}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to stop {s['id']} (pid {pid}) without --yes")
    os.kill(pid, signal.SIGUSR1 if ctx.a.now else signal.SIGTERM)
    waited, wait = 0, ctx.a.wait or (ABORT_GRACE + 5 if ctx.a.now else 0)   # --now: wait for it (it exits <= grace)
    while wait and job_pid(ctx, s) == pid and waited < wait:
        time.sleep(1); waited += 1
    still = job_pid(ctx, s) == pid
    if still and ctx.a.now:
        raise CliError(E_FAIL, f"{s['id']} (pid {pid}) still running {waited}s after --now", f"kill -9 {pid}")
    return {"ok": True, "action": "job.stop", "id": s["id"], "pid": pid, "mode": mode,
            "exited": not still if wait else None, "waited_s": waited,
            "hint": ("still finishing in-flight tiles; `lmap job list` to watch" if still and mode == "drain" else None)}


def run_stop_cmd(ctx, pid):
    """job stop <PID>: a registered run (lmap status). SIGTERM drains (runs that support it; others end now)."""
    d = run_read(pid)
    if not d:
        raise CliError(E_NOTFOUND, f"no run with pid {pid}", "lmap status (RUNS) or lmap job list")
    if d["state"] != "running":
        return {"ok": True, "unchanged": True, "action": "job.stop", "pid": pid, "note": f"not running ({d['state']})"}
    mode = "now" if ctx.a.now else ("pause" if d.get("pause") else "drain" if d.get("drain") else "end")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "job.stop", "pid": pid, "cmd": d.get("cmd"), "mode": mode}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to stop run {pid} ({d.get('cmd')}) without --yes")
    exited, waited = run_stop(pid, ctx.a.now, ctx.a.wait or (ABORT_GRACE + 5 if ctx.a.now else 0))
    if not exited and ctx.a.now:
        raise CliError(E_FAIL, f"run {pid} still running {waited}s after --now", f"kill -9 {pid}")
    return {"ok": True, "action": "job.stop", "pid": pid, "cmd": d.get("cmd"), "mode": mode,
            "exited": exited if (ctx.a.wait or ctx.a.now) else None, "waited_s": waited,
            "hint": ("pausing: the running step finishes however long it takes, then it exits (resume = run it "
                     "again; `--now` to stop at once); `lmap status` to watch") if not exited and mode == "pause"
            else "finishing running steps; `lmap status` to watch" if not exited and mode == "drain" else None}


def cmd_job_log(ctx):
    """Last --lines of a source's fetch log or a run's log."""
    need_root(ctx)
    if str(ctx.a.id).isdigit():
        d = run_read(int(ctx.a.id))
        if not d or not d.get("log"):
            raise CliError(E_NOTFOUND, f"no run log for pid {ctx.a.id}", "lmap status (RUNS)")
        lf = Path(d["log"])
    else:
        lf = job_log(ctx.src(ctx.a.id))
    if not lf.is_file():
        raise CliError(E_NOTFOUND, f"no log yet: {lf}")
    lines = lf.read_text(errors="replace").splitlines()[-max(1, ctx.a.lines):]
    return {"log": str(lf), "lines": lines}
