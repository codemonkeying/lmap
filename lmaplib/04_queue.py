# lmap: queue. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- queue
# One supervisor process runs every queued source at once: one thread per server (sources on the same
# server go one after another - the QLD government server only ever sees one source + 3 requests),
# sharing one bandwidth cap (BW), per-server slots (host_slot) and one CPU pool (CPU).
QUEUE = STATE / "queue.json"


def queue_load():
    try:
        return json.loads(QUEUE.read_text())
    except FileNotFoundError:
        return {"ids": [], "results": {}}
    except ValueError as e:
        raise CliError(E_FAIL, f"{QUEUE} is corrupt ({e})", "fix or delete it, then `lmap queue add` again")


def queue_save(q):
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(QUEUE, json.dumps(q, indent=1))


_qlock = threading.Lock()


def queue_edit(fn):
    with _qlock:
        STATE.mkdir(parents=True, exist_ok=True)       # a fresh install: `queue add` is often the first command
        fd = os.open(STATE / "queue.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX); q = queue_load(); r = fn(q); queue_save(q); return r
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def source_host(s):
    """Which server a source hammers: sources sharing one run one after another."""
    k = s["kind"]
    u = (s.get("url") or (s.get("urls") or [""])[0] or s.get("base") or
         {"s2-worldcover": S2WC, "tcc-aerial": TCC_BLOB}.get(k) or (cop_bucket(s) if k == "copernicus" else ""))
    return urllib.parse.urlsplit(u).hostname or k


def queue_pid():
    if not lock_held(STATE / "locks" / "queue.lock"):
        return None
    try:
        pid = int((STATE / "locks" / "queue.lock").read_text().strip() or 0)
        a = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    return pid if b"queue" in a and b"run" in a else None


def cmd_queue_add(ctx):
    ids = ctx.a.ids
    for i in ids:
        s = ctx.src(i)
        if s["kind"] in ("manual", "build-input"):
            raise CliError(E_USAGE, f"{i} is not fetched on its own ({s['kind']})")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "queue.add", "ids": ids}
    refresh = bool(getattr(ctx.a, "refresh", False))
    def f(q):
        new = [i for i in ids if i not in q["ids"]]
        q["ids"] += new
        for i in new:
            q["results"].pop(i, None)
        if refresh:                                  # kept in the queue file: a stop + `queue run` still re-gets it
            q["refresh"] = sorted(set(q.get("refresh", [])) | set(ids))
        return new
    added = queue_edit(f)
    return {"ok": True, "action": "queue.add", "added": added, "queue": queue_load()["ids"],
            "refresh": [i for i in ids if i in queue_load().get("refresh", [])],
            "running": queue_pid(), "hint": None if queue_pid() else "start it: lmap queue run --yes"}


def cmd_queue_remove(ctx):
    def f(q):
        gone = [i for i in ctx.a.ids if i in q["ids"]]
        q["ids"] = [i for i in q["ids"] if i not in ctx.a.ids]
        q["refresh"] = [i for i in q.get("refresh", []) if i not in ctx.a.ids]
        return gone
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "queue.remove", "ids": ctx.a.ids}
    gone = queue_edit(f)
    return {"ok": True, "action": "queue.remove", "removed": gone,
            "note": "a source already running finishes its current item; `lmap job stop <id>` won't stop one thread - "
                    "stop the whole queue with `lmap queue stop --yes` and run it again"}


def cmd_queue_status(ctx):
    q = queue_load(); rows = []
    for i in q["ids"] + [i for i in q["results"] if i not in q["ids"]]:
        if i not in ctx.sources:
            continue
        s = ctx.src(i); st = job_state(ctx, s); r = q["results"].get(i) or {}
        state = ("running" if st["running"] else r.get("state") or "queued")
        rows.append({"id": i, "server": source_host(s), "state": state, "done": st.get("have"), "total": st.get("total"),
                     "eta": fmt_dur(st.get("eta_s")) if st["running"] else None, "error": r.get("error")})
    return {"ok": True, "action": "queue.status", "supervisor": queue_pid(), "bandwidth_mib_s": round(BW.rate / 1048576, 1)
            if BW.rate else "unlimited", "cpu_slots": ctx.shrink, "log": str(STATE / "queue.log"), "queue": rows}


def cmd_queue_stop(ctx):
    pid = queue_pid()
    if not pid:
        return {"ok": True, "unchanged": True, "action": "queue.stop", "note": "not running"}
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "queue.stop", "pid": pid}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to stop the queue (pid {pid}) without --yes")
    os.kill(pid, signal.SIGUSR1 if ctx.a.now else signal.SIGTERM)
    return {"ok": True, "action": "queue.stop", "pid": pid, "mode": "now" if ctx.a.now else "drain",
            "hint": "every source resumes where it stopped: lmap queue run --yes"}


def scratch_need(ctx, s):
    """Rough scratch bytes a tiled source (wms-tiles with out=) still needs: the share of est_gb not yet fetched
    plus the COG build that follows (build_cog wants tiles x1.5 + 5 GB beside the tiles, and refuses when short),
    so two sources can't both download and then both fail at the build. None = not scratch-heavy / can't tell."""
    if s.get("kind") != "wms-tiles" or not s.get("out") or not s.get("est_gb"):
        return None
    try:
        d = ctx.path(s["dest"]); units = list(wmst_tiles(s))
        fmt = cache_fmt(d); left = sum(1 for n, _ in units if not unit_file(d, n, fmt).is_file())
    except (CliError, KeyError, TypeError, ValueError, OSError) as e:   # bad catalog entry / unreadable dir: no guard
        ctx.vlog(f"scratch need for {s.get('name') or s.get('dest')}: unknown ({e})"); return None
    tiles = float(s["est_gb"]) * 1e9
    return int(left / max(1, len(units)) * tiles + tiles * 1.5 + 5e9)


def space_wait(ctx, s):
    """Space guard: before a queued source starts, its download must fit in scratch next to what the other
    running sources still need. Waits (another source finishing frees space) or, alone and still short, refuses."""
    need = scratch_need(ctx, s)
    if not need:
        return
    said = False
    while True:
        others = [i for i, o in ctx.sources.items() if i != s["id"] and job_pid(ctx, {**o, "id": i})]
        # space the running sources will still fill + live builds' reservations: two jobs starting together
        # can't both count the same free space (0.12 only compared against the free space right now)
        claimed = sum(scratch_need(ctx, ctx.src(i)) or 0 for i in others) + BuildSlot.reserved(ctx.scratch())
        free = free_bytes(ctx.scratch()) - claimed     # free_bytes keeps the margin / disk cap
        if free > need:
            return
        if not others:
            raise CliError(E_FAIL, f"scratch too small for {s['id']}: needs ~{need / 1e9:.0f} GB, {max(0, free) / 1e9:.0f} GB free",
                           f"free space in {ctx.scratch()} or set [paths] tmp = to a bigger disk")
        if not said:
            said = True; ctx.log(f"waiting for scratch space: needs ~{need / 1e9:.0f} GB, {max(0, free) / 1e9:.0f} GB free")
        if DRAIN.wait(60):
            raise Aborted()


QLOCK = threading.Lock()   # todo/active hand-over between the supervisor and the per-server workers


def queue_worker(ctx, host, todo, active):
    """One server's sources, one after another. Exits (and leaves `active`) under QLOCK, so a `queue add`
    landing as the last source finishes is either seen here or starts a new worker - never lost."""
    import copy
    while True:
        with QLOCK:
            if not todo:
                active.discard(host); return
            if DRAIN.is_set():
                active.discard(host); return
            sid = todo.pop(0)
        s = ctx.src(sid); c = copy.copy(ctx); c.prefix = sid
        refresh = sid in queue_load().get("refresh", [])   # only when added with --refresh: re-get even if out exists
        c.a = argparse.Namespace(**{**vars(ctx.a), "refresh": refresh, "id": sid})   # fetch code reads these
        if refresh:
            c.log("refresh: re-getting it even though it exists")
        c.log(f"start (server {source_host(s)})")
        try:
            space_wait(c, s)
            with Lock(lock_path(c, s), wait=c, drain=True):      # a hand-run fetch of it: wait, don't fail it
                if DRAIN.is_set():
                    raise Aborted()                  # stop came while waiting: start nothing new
                n = clean_stale(c, s)
                if n:
                    c.log(f"removed {n} stale partial file(s)")
                res = do_fetch(c, s, False)
            if DRAIN.is_set() and res.get("ok") is not False:
                res.setdefault("stopped", "drained")
        except Aborted:                              # stop while waiting for space/lock or mid-fetch: stays queued
            res = {"stopped": "aborted" if ABORT.is_set() else "drained"}
        except CliError as e:
            res = {"ok": False, "error": e.msg}
        except Exception as e:                       # one source's bug must not kill the others
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            for z in own_glob(c, c.scratch(), sid, "*.zip*"):
                z.unlink(missing_ok=True)
        if res.get("stopped"):
            c.log(f"stopped ({res['stopped']}); stays queued")
            with QLOCK:
                active.discard(host)
            return
        ok = res.get("ok") is not False and not res.get("failed")
        c.log(("done: " if ok else "FAILED: ") + json.dumps({k: v for k, v in res.items() if k != "failed"}, default=str)[:300])
        def f(q, sid=sid, ok=ok, res=res, refresh=refresh):
            if ok and not refresh and sid in q.get("refresh", []):
                return True                                  # add --refresh landed while a plain run went: go again
            if ok:
                q["ids"] = [i for i in q["ids"] if i != sid]
                if refresh:                                  # cleared only by the run that re-got it
                    q["refresh"] = [i for i in q.get("refresh", []) if i != sid]
            q["results"][sid] = {"state": "done" if ok else "failed", "at": time.strftime("%F %T"),
                                 "error": None if ok else (res.get("error") or res.get("hint") or "see log")}
        if queue_edit(f):
            c.log("refresh asked while it ran: running it again to re-get")
            with QLOCK:
                todo.insert(0, sid)


def cmd_queue_run(ctx):
    """Supervisor (foreground; --detach to background it). Picks up `queue add` while running."""
    need_root(ctx)
    if not queue_load()["ids"]:
        return {"ok": True, "unchanged": True, "action": "queue.run", "note": "queue is empty: lmap queue add <id> ..."}
    if ctx.a.dry_run:
        q = queue_load()
        by = {}
        for i in q["ids"]:
            by.setdefault(source_host(ctx.src(i)), []).append(i)
        return {"ok": True, "dry_run": True, "action": "queue.run", "servers": by,
                "bandwidth_mib_s": round(BW.rate / 1048576, 1) if BW.rate else "unlimited", "cpu_slots": ctx.shrink}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, "refusing to start the queue without --yes", "preview: lmap queue run --dry-run")
    if ctx.a.detach:
        STATE.mkdir(parents=True, exist_ok=True)
        gate = os.open(STATE / "queue.start", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)          # two --detach at once: one spawns, the other sees it
            pid = queue_pid()
            if pid:
                return {"ok": True, "unchanged": True, "action": "queue.run", "pid": pid}
            argv = [a for a in sys.argv if a != "--detach"]
            lf = STATE / "queue.log"; rotate_log(lf)
            with open(lf, "a") as log:
                os.chmod(lf, 0o600)
                p = subprocess.Popen([sys.executable] + argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
            for _ in range(100):
                time.sleep(0.2)
                if p.poll() is not None:
                    raise CliError(E_FAIL, "queue exited immediately", f"see {lf}")
                if queue_pid() == p.pid:
                    return {"ok": True, "action": "queue.run", "pid": p.pid, "log": str(lf), "hint": "lmap queue status"}
            return {"ok": False, "action": "queue.run", "pid": p.pid, "pending": True, "log": str(lf),
                    "hint": "started but has not taken its lock yet; check `lmap queue status`"}
        finally:
            fcntl.flock(gate, fcntl.LOCK_UN); os.close(gate)
    install_signals()
    with Lock(STATE / "locks" / "queue.lock"):
        ctx.log(f"=== queue start {time.strftime('%F %T')}: bandwidth "
                f"{(str(round(BW.rate / 1048576, 1)) + ' MiB/s') if BW.rate else 'unlimited'}, cpu slots {ctx.shrink}")
        serial = bool(getattr(ctx.a, "serial", False)) or str(ctx.queue_mode).lower() == "serial"
        if serial:
            ctx.log("serial: one source at a time, in queue order (each built and cleared from scratch first)")
        threads, active, todo, seen = [], set(), {}, set()
        while True:
            if not DRAIN.is_set():
                for i in queue_load()["ids"]:            # pick up new `queue add`s
                    if i in seen or i not in ctx.sources:
                        continue
                    seen.add(i); h = "serial" if serial else source_host(ctx.src(i))
                    with QLOCK:
                        todo.setdefault(h, []).append(i); start = h not in active
                        if start:
                            active.add(h)
                    if start:
                        t = threading.Thread(target=queue_worker, args=(ctx, h, todo[h], active), daemon=True)
                        t.start(); threads.append(t)
            threads = [t for t in threads if t.is_alive()]
            if not threads:
                break
            DRAIN.wait(15) if not DRAIN.is_set() else threads[0].join(5)
        q = queue_load()
    left = q["ids"]
    ctx.log(f"=== queue {'stopped' if DRAIN.is_set() else 'finished'} {time.strftime('%F %T')}; left: {left or 'none'}")
    r = {"ok": not any(r.get("state") == "failed" for r in q["results"].values() if r),
         "action": "queue.run", "left": left, "results": q["results"]}
    if left and (ABORT.is_set() or DRAIN.is_set()):
        r["stopped"] = "aborted" if ABORT.is_set() else "drained"
    return r


def fmt_dur(sec):
    if sec is None:
        return "?"
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


STAGE_RE = re.compile(r"^(building \S+ from \d+ tiles|step \d/\d: .*|building \S+ \(.*)$")


def job_stage(log, started):
    """The last build-stage line a job's log has written since this run started ('step 2/2: COG ... 17:41'),
    with when it appeared. Units all done but the job still running = it is joining them: per-unit ETA is
    meaningless then. None while it is still on units."""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 65536)); tail = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    hit = None
    for line in tail:
        if line.startswith("=== ") and " start" in line:
            hit = None                                     # a new run: older stages don't count
        elif STAGE_RE.match(line.strip()):
            hit = line.strip()
    if not hit:
        return None
    m = re.search(r"\b(\d\d:\d\d)$", hit)
    return {"stage": re.sub(r"\s+\d\d:\d\d$", "", hit), "stage_since": m.group(1) if m else None}


def job_log_outcome(log):
    """done / failed / stopped from the last run in a job log (its result block), or None (no log / no result:
    killed). Only the tail is read."""
    try:
        with open(log, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 65536)); t = f.read().decode(errors="replace")
    except OSError:
        return None
    t = t.rsplit("\n=== ", 1)[-1]                      # the last "=== <time> start ===" run only
    if re.search(r"^stopped: |^\{\"ok\": [^\n]*\"stopped\": |^\d\d:\d\d:\d\d stopped \(", t, re.M):
        return "stopped"
    last = None                                        # top-level result lines only; the last one wins
    for m in re.finditer(r"^(?:ok: (True|False)\b|\{\"ok\": (true|false)|(\{\"error\"))", t, re.M):
        last = "failed" if m.group(3) or (m.group(1) or m.group(2)) in ("False", "false") else "done"
    return last


def job_state(ctx, s):
    pid = job_pid(ctx, s)
    p = STATE / f"{s['id']}.json"
    try:
        st = json.loads(p.read_text()) if p.is_file() else {}
    except (OSError, ValueError):
        st = {}
    now = time.time()
    r = {"id": s["id"], "running": bool(pid), "pid": pid, "log": str(job_log(s)), **st}
    if st.get("updated") and st.get("started"):
        r["age_s"] = int(now - st["updated"])
        r["elapsed_s"] = int(st["updated"] - st["started"])
        if pid and st.get("eta_s") is not None:
            r["eta_s"] = max(0, st["eta_s"] - r["age_s"])
            r["eta_at"] = time.strftime("%a %H:%M", time.localtime(now + r["eta_s"]))
        if not pid:
            r["eta_s"] = None
    if not pid and not r.get("final"):                     # kinds with no progress file: the log's last run says
        r["final"] = job_log_outcome(job_log(s))
    if pid and st.get("left") == 0:                        # units done, still running: it is joining them
        stg = job_stage(job_log(s), st.get("started"))
        r.update(stg or {"stage": "finishing (joining the units)"})
        r["eta_s"] = r["eta_at"] = None
    return r


def cmd_job_status(ctx):
    need_root(ctx)
    if ctx.a.id and str(ctx.a.id).isdigit():
        d = run_read(int(ctx.a.id))
        if not d:
            raise CliError(E_NOTFOUND, f"no run with pid {ctx.a.id}", "lmap status (RUNS)")
        return {"jobs": [], "runs": [d]}
    if not ctx.a.id:
        return {"jobs": [job_state(ctx, ctx.src(i)) for i in ctx.sources if job_pid(ctx, ctx.src(i))
                         or (STATE / f"{i}.json").is_file()], "runs": run_list()}
    ids = [ctx.a.id] if ctx.a.id else [i for i in ctx.sources if job_pid(ctx, ctx.src(i))
                                        or (STATE / f"{i}.json").is_file()]
    return {"jobs": [job_state(ctx, ctx.src(i)) for i in ids]}


USE_WITH = {
    "copernicus": "fetch, job, verify, vrt, pyramid",
    "tcc-aerial": "fetch, job, verify, vrt, pyramid",
    "url":        "fetch, job",
    "wms-clip":   "fetch, job, verify",
    "file-clip":  "fetch, job, verify",
    "wms-tiles":  "fetch, job, verify",
    "s2-worldcover": "fetch, job, verify",
    "qld-tides":  "fetch, job; then: lmap tides --near=LAT,LON",
    "osm-clip":   "fetch, job",
    "vector-zip": "fetch, job",
    "arcgis":     "fetch, job",
    "manual":     "(download by hand: lmap source get <id>)",
}


def short_state(st):
    if st["kind"] in ("manual", "build-input"):
        return st["kind"]
    if st["running"]:
        return "running"
    if st.get("complete"):
        return "done"
    return "partial" if st.get("items_have") else "missing"


def cmd_list(ctx):
    """Every id you can pass to fetch/job/verify/vrt, with its state and the commands it supports."""
    need_root(ctx)
    out = []
    for i in [k for k, v in ctx.sources.items() if not v.get("template")]:
        s = ctx.src(i); st = status_of(ctx, s)
        out.append({"id": i, "kind": s["kind"], "state": short_state(st), "use_with": USE_WITH.get(s["kind"], ""),
                    "title": s["title"]})
    return {"list": out}


def share_reconnects():
    """Network-share (CIFS/SMB) reconnects since boot, from /proc/fs/cifs/Stats; None if no such share. A number that
    keeps rising = the network share link is dropping (builds retry, but it slows everything)."""
    try:
        m = re.search(r"(\d+) share reconnects", Path("/proc/fs/cifs/Stats").read_text())
        return int(m.group(1)) if m else None
    except OSError:
        return None


def areas_not_current(ctx):
    """Area layers the last build could not refresh (an old copy kept, or none): [{area, layer, why}]."""
    out = []
    for aj in lib_jsons(ctx, "areas"):
        try:
            b = json.loads(aj.read_text()).get("built") or {}
        except (OSError, ValueError):
            continue
        for k, r in b.items():
            if isinstance(r, dict) and (r.get("stale_kept") or r.get("error")):   # plain skips (all sea, no QGIS) are by design
                out.append({"area": aj.parent.name, "layer": k,
                            "why": "; ".join(str(r[x]) for x in ("stale_kept", "skipped", "error") if r.get(x))})
    return out


def cmd_status(ctx):
    """One screen: running jobs (rate/ETA) + every dataset's state and size."""
    need_root(ctx)
    ids = [ctx.src(ctx.a.id)["id"]] if getattr(ctx.a, "id", None) else [k for k, v in ctx.sources.items() if not v.get("template")]
    if getattr(ctx.a, "id", None):
        return {"detail": detail_of(ctx, ctx.src(ctx.a.id))}
    jobs = [job_state(ctx, ctx.src(i)) for i in ids if job_pid(ctx, ctx.src(i))]
    data = []
    for i in ids:
        st = status_of(ctx, ctx.src(i))
        data.append({"id": i, "state": short_state(st), "have": st.get("items_have"), "total": st.get("items_total"),
                     "no_data": st.get("items_no_data"), "bytes": st.get("bytes")})
    runs = run_list(); jl = [j for j in jobs if j["running"]]
    try:                                              # queued = waiting in a queue whose supervisor is alive
        q = queue_load(); live = {j["id"] for j in jl}
        queued = (sum(1 for i in q["ids"] if i in ctx.sources and i not in live
                      and (q["results"].get(i) or {}).get("state") not in ("done", "failed"))
                  if any(job_pid(ctx, ctx.src(i)) for i in q["ids"] if i in ctx.sources) else 0)
    except Exception:                                # status must show even with an odd queue file
        queued = 0
    return {"status": {"overall": status_overall(runs, jl, queued), "runs": runs, "jobs": jl, "data": data,
                       "areas_not_current": areas_not_current(ctx),
                       "root": str(ctx.root), "root_free_gb": round(free_bytes(ctx.root, raw=True) / 1e9, 1),
                       "root_used_pct": disk_used_pct(ctx.root), "disk_limit_pct": ctx.limits["disk"],
                       **({"share_reconnects": share_reconnects()} if share_reconnects() is not None else {})}}


def detail_of(ctx, s):
    st = status_of(ctx, s)
    d = {"id": s["id"], "title": s["title"], "kind": s["kind"], "state": short_state(st),
         "have": st.get("items_have"), "total": st.get("items_total"), "no_data": st.get("items_no_data"),
         "bytes": st.get("bytes"), "dest": st["dest"], "vrt": st.get("vrt"), "licence": s.get("licence"),
         "use_with": USE_WITH.get(s["kind"], "")}
    j = job_state(ctx, s)
    if j.get("total") is not None or j["running"]:
        d["job"] = j
    return d


def cmd_job_list(ctx):
    need_root(ctx)
    out = []
    for i in ctx.sources:
        s = ctx.src(i); pid = job_pid(ctx, s)
        lf = job_log(s); last = None
        if lf.is_file():
            lines = [l for l in lf.read_text(errors="replace").splitlines() if l.strip()]
            last = lines[-1] if lines else None
        if pid or (ctx.a.all and lf.is_file()):
            out.append({"id": i, "running": bool(pid), "pid": pid, "log": str(lf), "last": last})
    for r in run_list():                                # registered runs (builds, refreshes, direct fetches)
        if r["state"] in ("running", "died") or ctx.a.all:
            out.append({"id": r.get("cmd"), "run": True, "running": r["state"] == "running", "state": r["state"],
                        "pid": r["pid"], "log": r.get("log"), "done": r.get("done"), "total": r.get("total"),
                        "eta_s": r.get("eta_s"), "steps_running": r.get("running")})
    return out


def cop_audit(ctx, s):
    """Cross-check against the bucket's official tileList.txt: every real tile in range must be on disk."""
    raw = http_get(cop_bucket(s) + "/tileList.txt")
    if raw is None:
        raise CliError(E_UPSTREAM, "Copernicus tileList.txt not found (404)", "bucket layout changed?")
    tl = raw.decode().split()
    real = set(tl) & set(cop_tiles(s)); d = ctx.path(s["dest"])
    absent = sorted(t for t in real if not (d / f"{t}.tif").is_file())
    return {"official_tiles_in_range": len(real), "absent": absent}


def cmd_data_verify(ctx):
    need_root(ctx)
    need_tool("gdalinfo")
    s = ctx.src(ctx.a.id); d = ctx.path(s["dest"])
    if s.get("out") and ctx.path(s["out"]).is_file():
        o = ctx.path(s["out"]); good = gdal_ok(o, deep=not ctx.a.quick)
        return {"ok": good, "complete": good, "action": "data.verify", "id": s["id"], "checked": 1, "file": str(o),
                "bad": [] if good else [o.name], "hint": None if good else f"rebuild: lmap data fetch {s['id']} --yes --refresh"}
    if s["kind"] not in ("copernicus", "tcc-aerial", "wms-clip", "wms-tiles", "s2-worldcover"):
        raise CliError(E_USAGE, f"verify only supports raster sources, not {s['kind']}")
    raw = s["kind"] == "wms-tiles" and d.is_dir() and cache_fmt(d) == "raw"
    if not (d.is_file() or d.is_dir() and (any(d.glob("*.tif")) or raw and (d / "tiles").is_dir())):
        raise CliError(E_NOTFOUND, f"nothing of {s['id']} downloaded yet ({d})", f"lmap data fetch {s['id']} --yes")
    if raw:                                    # server tiles: quick = the unit markers; deep = decode every tile
        files = [f for f in (d / "tiles").glob("*/*/*") if not f.name.endswith(".tmp")] if (d / "tiles").is_dir() else []
        nbad = 0 if ctx.a.quick else tms_scrub(d / "tiles", files, delete=bool(ctx.a.yes and not ctx.a.dry_run))
        done_ = [(n, b) for n, b in wmst_tiles(s) if (d / f"{n}.done").is_file()]
        miss_ = raw_missing(d, done_) if done_ else {}
        bad = ([f"{nbad} undecodable tile(s)"] if nbad else []) + \
              ([f"{sum(max(k, 1) for k in miss_.values())} tile(s) missing in {len(miss_)} done unit(s)"] if miss_ else [])
        nbad += len(miss_)
        if nbad and ctx.a.yes and not ctx.a.dry_run:
            for f in d.glob("*.done"):         # units re-check; only the deleted tiles are fetched again
                f.unlink(missing_ok=True)
    else:
        files = sorted(d.glob("*.tif")) if d.is_dir() else [d]
        from concurrent.futures import ThreadPoolExecutor
        real = [f for f in files if f.is_file()]
        with ThreadPoolExecutor(max(2, min(8, cpu_cap()))) as ex:
            bad = [f.name for f, ok in zip(real, ex.map(lambda f: gdal_ok(f, deep=not ctx.a.quick), real)) if not ok]
    extra = cop_audit(ctx, s) if s["kind"] == "copernicus" else {}
    if s["kind"] == "tcc-aerial":
        st = tcc_status(ctx, s); extra = {"absent_count": st["items_todo"]}
    if s["kind"] == "wms-tiles":
        st = wmst_status(ctx, s); extra = {"absent_count": st["items_todo"]}
    complete = not extra.get("absent") and not extra.get("absent_count")
    parts = [f.name for f in (d.glob("*.part*") if d.is_dir() else [])]
    if bad and not ctx.a.dry_run and ctx.a.yes and not raw:
        for b in bad: (d / b).unlink()
    return {"ok": not bad and complete, "complete": complete, **extra, "action": "data.verify", "id": s["id"], "checked": len(files),
            "bad": bad, "stale_parts": parts,
            "removed_bad": bool(bad) and ctx.a.yes and not ctx.a.dry_run,
            "hint": "re-run with --yes to delete bad tiles, then fetch again" if bad and not ctx.a.yes else None}


def cmd_vrt_create(ctx):
    need_root(ctx)
    need_tool("gdalbuildvrt")
    s = ctx.src(ctx.a.id)
    if not s.get("vrt"):
        raise CliError(E_USAGE, f"{s['id']} has no vrt defined in the catalog")
    d, out = ctx.path(s["dest"]), ctx.path(s["vrt"])
    if s.get("kind") == "wms-tiles" and cache_fmt(d) == "raw":
        raise CliError(E_USAGE, f"{d} holds server tiles (raw), not a vrt source",
                       f"lmap data fetch {s['id']} --yes builds the finished file")
    files = sorted(str(f) for f in d.glob("*.tif"))
    if not files:
        raise CliError(E_NOTFOUND, f"no tiles in {d}", f"run `lmap data fetch {s['id']} --yes`")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "vrt.create", "id": s["id"], "vrt": str(out),
                "tiles": len(files), "overwrites": out.is_file()}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write {out} without --yes", f"preview: lmap vrt create {s['id']} --dry-run")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=ctx.scratch()) as f:
        f.write("\n".join(files)); lst = f.name
    try:
        run(["gdalbuildvrt", "-q", "-overwrite", "-input_file_list", lst, out], ctx)
    finally:
        os.unlink(lst)
    return {"ok": True, "action": "vrt.create", "id": s["id"], "vrt": str(out), "tiles": len(files)}

def _pyr_levels(path):
    """2,4,8.. until the smallest overview is <= 512 px on its long side."""
    out = subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True)
    w, h = json.loads(out.stdout)["size"]
    lv, f = [], 2
    while max(w, h) / f >= 256:
        lv.append(str(f)); f *= 2
    return lv or ["2"]


def _pyr_opts(path):
    """Compression by band type: RGB(A) byte -> photo, else DEFLATE (elevation keeps exact values)."""
    out = json.loads(subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True).stdout)
    bands = out.get("bands", [])
    rgb = len(bands) in (3, 4) and all(b.get("type") == "Byte" for b in bands) and \
        (len(bands) == 3 or bands[3].get("colorInterpretation") == "Alpha")
    return rgb, "average"


def grey3(ctx, src, part, i=0):
    """A 1-band photo as a 3-band VRT (R=G=B), for WEBP. Costs no disk: the VRT points at src.
    Named after the build's part file (+ index, not the source name: two sources can share one), so the
    part's own cleanup (glob part.name + "*") deletes it."""
    v = Path(part).parent / f"{Path(part).name}.grey3-{i}.vrt"
    run(["gdal_translate", "-q", "-of", "VRT", "-b", "1", "-b", "1", "-b", "1", src, v], ctx)
    return str(v)


def photo_src(ctx, src, part, i=0):
    """Every WEBP photo encode reads its source through here: a grey (1-band) one becomes R=G=B."""
    if ctx.webp and raster_info(str(src))["bands"] == 1:
        return grey3(ctx, src, part, i)
    return str(src)


def photo_q(ctx, q):
    """The encode quality for a photo: q, or 100 (= lossless WEBP, GDAL COG/GTiff QUALITY) for quality=original."""
    return 100 if getattr(ctx, "quality", "standard") == "original" else q


def photo_co(ctx, q=85):
    """-co args for a photo (Byte RGB/RGBA) raster: WEBP (default, webp=yes) or YCbCr JPEG (webp=no).
    Every photo encode goes through here so the config is honoured everywhere (0.10.2 only did cog_co)."""
    co = [f"COMPRESS=WEBP", f"QUALITY={photo_q(ctx, q)}"] if ctx.webp else ["COMPRESS=JPEG", f"QUALITY={q}"]
    return sum((["-co", c] for c in co), [])


def is_float(path):
    """True if band 1 is floating point (picks PREDICTOR 3 vs 2: 3 on integers errors or does nothing)."""
    r = subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)["bands"][0]["type"].startswith("Float")
    except (ValueError, KeyError, IndexError):
        return False


def photo_ovr_env(ctx, q=85):
    """GDAL config for external photo overviews (.ovr), same choice as photo_co."""
    if ctx.webp and photo_q(ctx, q) == 100:          # WEBP_LEVEL 100 is NOT lossless (measured): separate switch
        return {"COMPRESS_OVERVIEW": "WEBP", "WEBP_LOSSLESS_OVERVIEW": "YES"}
    return ({"COMPRESS_OVERVIEW": "WEBP", "WEBP_LEVEL_OVERVIEW": str(q)} if ctx.webp else
            {"COMPRESS_OVERVIEW": "JPEG", "PHOTOMETRIC_OVERVIEW": "YCBCR", "JPEG_QUALITY_OVERVIEW": str(q)})


def cog_co(ctx, src):
    """(gdal_translate args, label) for a finished COG from src. Photo: WEBP q85 (config [build] webp = yes, default;
    ~35% smaller than JPEG, QGIS/GDAL read it) or JPEG q85 YCbCr (webp=no: older phone/ATAK readers).
    An alpha band becomes the internal mask (-mask 4), so both keep transparent no-data.
    Elevation/other: DEFLATE + float predictor (exact values)."""
    out = json.loads(subprocess.run(["gdalinfo", "-json", str(src)], capture_output=True, text=True).stdout or "{}")
    bands = out.get("bands", []); rgb, rs = _pyr_opts(src)
    common = ["BIGTIFF=YES", "BLOCKSIZE=512", "SPARSE_OK=TRUE", f"OVERVIEW_RESAMPLING={rs.upper()}", "NUM_THREADS=ALL_CPUS"]
    if not rgb:
        flt = bands and bands[0].get("type", "").startswith("Float")
        # IGNORE_EXISTING: Copernicus tiles carry 2-3 levels of their own; reusing them left dem-SE-30m with 3
        # levels (smallest 4950 px: slow zoomed out). Photo builds keep AUTO (they reuse _staged_overviews).
        co = ["COMPRESS=DEFLATE", "PREDICTOR=FLOATING_POINT" if flt else "PREDICTOR=2", "OVERVIEWS=IGNORE_EXISTING"]
        return sum((["-co", c] for c in co + common), []), "deflate"
    alpha = ["-b", "1", "-b", "2", "-b", "3", "-mask", "4"] if len(bands) == 4 else []
    co = ["COMPRESS=WEBP", f"QUALITY={photo_q(ctx, 85)}"] if ctx.webp else ["COMPRESS=JPEG", "QUALITY=85"]
    return alpha + sum((["-co", c] for c in co + common), []), "webp" if ctx.webp else "jpeg"


BUILD_SLOTS = 2              # big GDAL builds at once machine-wide (COG, hillshade); config [build] builds = / LMAP_BUILDS
BUILD_THREADS = [4]          # GDAL threads each; 8 cores / BUILD_SLOTS
BUILD_CACHE = [1536]         # MB GDAL cache each: 2 x 1.5 GB fits 16 GB beside the downloads


class BuildSlot:
    """A big-build slot shared by every lmap process (flock'd STATE/locks/build.<i>). Each holder writes the
    scratch space it reserved ("<bytes> <dir>"), so two builds starting together don't both count the same
    free space (reserved()). A crashed holder's flock and reservation vanish with it."""
    def __init__(self, ctx, what):
        self.ctx, self.what, self.fd = ctx, what, None

    def __enter__(self):
        d = STATE / "locks"; d.mkdir(parents=True, exist_ok=True); said = False
        while True:
            for i in range(max(1, BUILD_SLOTS)):
                fd = os.open(d / f"build.{i}", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd); continue
                os.ftruncate(fd, 0); self.fd = fd
                return self
            if not said:
                said = True; self.ctx.log(f"{self.what}: waiting for a build slot ({BUILD_SLOTS} at once)")
            if ABORT.wait(5 + random.random()):
                raise Aborted()

    def others(self):
        """Other build slots held right now (by any lmap process)."""
        n = 0
        for f in (STATE / "locks").glob("build.*"):
            try:
                fd = os.open(f, os.O_RDONLY)
            except OSError:
                continue
            try:
                if os.fstat(fd).st_ino == os.fstat(self.fd).st_ino:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    n += 1
            finally:
                os.close(fd)
        return n

    def threads(self):
        """GDAL threads for this build: every core when it's the only build running (a lone build on half the
        cores ran 110+ min vs ~75 on all 8, 2026-10-14), else its share. Decided at start."""
        cores = cpu_cap()                              # [limits] cpu, not every core
        return cores if self.others() == 0 else BUILD_THREADS[0]

    def reserve(self, nbytes, where):
        os.ftruncate(self.fd, 0); os.pwrite(self.fd, f"{int(nbytes)} {Path(where).resolve()}".encode(), 0)

    def __exit__(self, *e):
        try:
            os.ftruncate(self.fd, 0); fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)

    @staticmethod
    def reserved(where, mine=None):
        """Bytes other live builds/steps reserved on the filesystem of `where` (BuildSlot + Reservation files whose
        flock is held: a dead process's reservation stops counting the moment it dies, reboot included)."""
        n, dev = 0, os.stat(where).st_dev
        for f in list((STATE / "locks").glob("build.*")) + list((STATE / "locks").glob("space.*.*")):
            try:
                fd = os.open(f, os.O_RDONLY)
            except OSError:
                continue
            try:
                if mine is not None and os.fstat(fd).st_ino == os.fstat(mine).st_ino:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); continue   # free
                except BlockingIOError:
                    pass
                b, _, p = os.pread(fd, 4096, 0).decode(errors="replace").partition(" ")
                if b.strip().isdigit() and p and os.path.exists(p) and os.stat(p).st_dev == dev:
                    n += int(b)
            finally:
                os.close(fd)
        return n

    def free(self, where):
        return free_bytes(where) - self.reserved(where, self.fd)

class Reservation:
    """Scratch/disk space claimed for one step: area create, contours, pyramids, export.
    `with Reservation(ctx, what, nbytes, where):` checks free space minus every other live reservation on that
    filesystem (BuildSlot's too) and refuses up front, or writes "<bytes> <dir>" into its own flock'd
    STATE/locks/space.<pid>.<n> for as long as the step runs. The flock dies with the process (crash, kill -9,
    OOM, reboot): nothing to clean up, no counter to undo; a leftover file without a held lock counts as 0.
    Check + claim run under one machine-wide lock (space.lock), so two steps starting together can't both
    count the same free space. raw=True: the plain free space (USB targets: no 5 GB margin).
    The claim is the step's whole estimate for its whole run (it doesn't shrink as files get written), so a long
    step may keep others waiting a bit early: the safe side."""
    _n = itertools.count()

    def __init__(self, ctx, what, nbytes, where, raw=False, hint=None):
        self.ctx, self.what, self.need, self.raw, self.hint = ctx, what, int(max(0, nbytes)), raw, hint
        self.where = Path(where); self.fd = None; self.f = None

    def __enter__(self):
        if not self.need:                              # nothing to claim (e.g. no OSM clip): never refuses
            return self
        d = STATE / "locks"; d.mkdir(parents=True, exist_ok=True)
        w = self.where
        while not w.exists():                          # not made yet: its nearest existing parent's disk
            w = w.parent
        with Lock(d / "space.lock", wait=self.ctx):
            for f in d.glob("space.*.*"):              # a dead process's file (its lock gone): tidy it away
                try:
                    fd = os.open(f, os.O_RDWR)
                except OSError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if _same_inode(f, fd):
                        f.unlink()
                except (BlockingIOError, OSError):
                    pass
                finally:
                    os.close(fd)
            other = BuildSlot.reserved(w); avail = free_bytes(w, raw=self.raw) - other
            if self.need > avail:
                rm = f"{free_bytes(w, raw=True) / 1e9:.1f} GB free" if self.raw else room(w)
                raise CliError(E_FAIL, f"not enough space for {self.what}: needs ~{self.need / 1e9:.1f} GB on {w}, "
                               f"{rm}" + (f", {other / 1e9:.1f} GB of it claimed by other running lmap steps"
                                               if other else ""),
                               self.hint or "free space (`lmap clean --dry-run` shows leftovers) or wait for the other "
                                            "steps; scratch: [paths] tmp = in ~/.config/lmap/config.toml")
            self.f = d / f"space.{os.getpid()}.{next(self._n)}"
            fd = os.open(self.f, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.pwrite(fd, f"{self.need} {w.resolve()}".encode(), 0)
            except BaseException:
                os.close(fd); self.f.unlink(missing_ok=True); raise
            self.fd = fd
        return self

    def __exit__(self, *e):
        if self.fd is not None:
            try:
                self.f.unlink(missing_ok=True)
            finally:
                os.close(self.fd); self.fd = None


def boot_time():
    """When this machine booted (Linux /proc/stat btime), or 0 if unknown."""
    try:
        for ln in Path("/proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                return int(ln.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def _sweep_build_tmp(where):
    """Remove .<name>.<pid>.tmp build dirs / <name>.<pid>.part.tif left by lmap processes that died hard
    (SIGKILL/OOM skip the finally): a leftover overview file can be tens of GB. A file last written over an hour
    before this boot is gone whoever has its pid now (the hour: a clock step after boot moves btime)."""
    bt = boot_time()
    for p in list(where.glob(".*.tmp")) + list(where.glob("*.part.tif")):
        m = re.search(r"\.(\d+)\.(?:tmp|part\.tif)$", p.name)
        if not m or int(m.group(1)) == os.getpid():
            continue
        try:
            if bt and p.stat().st_mtime < bt - 3600:
                raise ProcessLookupError                   # made before the last reboot: its pid may be reused now
            os.kill(int(m.group(1)), 0); continue          # owner still running
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def vrt_abs_sources(vrt, base):
    """A VRT made from a one-tile VRT can copy that tile's relativeToVRT path verbatim; it lives elsewhere (scratch
    tmp / checkpoint), so the path would point nowhere. Make every relative source absolute against `base`."""
    t = Path(vrt).read_text()
    def fix(m):
        f = m.group(2)
        return f'<SourceFilename relativeToVRT="0"{m.group(1)}>{f if os.path.isabs(f) else (Path(base) / f).resolve()}<'
    Path(vrt).write_text(re.sub(r'<SourceFilename relativeToVRT="1"([^>]*)>([^<]+)<', fix, t))


def _staged_overviews(ctx, src, tmpd, env, guard):
    """Photo sources: build the overviews ourselves as WEBP 95 on a band-selected VRT, so the COG driver reuses
    them. The driver's own overview temp file is always ZSTD (GDAL 3.10 cogdriver.cpp), ~8x WEBP for aerial
    photos: ~70 GB for a 37 GB aerial, which filled scratch twice. Measured (89k x 24k sample): temp 1.3 MB vs
    1.8 GB, full-res pixel-identical, overviews closer to a lossless reference (37.5 vs 35 dB PSNR), no dark
    fringes at mask edges (the single-step build has them: +26..+69 brightness error). Must use OVERVIEWS=AUTO:
    FORCE_USE_EXISTING skips the mask overviews. Returns the VRT to build the COG from."""
    bands = json.loads(subprocess.run(["gdalinfo", "-json", str(src)], capture_output=True, text=True).stdout
                       or "{}").get("bands", [])
    sel = tmpd / "sel.vrt"; base = Path(src).resolve()
    if len(bands) == 3 and "PER_DATASET" in (bands[0].get("mask") or {}).get("flags", []):
        # 3 bands + an internal mask (e.g. a one-tile source): the COG driver drops the mask's overviews from a
        # staged .ovr ("lacks overviews on the mask band"). As an alpha band they're built like a mosaic's.
        a4 = tmpd / "a4.vrt"
        run(["gdal_translate", "-q", "-of", "VRT", "-b", "1", "-b", "2", "-b", "3", "-b", "mask",
             "-colorinterp_4", "alpha", str(base), a4], ctx, env=env)
        vrt_abs_sources(a4, base.parent); base = a4; bands = bands + [{}]
    sel_args = ["-b", "1", "-b", "2", "-b", "3", "-mask", "4"] if len(bands) == 4 else []
    run(["gdal_translate", "-q", "-of", "VRT"] + sel_args + [str(base), sel], ctx, env=env)
    vrt_abs_sources(sel, base.parent)
    levels = _pyr_levels(sel)
    # staged at 95 (the COG re-encodes these at its own quality); format follows webp= so a JPEG build stays JPEG
    oenv = {**env, **photo_ovr_env(ctx, 95), "BIGTIFF_OVERVIEW": "YES", "INTERLEAVE_OVERVIEW": "PIXEL"}
    ql = "lossless" if "WEBP_LOSSLESS_OVERVIEW" in oenv else "95"
    ctx.log(f"step 1/2: overviews ({len(levels)} levels, {oenv['COMPRESS_OVERVIEW']} {ql}) {time.strftime('%H:%M')}")
    run(["gdaladdo", "-q", "-ro", "-r", "average", sel] + levels, ctx, env=oenv, watch=guard)
    if ABORT.is_set():
        raise Aborted()
    # the driver silently rebuilds them in its huge ZSTD temp if it can't see them: refuse instead
    info = json.loads(subprocess.run(["gdalinfo", "-json", str(sel)], capture_output=True, text=True).stdout or "{}")
    b1 = (info.get("bands") or [{}])[0]
    got = [tuple(o["size"]) for o in b1.get("overviews", [])]
    if len(got) != len(levels):
        raise CliError(E_FAIL, f"overview step made {len(got)} levels, expected {len(levels)}; build stopped",
                       "re-run; if it repeats, report it")
    msk = [tuple(o["size"]) for o in b1.get("mask", {}).get("overviews", [])]
    if msk and msk != got:          # mask overviews inherited from somewhere else (e.g. an old src.vrt.ovr)
        raise CliError(E_FAIL, f"mask overviews {msk[:2]}.. don't match image overviews {got[:2]}..; build stopped",
                       f"remove a stale {src}.ovr and re-run")
    return sel


NAS_RETRY_S = (15, 60, 180)        # waits between network-share copy attempts (a CIFS blip usually clears within minutes)


def nas_copy_retry(ctx, src, out, prog=None):
    """Copy src to out.part then rename, with retries that CONTINUE the copy (copy_resumable), not restart it:
    a short network-share outage must not cost the hours spent building src. One copier per out (lock)."""
    import hashlib
    dst = Path(str(out) + ".part")
    with Lock(STATE / "locks" / f"copy-{hashlib.sha1(str(out).encode()).hexdigest()[:12]}.lock"):
        for i, w in enumerate((0,) + NAS_RETRY_S):
            if w:
                ctx.log(f"copy of {out.name} failed; retry {i}/{len(NAS_RETRY_S)} in {w}s (continues where it stopped)")
                if ABORT.wait(w):
                    raise Aborted()
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                copy_resumable(src, dst, prog=prog); replace_retry(dst, out); return
            except (OSError, CliError):
                if i == len(NAS_RETRY_S):
                    raise


def src_ident(src):
    """What a build's source IS, not just its path: size/mtime/inode, and for a VRT its text + its files' mtimes
    (a re-fetch to the same path must not reuse stale overviews)."""
    import hashlib
    try:
        st = os.stat(src); ident = [st.st_size, st.st_mtime_ns, st.st_ino]
        if str(src).endswith(".vrt"):
            txt = Path(src).read_text(errors="replace"); ident.append(hashlib.sha1(txt.encode()).hexdigest())
            for f in re.findall(r"<SourceFilename[^>]*>([^<]+)<", txt)[:200000:997]:   # sampled: ~200 of them
                p = Path(f) if os.path.isabs(f) else Path(src).parent / f
                try:
                    ident.append(p.stat().st_mtime_ns)
                except OSError:
                    ident.append(None)
        return ident
    except OSError:
        return None


def build_cog(ctx, src, out, need_bytes, refs=None):
    """One build per output at a time (a per-output lock): two would share out.part / the checkpoint."""
    import hashlib
    with Lock(STATE / "locks" / f"build-{hashlib.sha1(str(out).encode()).hexdigest()[:12]}.lock", wait=ctx):
        return _build_cog(ctx, src, out, need_bytes, refs)


def _build_cog(ctx, src, out, need_bytes, refs=None):
    import hashlib
    """Write a finished COG for src at out. Built in local scratch (the COG driver writes a temp file then
    rewrites it: on CIFS that is ~3x the data over the network, random writes), full-decode verified, then
    copied to the network share and os.replace'd in. BUILD_SLOTS builds at once across all lmap processes, each with
    BUILD_THREADS GDAL threads + BUILD_CACHE MB; each reserves its scratch space so a second build can't count
    the same free space. Short of scratch space it refuses (it used to build beside out, i.e. on a network share:
    ~3x the data over the network, and it hit an I/O error there). If scratch and out are the same disk,
    the space check is the same either way.
    Photo sources build in 2 steps (see _staged_overviews); a free-space watchdog stops any step before
    the disk fills."""
    args, label = cog_co(ctx, src)
    rgb = label in ("webp", "jpeg")
    sc = ctx.scratch(); out.parent.mkdir(parents=True, exist_ok=True)
    kd = sc / "kept"; kn = hashlib.sha1(str(out).encode()).hexdigest()[:12] + "-" + out.name   # per out path
    kf, kj = kd / kn, kd / (kn + ".json")
    key = {"out": str(out), "src": str(src), "need": int(need_bytes), "label": label}
    if kf.is_file() or kj.is_file():
        with Lock(STATE / "locks" / f"kept-{kn}.lock"):
            try:
                kept_ok = kf.is_file() and json.loads(kj.read_text()) == key
                kept_ok = kept_ok and not (out.is_file() and out.stat().st_mtime > kf.stat().st_mtime)   # never over newer
            except (OSError, ValueError):
                kept_ok = False
            if kept_ok:                                # a verified build whose network-share copy failed last time: no rebuild
                ctx.log(f"{out.name}: installing the verified build kept from the last run (copy had failed)")
                nas_copy_retry(ctx, kf, out)
            kf.unlink(missing_ok=True); kj.unlink(missing_ok=True)   # used, or stale (inputs changed): gone
            if kept_ok:
                return label
    with BuildSlot(ctx, out.name) as slot:
        for d in (sc, out.parent):
            _sweep_build_tmp(d)
        # Peak space. Photo (staged): finished COG + our WEBP 95 overviews, no big driver temp; measured 1.39x
        # the source tiles. Other (DEM etc., single step): full-res + the driver's overview temp (~1/3 more).
        # quality=original: lossless output may be several times the (lossy) source tiles: 4x to be safe
        want = need_bytes * ((4.0 if photo_q(ctx, 85) == 100 else 1.5) if rgb else 1.6) + 5e9
        sl = Lock(STATE / "locks" / "space.lock", wait=ctx); sl.__enter__()   # check + claim as one step (Reservation)
        try:
            local = slot.free(sc) > want
            if local:
                slot.reserve(want, sc)
        finally:
            sl.__exit__(None, None, None)
        if not local:
            raise CliError(E_FAIL, f"not enough scratch space to build {out.name}: need ~{want / 1e9:.0f} GB free in "
                           f"{sc}, it has {slot.free(sc) / 1e9:.0f} after other builds' reservations",
                           f"`lmap clean --dry-run` shows leftovers to delete; or point scratch at a bigger local disk: "
                           f"[paths] tmp = in ~/.config/lmap/config.toml, or LMAP_TMP=<dir>")
        where = sc
        part = where / f"{out.name}.{os.getpid()}.part.tif"
        tmpd = where / f".{out.name}.{os.getpid()}.tmp"
        shutil.rmtree(tmpd, ignore_errors=True); tmpd.mkdir()
        be = BuildEta(need_bytes / 1e9, rgb)
        nt = slot.threads(); cache = BUILD_CACHE[0] * (2 if nt > BUILD_THREADS[0] else 1)
        ctx.log(f"building {out.name} ({label}) in {'local scratch' if local else out.parent}, {nt} threads")
        env = {**os.environ, "GDAL_CACHEMAX": str(cache), "GDAL_NUM_THREADS": str(nt), "CPL_TMPDIR": str(tmpd)}
        args = [a.replace("NUM_THREADS=ALL_CPUS", f"NUM_THREADS={nt}") for a in args]

        def guard():                                   # stop before the disk fills, not hours later
            f = free_bytes(where)
            return f"only {f / 1e9:.1f} GB left on {where}; output kept small, sources untouched" if f < 2e9 else None
        try:
            part.unlink(missing_ok=True)
            build_src = src; ck = None
            if rgb and max(json.loads(subprocess.run(["gdalinfo", "-json", str(src)], capture_output=True,
                                                     text=True).stdout or "{}").get("size") or [0]) > 512:
                # (<= one 512 block: the COG driver makes no overviews and would drop a staged level's mask)
                # checkpoint: the overview step (~1/3 of the build) survives a failure later in the build; the
                # next run with the same inputs + settings reuses it. Other inputs/settings: old checkpoints go.
                ckk = hashlib.sha1(json.dumps([key, args, photo_q(ctx, 95), src_ident(src)], default=str).encode()).hexdigest()[:8]
                ck = where / f".{kn}-{ckk}.ckpt"
                for old in where.glob(f".{kn}-*.ckpt"):
                    if old != ck:
                        shutil.rmtree(old, ignore_errors=True)
                if (ck / "ovr.done").is_file() and (ck / "sel.vrt").is_file():
                    ctx.log(f"{out.name}: reusing the overviews from the last run (checkpoint)")
                    build_src = ck / "sel.vrt"; be.stages = ["cog", "copy"]; be.cur = "ovr-reused"
                else:
                    be.stage("ovr", f"building {out.name}: overviews")
                    shutil.rmtree(ck, ignore_errors=True); ck.mkdir()
                    (ck / "key.json").write_text(json.dumps({**key, "ckpt": ckk}))
                    build_src = _staged_overviews(ctx, src, ck, env, guard)
                    (ck / "ovr.done").touch()
                ctx.log(f"step 2/2: COG (full detail + mask overviews) {time.strftime('%H:%M')}")
                sel_args = ["-b", "1", "-b", "2", "-b", "3", "-mask", "4"]
                if args[:len(sel_args)] == sel_args:      # already applied in sel.vrt
                    args = args[len(sel_args):]
            be.stage("cog" if be.cur in ("ovr", "ovr-reused") else "single", f"building {out.name}: full detail")
            _run(["gdal_translate", "-q", "-of", "COG"] + args + [build_src, part], ctx, env=env, watch=guard)
            be.stage("copy", f"verifying + copying {out.name}")
            tot = max(os.stat(part).st_size, 1)
            if ABORT.is_set():
                raise Aborted()
            shutil.rmtree(tmpd, ignore_errors=True)    # free the overview file before verify + copy
            if ck is not None:
                shutil.rmtree(ck, ignore_errors=True)  # full detail built: the checkpoint is no longer needed
            try:
                local = os.stat(part).st_dev != os.stat(out.parent).st_dev
            except OSError:
                pass
            if local:
                # copy to the network share (as out.part) WHILE verifying: the slow network-share copy hides under the decode check.
                # out is only replaced if the verify passes; on any failure the .part copy is removed.
                ctx.log(f"verifying {out.name} while copying it to {out.parent}")
                import concurrent.futures as cf
                dst = Path(str(out) + ".part"); keep_dst = [False]
                stop_copy = threading.Event()             # set on verify failure: the copy quits within 16 MB
                def copy():
                    copy_synced(part, dst, stop=lambda: stop_copy.is_set() or ABORT.is_set(),
                                prog=lambda n: be.progress(n / tot, f"copying {out.name} {n / 1e9:.1f}/{tot / 1e9:.1f} GB"))
                with cf.ThreadPoolExecutor(1) as ex:
                    cp = ex.submit(copy)
                    try:
                        verify_cog(ctx, part, refs or src)
                        try:
                            cp.result()
                            replace_retry(dst, out)
                        except (OSError, CliError) as e:      # a network-share blip: the verified build is NOT thrown away
                            ctx.log(f"copy to {out.parent} failed ({getattr(e, 'msg', e)}): retrying")
                            try:                               # continues the partial out.part, not from byte 0
                                nas_copy_retry(ctx, part, out, prog=lambda n: be.progress(
                                    n / tot, f"copying {out.name} {n / 1e9:.1f}/{tot / 1e9:.1f} GB"))
                            except Aborted:                    # stopped mid-retry: keep the verified build
                                kd.mkdir(exist_ok=True); os.replace(part, kf); kj.write_text(json.dumps(key))
                                keep_dst[0] = True              # and the partial network-share copy, for the next run
                                raise
                            except (OSError, CliError):
                                kd.mkdir(exist_ok=True); os.replace(part, kf); kj.write_text(json.dumps(key))
                                keep_dst[0] = True              # the next run continues this partial network-share copy
                                raise CliError(E_FAIL, f"{out.name}: built and verified, but the copy to {out.parent} "
                                               f"failed 4 times", f"kept in {kd}; re-run the same command: it installs "
                                               "that file without rebuilding")
                    except BaseException:
                        stop_copy.set(); cp.cancel()
                        try:
                            cp.result()
                        except BaseException:
                            pass
                        if not keep_dst[0]:
                            dst.unlink(missing_ok=True)
                        raise
            else:                                         # scratch on the same disk as out: verify, rename
                verify_cog(ctx, part, refs or src)
                try:
                    replace_retry(part, out)
                except OSError as e:
                    import errno
                    if e.errno != errno.EXDEV:            # same st_dev but another mount (bind mount): copy
                        raise
                    install_file(part, out)
        except BaseException:
            part.unlink(missing_ok=True); be.cur = None; RUN.update(stage=None); raise
        finally:
            part.unlink(missing_ok=True); shutil.rmtree(tmpd, ignore_errors=True)
    be.done()
    return label


def pyramid_short(f):
    """Smallest overview's long side (px) if the file needs more levels (> 1024 px), else None."""
    j = json.loads(subprocess.run(["gdalinfo", "-json", str(f)], capture_output=True, text=True).stdout or "{}")
    w = max(j.get("size") or [0]); b1 = (j.get("bands") or [{}])[0]
    small = min((max(o["size"]) for o in b1.get("overviews", [])), default=w)
    return small if w > 2048 and small > 1024 else None


def cmd_pyramid_fix(ctx):
    """Finished COGs (<root>/**/*.cog.tif) whose pyramid stops short: rebuild them with a full one. One file (path
    or source id) or, without one, every short file. Each: built + full-decode verified in scratch, then swapped in
    (the old file works until then). Same pixels; only the zoomed-out levels are added."""
    need_root(ctx)
    if ctx.a.id:
        s = ctx.sources.get(ctx.a.id)
        files = [ctx.path(s["out"])] if s and s.get("out") else [Path(ctx.a.id)]
        if not files[0].is_file():
            raise CliError(E_NOTFOUND, f"no finished file for {ctx.a.id}", "give a source id with an out file, or a path")
    else:
        files = sorted(x for x in ctx.path(".").glob("**/*.cog.tif") if "lmap-" not in str(x))
    todo = [(f, sm) for f in files if (sm := pyramid_short(f))]
    lossy = [str(f) for f, _ in todo if _pyr_opts(f)[0]]   # photo (JPEG/WEBP): a rebuild re-compresses every pixel
    todo = [(f, sm) for f, sm in todo if not _pyr_opts(f)[0]]
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "pyramid.fix", "checked": len(files),
                "fix": [{"file": str(f), "smallest_px": sm, "gb": round(f.stat().st_size / 1e9, 2)} for f, sm in todo],
                "skipped_photo": lossy}
    if not todo:
        return {"ok": True, "unchanged": True, "action": "pyramid.fix", "checked": len(files), "fixed": [],
                "skipped_photo": lossy}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to rebuild {len(todo)} file(s) without --yes", "preview: lmap pyramid fix --dry-run")
    install_signals()
    signal.signal(signal.SIGTERM, abort_signal)   # one long GDAL step: stop = abort
    done = []
    for f, sm in todo:
        if ABORT.is_set():
            break
        with Lock(STATE / "locks" / f"out-{f.name}.lock", wait=ctx):
            t0 = time.time(); ctx.log(f"{f.name}: smallest overview {sm} px: rebuilding the pyramid")
            build_cog(ctx, f, f, f.stat().st_size)     # verify_cog checks levels + every block, then os.replace
            if pyramid_short(f):
                raise CliError(E_FAIL, f"{f.name} still short after rebuild", "report it")
            done.append({"file": str(f), "secs": round(time.time() - t0)})
    if ABORT.is_set():
        return {"ok": False, "action": "pyramid.fix", "fixed": done, "stopped": "aborted", "hint": "re-run to continue"}
    return {"ok": True, "action": "pyramid.fix", "checked": len(files), "fixed": done, "skipped_photo": lossy}


def cmd_pyramid_create(ctx):
    """Zoomed-out copies so QGIS opens one file instead of hundreds of tiles.
    ovr: <vrt>.ovr beside the .vrt (small, quick; full detail still reads the tiles).
    cog: one Cloud-Optimised GeoTIFF <vrt stem>.cog.tif with built-in overviews (fastest; ~same size again)."""
    need_root(ctx)
    s = ctx.src(ctx.a.id)
    if not s.get("vrt"):
        raise CliError(E_USAGE, f"{s['id']} has no vrt in the catalog (pyramids are built over the .vrt)")
    vrt = ctx.path(s["vrt"])
    if not vrt.is_file():
        raise CliError(E_NOTFOUND, f"{vrt} missing", f"run `lmap vrt create {s['id']} --yes` first")
    mode = ctx.a.mode
    need_tool("gdaladdo" if mode == "ovr" else "gdal_translate")
    rgb, rs = _pyr_opts(vrt)
    out = Path(str(vrt) + ".ovr") if mode == "ovr" else vrt.with_name(vrt.stem + ".cog.tif")
    if ctx.a.dry_run:
        return {"ok": True, "dry_run": True, "action": "pyramid.create", "id": s["id"], "mode": mode,
                "out": str(out), "overwrites": out.is_file(), "compress": "JPEG" if rgb else "DEFLATE"}
    if not ctx.a.yes:
        raise CliError(E_REFUSED, f"refusing to write {out} without --yes",
                       f"preview: lmap pyramid create {s['id']} --mode {mode} --dry-run")
    if out.is_file() and not ctx.a.refresh:
        return {"ok": True, "unchanged": True, "action": "pyramid.create", "id": s["id"], "out": str(out),
                "hint": "already built; --refresh to rebuild (e.g. after new tiles)"}
    src_bytes = du_tree(ctx.path(s["dest"])) if ctx.path(s["dest"]).is_dir() else vrt.stat().st_size
    need = src_bytes * (0.4 if mode == "ovr" else 1.1)
    install_signals()
    # one long GDAL step: a plain stop (SIGTERM) can't "finish in-flight work", so treat it as abort
    signal.signal(signal.SIGTERM, abort_signal)
    t0 = time.time()
    env = {**os.environ, "GDAL_NUM_THREADS": str(cpu_cap()), "GDAL_CACHEMAX": "1024"}
    try:
        # cog mode: build_cog reserves its own (larger) scratch claim inside; a second claim here would count twice
        with Reservation(ctx, f"the {mode} pyramid of {s['id']}", need if mode == "ovr" else 0, out.parent):
            return _pyramid_build(ctx, s, mode, vrt, out, rgb, rs, env, t0)
    except Aborted:
        return {"ok": False, "action": "pyramid.create", "id": s["id"], "stopped": "aborted",
                "hint": "partial output removed; re-run to start again"}


def _pyramid_build(ctx, s, mode, vrt, out, rgb, rs, env, t0):
    with Lock(STATE / "locks" / f"pyramid-{s['id']}.lock"):
        if mode == "ovr":
            env.update(photo_ovr_env(ctx) if rgb else
                       {"COMPRESS_OVERVIEW": "DEFLATE", "PREDICTOR_OVERVIEW": "3" if is_float(vrt) else "2"})
            env["BIGTIFF_OVERVIEW"] = "YES"
            # build against a copy of the .vrt (same dir, so its relative tile paths still resolve),
            # then rename the finished .ovr into place: readers never see a half-written overview
            pv = vrt.with_name(vrt.stem + ".part.vrt"); po = Path(str(pv) + ".ovr")
            try:
                shutil.copyfile(vrt, pv); po.unlink(missing_ok=True)
                run(["gdaladdo", "-ro", "-r", rs, pv] + _pyr_levels(vrt), ctx, env=env)
                if ABORT.is_set() or DRAIN.is_set():
                    raise Aborted()
                replace_retry(po, out)
            except BaseException:
                po.unlink(missing_ok=True); raise
            finally:
                pv.unlink(missing_ok=True)
        else:
            src_bytes = du_tree(ctx.path(s["dest"])) if ctx.path(s["dest"]).is_dir() else vrt.stat().st_size
            build_cog(ctx, vrt, out, src_bytes)  # old COG stays usable until the final os.replace
    return {"ok": True, "action": "pyramid.create", "id": s["id"], "mode": mode, "out": str(out),
            "bytes": out.stat().st_size, "secs": round(time.time() - t0)}
