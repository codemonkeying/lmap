# lmap: plumbing. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- plumbing
class Ctx:
    def __init__(self, a):
        self.a = a
        cfg = read_config()                       # [paths]/[net]/[build] lifted to plain keys
        self.root = Path(a.root or os.environ.get("LMAP_ROOT") or cfg.get("root") or DEFAULT_ROOT)
        cat = Path(a.catalog or os.environ.get("LMAP_CATALOG") or cfg.get("catalog") or HERE / "sources.toml")
        self.catalog = cat
        if not cat.is_file():
            raise CliError(E_NOTFOUND, f"catalog not found: {cat}", "pass --catalog or set LMAP_CATALOG")
        self.sources = tomllib.loads(cat.read_text()).get("source", {})
        j = getattr(a, "jobs", None) or os.environ.get("LMAP_JOBS") or cfg.get("jobs") or 3
        try:
            self.jobs = int(j)
        except ValueError:
            raise CliError(E_USAGE, f"jobs must be an integer, got {j!r}")
        if not 1 <= self.jobs <= 32:
            raise CliError(E_USAGE, f"jobs must be 1-32, got {self.jobs}")
        self.limits = limits(cfg); LIMITS.update(self.limits); cap_threads(self.limits)
        sh = getattr(a, "shrink", None) or os.environ.get("LMAP_SHRINK") or cfg.get("shrink") or max(1, min(4, cpu_cap(self.limits) // 2))
        try:
            self.shrink = int(sh)
        except ValueError:
            raise CliError(E_USAGE, f"shrink must be an integer, got {sh!r}")
        if not 1 <= self.shrink <= 32:
            raise CliError(E_USAGE, f"shrink must be 1-32, got {self.shrink}")
        global BUILD_SLOTS
        bs = os.environ.get("LMAP_BUILDS") or cfg.get("builds") or BUILD_SLOTS
        try:
            BUILD_SLOTS = max(1, min(4, int(bs)))
        except ValueError:
            raise CliError(E_USAGE, f"builds must be an integer 1-4, got {bs!r}")
        cores = cpu_cap(self.limits)                    # [limits] cpu: the whole of lmap keeps at most this many busy
        BUILD_THREADS[0] = max(1, cores // BUILD_SLOTS)
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e6 * self.limits["ram"] / 100
        BUILD_CACHE[0] = max(256, min(2048, int(ram * 0.25 / BUILD_SLOTS)))   # a quarter of lmap's RAM, all builds
        self.tmp = getattr(a, "tmp", None) or os.environ.get("LMAP_TMP") or cfg.get("tmp") or DEFAULT_TMP
        if _dev(self.tmp) != _dev(self.root):          # scratch on its own disk: the disk cap holds it
            DISK_CAPPED.add(_dev(self.tmp))
        self.tiles_dir = os.environ.get("LMAP_TILES") or cfg.get("tiles")
        self.webp = str(os.environ.get("LMAP_WEBP") or cfg.get("webp", "yes")).lower() not in ("0", "no", "false", "off")
        # photo pixels: standard = high-quality lossy (smaller); original = lossless WEBP everywhere (true to the
        # server's pixels; several times bigger). JPEG has no lossless mode.
        self.quality = str(os.environ.get("LMAP_QUALITY") or cfg.get("quality") or "standard").lower()
        if self.quality not in ("standard", "original"):
            raise CliError(E_USAGE, f"quality = {self.quality!r}", 'use quality = "standard" or "original"')
        if self.quality == "original" and not self.webp:
            raise CliError(E_USAGE, 'quality = "original" needs webp (JPEG is always lossy)', "remove webp = no")
        self.gpu = str(cfg.get("gpu", "auto"))   # auto: used when nvcc + a working NVIDIA driver are found
        self.queue_mode = str(os.environ.get("LMAP_QUEUE") or cfg.get("queue", "parallel"))
        for v in self.sources.values():
            if v.get("xml") and not v["xml"].startswith("/"):
                v["xml"] = str(HERE / v["xml"])             # tile-server definitions ship with the tool (share/)
            if v.get("out"):                                 # dest/vrt name the tile cache, out the finished file
                for k in ("dest", "vrt"):
                    if v.get(k) and not v[k].startswith("/"):
                        v[k] = str(Path(self.tiles_dir or Path(self.tmp) / f"lmap-{os.getuid()}" / "tiles") / v[k])
        self.prefix = ""
        global UA, OPENER, CPU
        BW.rate = BW.tokens = parse_rate(getattr(a, "bandwidth", None) or os.environ.get("LMAP_BANDWIDTH")
                                         or cfg.get("bandwidth") or 0)
        try:
            hosts = {k: int(v) for k, v in (cfg.get("hosts") or {}).items()}
            SERVER_CFG.update({k: dict(v) for k, v in (cfg.get("servers") or {}).items()})
        except (TypeError, ValueError, AttributeError) as e:
            raise CliError(E_USAGE, f"config [hosts] values must be whole numbers, [servers.<host>] tables ({e})")
        HOSTS_CFG.update(hosts)
        CPU = threading.BoundedSemaphore(self.shrink); CPU_N[0] = self.shrink
        UA = os.environ.get("LMAP_UA") or cfg.get("ua") or DEFAULT_UA
        proxy = os.environ.get("LMAP_PROXY") or cfg.get("proxy")
        if proxy:   # http(s) proxy; for SOCKS run under torsocks/proxychains
            PROXY_SET[0] = True
            OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                                                 _NoDowngrade())
            self.vlog(f"proxy {mask_url(proxy)}")
            os.environ["GDAL_HTTP_PROXY"] = proxy          # GDAL (/vsicurl, WMS/TMS xml) in every child goes the
            for v in ("http_proxy", "https_proxy", "HTTPS_PROXY"):   # same way (lmap's proxy setting wins: it is
                os.environ.setdefault(v, proxy)                   # what the user asked lmap to use)
            for v in ("no_proxy", "NO_PROXY"):                  # this machine itself is never proxied
                os.environ.setdefault(v, "localhost,127.0.0.1,::1")
        os.environ.setdefault("GDAL_HTTP_USERAGENT", UA)   # GDAL's own default UA names GDAL + version
        os.environ.setdefault("OGR2OGR_USE_ARROW_API", "NO")   # GDAL 3.10 ogr2ogr Arrow path (GPKG->GPKG) segfaults
        # in OGRLayer::WriteArrowBatch under parallel load (measured: 2/24 vs 0/64 off); off costs ~1 s per 1 GB layer
        _NETLOG[0] = self.log

    def tiles(self):
        """Tile cache for downloads that end as one file (`out`): config [paths] tiles = / LMAP_TILES, default <tmp>/lmap-UID/tiles.
        Tiles are only the resumable download units; they are deleted once the finished file is built."""
        d = Path(self.tiles_dir) if self.tiles_dir else self.scratch() / "tiles"   # scratch(): owner/link check
        d.mkdir(parents=True, exist_ok=True)
        return d

    def scratch(self):
        """Private (0700) per-user scratch dir under --tmp; zips never land world-readable."""
        d = Path(self.tmp) / f"lmap-{os.getuid()}"
        d.parent.mkdir(parents=True, exist_ok=True); d.mkdir(mode=0o700, exist_ok=True)
        import stat as st_
        st = os.lstat(d)                               # a shared tmp: someone else may have made it first
        if st_.S_ISLNK(st.st_mode) or not st_.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise CliError(E_REFUSED, f"scratch {d} is not a directory owned by you (a link, or another user's)",
                           "remove it, or set [paths] tmp = in ~/.config/lmap/config.toml (LMAP_TMP) to a private disk")
        os.chmod(d, 0o700)
        if st.st_dev != _dev(self.root):               # scratch on its own disk: the disk cap holds it
            DISK_CAPPED.add(st.st_dev)
        if not getattr(self, "_warned_tmpfs", False) and is_tmpfs(d):
            self._warned_tmpfs = True
            self.log(f"WARNING: scratch {d} is on tmpfs (RAM): big jobs can fill memory and a reboot loses "
                     "progress. Set [paths] tmp = in ~/.config/lmap/config.toml (or LMAP_TMP) to a local disk")
        return d

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} [{self.prefix}] {msg}" if self.prefix else msg
        RUN.logline(line if self.prefix else f"{time.strftime('%H:%M:%S')} {msg}")   # run registry log (16_runs.py)
        if not self.a.quiet:
            print(line, file=sys.stderr, flush=True)

    def vlog(self, msg):
        if self.a.verbose:
            print(msg, file=sys.stderr, flush=True)

    def src(self, sid):
        if sid not in self.sources:
            raise CliError(E_NOTFOUND, f"source {sid} not found", "run `lmap source list`")
        return dict(self.sources[sid], id=sid)

    def path(self, rel):
        """<root>/rel (catalog paths are data/..., areas/..., maps/...)"""
        rel = str(rel)
        if os.path.isabs(rel) and not getattr(self, "tiles_dir", None) and getattr(self, "tmp", None) \
                and rel.startswith(str(Path(self.tmp) / f"lmap-{os.getuid()}") + "/"):
            self.scratch()                             # a tile cache under tmp: owner / not-a-link check first
        return self.root / rel


def own_glob(ctx, d, sid, rest):
    """d.glob(f"{sid}-{rest}") minus files of a LONGER source id that starts with "{sid}-" (sat-qld-s2 vs
    sat-qld-s2-2025): cleaning one source must never delete another's live scratch files."""
    longer = [o + sep for o in ctx.sources if o != sid and o.startswith(sid + "-") for sep in ("-", ".")]
    return [p for p in Path(d).glob(f"{sid}-{rest}") if not any(p.name.startswith(x) for x in longer)]


_STATE_G = {}


def _state_geom(slug):
    if slug not in _STATE_G:
        _STATE_G[slug] = _state_geom_read(slug)
    return _STATE_G[slug].Clone()


def _state_geom_read(slug):
    from osgeo import ogr
    ds = ro_ogr(STATES_GPKG) if STATES_GPKG.is_file() else None
    if ds is None:
        raise CliError(E_NOTFOUND, f"{STATES_GPKG} missing (state borders)", "reinstall lmap")
    for f in ds.GetLayer(0):
        if re.sub(r"[^a-z0-9]+", "-", (f.GetField("name") or "").lower()).strip("-") == slug and f.GetGeometryRef():
            return f.GetGeometryRef().Clone()
    raise CliError(E_NOTFOUND, f"state {slug} not in {STATES_GPKG}")


def _box_geom(bbox):
    from osgeo import ogr
    w, s_, e, n = bbox
    return ogr.CreateGeometryFromWkt(f"POLYGON (({w} {s_},{e} {s_},{e} {n},{w} {n},{w} {s_}))")


def state_touch(bbox, slug):
    """The box overlaps the state's land (OSM admin_level 4 borders, share/au-states.gpkg)."""
    return _state_geom(slug).Intersects(_box_geom(bbox))


def state_holds(bbox, slug):
    """The whole box is inside the state: no border cut needed."""
    return _state_geom(slug).Contains(_box_geom(bbox))


def state_clip_file(ctx, slug, bbox):
    """A GeoJSON (scratch) of the state's polygon cut to the box grown 0.05 deg: ogr2ogr -clipsrc for border boxes, so
    QLD and NSW lines meet at the border and are never drawn twice."""
    g = _state_geom(slug).Intersection(_box_geom([bbox[0] - .05, bbox[1] - .05, bbox[2] + .05, bbox[3] + .05]))
    p = ctx.scratch() / f"clip-{slug}-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}.geojson"   # one per clip:
    # parallel area items (nsw-roads + nsw-water) each delete their own
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": json.loads(g.ExportToJson())}]}))
    return p


def place_of(lat, lon):
    """("australia", "<state>") holding the point (state slug: queensland, new-south-wales ...); offshore -> nearest
    state within ~50 km; else ("australia", "other")."""
    from osgeo import ogr
    ds = ro_ogr(STATES_GPKG) if STATES_GPKG.is_file() else None
    if ds is None:
        raise CliError(E_NOTFOUND, f"{STATES_GPKG} missing (files new areas/maps by state)", "reinstall lmap")
    ly = ds.GetLayer(0); pt = ogr.CreateGeometryFromWkt(f"POINT ({lon} {lat})"); best = (0.5, "other")
    for f in ly:
        g = f.GetGeometryRef(); slug = re.sub(r"[^a-z0-9]+", "-", (f.GetField("name") or "").lower()).strip("-")
        if g is None or not slug:
            continue
        if g.Contains(pt):
            return "australia", slug
        dd = g.Distance(pt)
        if dd < best[0]:
            best = (dd, slug)
    return "australia", best[1]


def lib_jsons(ctx, kind):
    """Every <kind>/<country>/<state>/NAME/area.json (kind = areas|maps)."""
    r = ctx.path(kind)
    return sorted(r.glob("*/*/*/area.json")) if r.is_dir() else []


def lib_dir(ctx, kind, name, lat=None, lon=None):
    """The folder of area/map NAME: where it already is, else (lat/lon given) the new filed place
    <kind>/<country>/<state>/NAME; else <kind>/NAME, which doesn't exist (callers report it missing)."""
    hit = [p.parent for p in lib_jsons(ctx, kind) if p.parent.name == name]
    if len(hit) > 1:
        raise CliError(E_USAGE, f"{name} is in more than one place: " + ", ".join(str(h) for h in hit), "rename one")
    if hit:
        return hit[0]
    if lat is not None:
        return ctx.path(kind).joinpath(*place_of(lat, lon), name)
    return ctx.path(kind) / name


def sheet_dir(ctx, name):
    """area or map folder NAME (areas/ first, then maps/: a map folder is an area too). For print/export/verify."""
    for k in ("areas", "maps"):
        d = lib_dir(ctx, k, name)
        if (d / "area.json").is_file():
            return d
    return lib_dir(ctx, "areas", name)                   # missing: callers report it as before


def need_root(ctx):
    if not ctx.root.is_dir():
        if str(ctx.root) == DEFAULT_ROOT:                # fresh install: make the default library dir
            ctx.root.mkdir(parents=True, exist_ok=True); return
        # a configured root that's missing is usually an unmounted share: never create it (data would land
        # on the local disk under the mount point)
        raise CliError(E_NOTFOUND, f"data root not found: {ctx.root}",
                       "mount the disk/share it lives on, or pass --root / set [paths] root = in the config")


def apt_pkg(tool):
    """the Debian package for a tool, from share/deps.tsv (the one dependency list); None if not listed"""
    try:
        for line in (HERE / "share" / "deps.tsv").read_text().splitlines():
            f = line.split("\t")
            if len(f) == 5 and f[1] == tool and f[2] != "-":
                return f[2]
    except OSError:
        pass
    return None


def need_tool(*names):
    for n in names:
        if not shutil.which(n):
            pk = apt_pkg(n)
            raise CliError(E_FAIL, f"missing dependency: {n}",
                           (f"sudo apt install {pk}  (or: ./install.sh --deps)" if pk else "./install.sh --deps"))


class CpuSlots:
    """Heavy-GDAL slots shared by every lmap process (flock'd files in STATE/cpu): `shrink` warps machine-wide,
    not per job. Also takes the in-process semaphore so threads don't spin on the files."""
    def __enter__(self):
        CPU.acquire()
        d = STATE / "cpu"; d.mkdir(parents=True, exist_ok=True); n = max(1, CPU_N[0])
        while True:
            for i in range(n):
                fd = os.open(d / f"slot{i}", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); self.fd = fd; return self
                except BlockingIOError:
                    os.close(fd)
            if ABORT.wait(1 + random.random()):
                CPU.release(); raise Aborted()

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
        finally:
            CPU.release()


CPU_N = [4]


def run(cmd, ctx, env=None, watch=None):
    if Path(str(cmd[0])).name in HEAVY:
        with MemTurn(ctx), CpuSlots():                 # memory turn first: no CPU slot held idle while waiting
            return _run(cmd, ctx, env, watch=watch)
    return _run(cmd, ctx, env, watch=watch)


WRITE_ERR = re.compile(r"ERROR \d+:(?![^\n]*_TIFFVSetField)[^\n]*(compress|encod|write|I/O error|no space left|disk full)[^\n]*", re.I)   # (_TIFFVSetField: an odd tag in a SOURCE file, e.g. "writecount" in GA scans: harmless)


def mem_avail_mb():
    """MemAvailable from the kernel (what can be used without swapping), MB; None if unknown."""
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return None


MEM_HUNGRY = {"gdalwarp", "gdal_translate", "gdal_contour", "gdaldem", "ogr2ogr", "osmium", "qgis_process", "gdalbuildvrt"}
MEM_LIVE = [0]; _memlk = threading.Condition(); _MEMHELD = threading.local()   # this thread already has its turn


def _mem_kind(cmd):
    return Path(str(cmd[0])).name in MEM_HUNGRY


class MemTurn:
    """`with MemTurn(ctx):` around a heavy child started without run() (numpy/QGIS scripts)."""
    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        _mem_gate(self.ctx); _MEMHELD.on = True; return self

    def __exit__(self, *a):
        _MEMHELD.on = False; _mem_done()


MEM_DIR = STATE / "mem"          # one flock'd file per lmap process with a heavy step running: machine-wide view
_MEMFD = [None]


def _mem_mark(on):
    """Hold (on) / drop our 'heavy step running' file. Called under _memlk on 0 <-> 1 transitions."""
    try:
        if on and _MEMFD[0] is None:
            MEM_DIR.mkdir(parents=True, exist_ok=True)
            tmp = MEM_DIR / f".new-{os.getpid()}-{threading.get_ident()}"   # locked before it is visible as p<pid>
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); os.rename(tmp, MEM_DIR / f"p{os.getpid()}")
            except OSError:
                os.close(fd); tmp.unlink(missing_ok=True); raise
            _MEMFD[0] = fd
        elif not on and _MEMFD[0] is not None:
            (MEM_DIR / f"p{os.getpid()}").unlink(missing_ok=True); os.close(_MEMFD[0]); _MEMFD[0] = None
    except OSError:
        pass                                     # bookkeeping only: never stop work over it


def _mem_others():
    """Does another lmap process have a heavy step running right now? (stale files of dead processes: removed)"""
    mine = f"p{os.getpid()}"
    try:
        fs = [f for f in MEM_DIR.glob("p*") if f.name != mine]
    except OSError:
        return False
    live = False
    for f in fs:
        try:
            fd = os.open(f, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                if os.fstat(fd).st_ino == os.stat(f).st_ino:
                    f.unlink()                   # its process is gone
            except OSError:
                pass
        except BlockingIOError:
            live = True
        finally:
            os.close(fd)
    return live


def _mem_gate(ctx):
    """Kind to memory, no fixed limit: a heavy child starts at once if the machine has room, or if no heavy step
    of ANY lmap process runs (so it can't deadlock); else it waits for one to finish or memory to come back.
    Room = 1/8 of RAM (at least 1.5 GB) of MemAvailable, read at each start, so it follows the RAM fitted."""
    tot = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2**20
    need = max(1536, tot // 8); said = False
    with _memlk:
        while True:
            av = mem_avail_mb()
            if av is None or av >= need or (MEM_LIVE[0] == 0 and not _mem_others()):
                break
            if not said:
                ctx.log(f"waiting for memory ({av} MB free, want {need}) before the next step"); said = True
            if ABORT.is_set():
                raise Aborted()
            _memlk.wait(2)
        MEM_LIVE[0] += 1
        if MEM_LIVE[0] == 1:
            _mem_mark(True)


def _mem_done():
    with _memlk:
        MEM_LIVE[0] -= 1; _memlk.notify_all()
        if MEM_LIVE[0] == 0:
            _mem_mark(False)


def _cache_fit(cmd, env):
    """GDAL_CACHEMAX (env or --config) never more than a quarter of what is free right now."""
    av = mem_avail_mb()
    if not av:
        return cmd, env
    cap = str(max(128, av // 4))
    if env and str(env.get("GDAL_CACHEMAX", "")).isdigit() and int(env["GDAL_CACHEMAX"]) > int(cap):
        env = {**env, "GDAL_CACHEMAX": cap}
    cmd = list(cmd)
    for i in range(len(cmd) - 2):
        if str(cmd[i]) == "--config" and str(cmd[i + 1]) == "GDAL_CACHEMAX" and str(cmd[i + 2]).isdigit() \
                and int(cmd[i + 2]) > int(cap):
            cmd[i + 2] = cap
    return cmd, env


THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_MAX_THREADS")


def cap_threads(lim):
    """Every child inherits a thread ceiling of the [limits] cpu share: GDAL's ALL_CPUS (GDAL_NUM_THREADS, read
    when a driver asks for ALL_CPUS), and numpy/scipy/QGIS thread pools. Values the user set are kept."""
    n = str(cpu_cap(lim))
    for v in THREAD_VARS:
        if os.environ.get(v, "ALL_CPUS").upper() == "ALL_CPUS":     # unset, or "every core": the cpu share
            os.environ[v] = n
    if os.environ.get("GDAL_NUM_THREADS", "").upper() == "ALL_CPUS":
        os.environ["GDAL_NUM_THREADS"] = n
    # GDAL_NUM_THREADS is NOT set when unset: GDAL is single-threaded unless asked, and a set value breaks QGIS
    # GeoPDF writing (the vector layers come out with no geometry - measured, QGIS 3.x / GDAL 3.x). lmap's own
    # ALL_CPUS requests are rewritten per command (_all_cpus).


def _all_cpus(cmd, env):
    """NUM_THREADS=ALL_CPUS in a command means every core to GDAL (the env above only covers GDAL's default):
    replaced by the [limits] cpu share."""
    n = str(cpu_cap())
    out = [str(c).replace("NUM_THREADS=ALL_CPUS", f"NUM_THREADS={n}") if "ALL_CPUS" in str(c) else c for c in cmd]
    for i in range(1, len(out)):                    # --config GDAL_NUM_THREADS ALL_CPUS (two arguments)
        if str(out[i]) == "ALL_CPUS" and str(out[i - 1]).endswith("NUM_THREADS"):
            out[i] = n
    return out, ({**env, "GDAL_NUM_THREADS": n} if env and env.get("GDAL_NUM_THREADS") == "ALL_CPUS" else env)


def _run(cmd, ctx, env=None, timeout=None, watch=None):
    """watch: optional callable checked every second; a returned string kills the command and fails with it
    (e.g. a free-space watchdog: stop a build before it fills the disk, not 3 hours later)."""
    cmd, env = _all_cpus(cmd, env)
    if _mem_kind(cmd) and not getattr(_MEMHELD, "on", False):
        _mem_gate(ctx)
        cmd, env = _cache_fit(cmd, env)                # sized to what is free when it actually starts
        try:
            return _run1(cmd, ctx, env, timeout, watch)
        finally:
            _mem_done()
    if _mem_kind(cmd):
        cmd, env = _cache_fit(cmd, env)
    return _run1(cmd, ctx, env, timeout, watch)


def _run1(cmd, ctx, env=None, timeout=None, watch=None):
    ctx.vlog("+ " + " ".join(map(str, cmd)))
    pr = child(subprocess.Popen(list(map(str, cmd)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True, env=env, preexec_fn=_pdeath))   # own group
    try:
        return _run_wait(pr, cmd, ctx, timeout, watch)
    finally:
        unchild(pr)


def _run_wait(pr, cmd, ctx, timeout, watch):
    t0 = time.time()

    def kill():                                            # the whole group: helpers (e.g. gpuwarp's decoders) too
        try:
            os.killpg(pr.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pr.kill()
        pr.communicate()
    while True:
        try:
            out, err = pr.communicate(timeout=1); break
        except subprocess.TimeoutExpired:
            if ABORT.is_set() or STOP_BUILDS.is_set():
                kill(); raise Aborted()
            if timeout and time.time() - t0 > timeout:
                kill(); raise CliError(E_FAIL, f"{Path(str(cmd[0])).name} timed out after {timeout}s")
            why = watch() if watch else None
            if why:
                kill(); raise CliError(E_FAIL, f"{Path(str(cmd[0])).name} stopped: {why}")
    r = subprocess.CompletedProcess(cmd, pr.returncode, out, err)
    if r.returncode < 0:                                   # killed by a signal: no message of its own
        try:
            sig = signal.Signals(-r.returncode).name
        except ValueError:
            sig = f"signal {-r.returncode}"
        tail = (r.stderr or r.stdout).strip()[-300:]
        raise CliError(E_FAIL, f"{Path(str(cmd[0])).name} crashed ({sig}){': ' + tail if tail else ''}")
    if r.returncode:
        raise CliError(E_FAIL, f"{Path(str(cmd[0])).name} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    bad = WRITE_ERR.search(err or "")
    if bad:                                                # GDAL can exit 0 with every tile unwritten (e.g. WEBP + 1 band)
        raise CliError(E_FAIL, f"{Path(str(cmd[0])).name} failed to write: {bad.group(0)[:300]}")
    return r.stdout


class Retry(Exception):
    pass


def retry_after_s(v):
    """Retry-After as seconds: a number, or an HTTP date. None if absent/unreadable."""
    if not v:
        return None
    v = str(v).strip()
    if v.isdigit():
        return float(v)
    try:
        import email.utils
        return max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


# HTTP answers that won't change on a retry: give up at once (0.77; before, a 401 burned 8 tries and ~10 min).
# 403 is NOT here: some servers answer 403 to mean "slow down" (see THROTTLE). Nor 400: S3 says 400 RequestTimeout
# on a slow link, and some servers answer a Range resume with 400 (http_get then restarts without the .part).
HTTP_PERMANENT = {401, 405, 410, 451}


class RetryPolicy:
    """One retry rule for network calls: exponential backoff base**(i+1) s, capped, with
    0.5-1.5x jitter (no synchronised retries across threads), times BACKOFF (tests set 0). A server's
    Retry-After (429/503) is honoured up to ra_cap. Waits are abort-aware; no wait after the last try."""
    def __init__(self, attempts, base=2.0, cap=300.0, ra_cap=600.0):
        self.attempts, self.base, self.cap, self.ra_cap = attempts, base, cap, ra_cap

    def delay(self, i, exc=None):
        d = BACKOFF * min(self.cap, self.base ** (i + 1)) * (0.5 + random.random())
        hd = getattr(exc, "headers", None)
        ra = retry_after_s(hd.get("Retry-After")) if hd is not None and hasattr(hd, "get") else None
        return max(d, min(ra, self.ra_cap)) if ra is not None else d

    def wait(self, i, exc=None):
        """Sleep before try i+1; False = no tries left (no sleep). Raises Aborted."""
        if i >= self.attempts - 1:
            return False
        if ABORT.wait(self.delay(i, exc)):
            raise Aborted()
        return True

    @staticmethod
    def permanent(exc):
        return isinstance(exc, urllib.error.HTTPError) and exc.code in HTTP_PERMANENT


STALL_S, STALL_B = 120, 64 << 10   # a download moving < 64 KB in 120 s is dead: retry it (the socket timeout
                                   # restarts on every packet, so a trickling server would never time out)


def copy_body(r, f, meter=None, blk=1 << 18):
    """r -> file f in blocks, abort-aware and stall-aware. Returns bytes copied."""
    n, mark, t_mark = 0, 0, time.time()
    with live(r):
        while True:
            b = r.read1(blk) if hasattr(r, "read1") else r.read(blk)
            if ABORT.is_set():
                raise Aborted()
            if not b:
                return n
            BW.take(len(b)); f.write(b); n += len(b)
            if meter:
                meter.add(len(b))
            now = time.time()
            if now - t_mark > STALL_S:
                if n - mark < STALL_B:
                    raise Retry(f"stalled: {n - mark} bytes in {now - t_mark:.0f}s")
                mark, t_mark = n, now


_POOL = threading.local()       # per thread: host -> open keep-alive connection (in-memory GETs)


GDAL_HTTP = {"GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "5", "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504",
             "GDAL_HTTP_TIMEOUT": "90", "GDAL_HTTP_CONNECTTIMEOUT": "20", "GDAL_HTTP_TCP_KEEPALIVE": "YES"}


def gdal_env(extra=None):
    """The one set of GDAL HTTP retry/timeout settings (/vsicurl, WMS/TMS xml) for every GDAL child; `extra` overrides
    (e.g. GDAL_HTTP_MAX_RETRY 0 where lmap retries itself). A default the user set in the environment is left to the
    user (not returned, so neither env= nor --config overrides it). Returns a fresh dict of strings."""
    return {**{k: v for k, v in GDAL_HTTP.items() if k not in os.environ},
            **{k: str(v) for k, v in (extra or {}).items()}}


def gdal_config(env):
    """gdal_env() as gdalwarp/gdal_translate `--config K V` arguments."""
    return [a for k, v in env.items() for a in ("--config", k, v)]


def _keepalive_get(url, hdr, timeout):
    """One in-memory GET over this thread's kept-alive connection to the host: saves the TCP + TLS handshake
    per request (ArcGIS pages, JSON, lists; ~1 s each at 335 ms RTT). -> body bytes. Raises HTTPError for
    4xx/5xx like urllib; returns None for a redirect (the caller then uses the normal opener, which checks
    https -> http). Not used through a proxy (the opener handles that)."""
    u = urllib.parse.urlsplit(url); key = (u.scheme, u.netloc)
    pool = getattr(_POOL, "c", None)
    if pool is None:
        pool = _POOL.c = {}
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    for fresh in (False, True):
        c = None if fresh else pool.get(key)
        if c is None:
            c = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=timeout)
            pool[key] = c
        c.timeout = timeout
        if c.sock is not None:
            c.sock.settimeout(timeout)
        try:
            c.request("GET", path, headers=hdr); r = c.getresponse()
        except (http.client.HTTPException, OSError):
            c.close(); pool.pop(key, None)
            if fresh or ABORT.is_set():
                raise
            continue                                   # a stale kept-alive connection: once more, new                                   # the server closed an idle connection: once more, new
        except BaseException:
            c.close(); pool.pop(key, None); raise
        try:
            with live(c):
                b = read_capped(r)
        except BaseException:
            c.close(); pool.pop(key, None); raise
        if (r.getheader("Connection") or "").lower() == "close":
            c.close(); pool.pop(key, None)
        if 300 <= r.status < 400:
            return None
        if r.status >= 400:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return b


def http_get(url, dest=None, timeout=60, retries=8, meter=None, check=None):
    """GET url. Returns bytes (dest=None), True once dest is written, None on 404.
    Downloads go to dest.part. A retry (or a later run) resumes it with an HTTP Range request.
    The size must match Content-Length / Content-Range, and `check(part)` must pass,
    before the rename. A truncated or corrupt file is never marked done.
    Backoff: exponential with jitter, capped at 5 min. Abort-aware."""
    last = None; pol = RetryPolicy(retries, 2, 300)
    part = Path(str(dest) + ".part") if dest is not None else None
    i = 0
    while i < retries:
        if ABORT.is_set():
            raise Aborted()
        try:
            hdr = {"User-Agent": UA}
            have = part.stat().st_size if part is not None and part.exists() else 0
            if have:
                hdr["Range"] = f"bytes={have}-"
            if dest is None and not _proxied(urllib.parse.urlsplit(url)) and not urllib.parse.urlsplit(url).username:
                with host_slot(url):
                    try:
                        b = _keepalive_get(url, hdr, timeout)
                    except ValueError as e:                # over MAX_BODY
                        raise CliError(E_UPSTREAM, f"{url}: {e}")
                if b is not None:
                    net_ok_again(url)
                    if ABORT.is_set():
                        raise Aborted()
                    BW.take(len(b)); return b
            with host_slot(url), OPENER.open(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
                net_ok_again(url)
                if dest is None:
                    with live(r):
                        try:
                            b = read_capped(r)
                        except ValueError as e:
                            raise CliError(E_UPSTREAM, f"{url}: {e}")
                    if ABORT.is_set():
                        raise Aborted()
                    BW.take(len(b)); return b
                total, mode = None, "wb"
                if have and r.status == 206:
                    m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("Content-Range", ""))
                    if not m or int(m.group(1)) != have:
                        part.unlink(missing_ok=True); raise Retry("bad Content-Range; restarting")
                    total = int(m.group(2)) if m.group(2) != "*" else None
                    mode = "ab"
                else:
                    cl = r.headers.get("Content-Length")
                    total = int(cl) if cl and cl.isdigit() else None
                with open(part, mode) as f:
                    copy_body(r, f, meter)
            size = part.stat().st_size
            if total is not None and size != total:
                if size > total:
                    part.unlink(missing_ok=True)
                raise Retry(f"short read: {size} of {total} bytes")
            if check and not check(part):
                part.unlink(missing_ok=True); raise Retry("integrity check failed")
            part.rename(dest)
            return True
        except Aborted:
            raise                                          # keep the .part: the next run resumes it
        except urllib.error.HTTPError as e:
            net_ok_again(url)
            if e.code == 404:
                if part is not None:
                    part.unlink(missing_ok=True)
                return None
            if e.code == 416 and part is not None:         # asked past the end: the .part may be complete
                if check and part.exists() and check(part):    # (a run died between the last byte and rename)
                    part.rename(dest); return True
                part.unlink(missing_ok=True)
            last = e
            if e.code == 400 and part is not None and have:   # a Range resume refused: start the file again
                part.unlink(missing_ok=True)
            if RetryPolicy.permanent(e):
                if part is not None and e.code in (410, 451):  # gone for good: no orphan .part
                    part.unlink(missing_ok=True)
                raise CliError(E_UPSTREAM, f"download failed: {url}: HTTP {e.code} {e.reason} (not retried)")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if net_wait(url, _NETLOG[0]):                  # outage waited out: not a failed try
                continue
        except (Retry, http.client.HTTPException, OSError) as e:
            last = e
        pol.wait(i, last)
        i += 1
    raise CliError(E_UPSTREAM, f"download failed after {retries} tries: {url}: {last}")


def _head(url):
    """(size, ranges ok, version tag) of url, with retries. Servers that refuse HEAD or omit the size are
    asked with a 1-byte Range GET instead. Abort-aware."""
    last = None; pol = RetryPolicy(6, 2, 60)
    for k in range(6):
        if ABORT.is_set():
            raise Aborted()
        for method, hdr in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                rq = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **hdr})
                with OPENER.open(rq, timeout=60) as r, live(r):
                    h = r.headers; tag = h.get("ETag") or h.get("Last-Modified") or ""
                    if method == "GET":
                        m = re.match(r"bytes 0-0/(\d+)", h.get("Content-Range", ""))
                        if r.status == 206 and m:
                            return int(m.group(1)), True, tag
                        return int(h.get("Content-Length") or 0), False, tag
                    size = int(h.get("Content-Length") or 0)
                    if size:
                        return size, h.get("Accept-Ranges", "").lower() == "bytes", tag
            except urllib.error.HTTPError as x:
                if x.code == 404:
                    return None, False, ""
                last = x
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as x:
                last = x
        if RetryPolicy.permanent(last):                    # the range GET (last tried) said no for good
            break
        pol.wait(k, last)
    raise CliError(E_UPSTREAM, f"can't get the size of {url}: {last}")


def pwrite_all(fd, b, off):
    """os.pwrite until every byte is written (a regular file may take fewer on a nearly full disk)."""
    mv = memoryview(b)
    while mv:
        k = os.pwrite(fd, mv, off)
        if k <= 0:
            raise OSError(f"write at {off} took {k} bytes")
        mv, off = mv[k:], off + k


def done_list(f, n):
    """Chunk numbers listed in a .chunks/done file. Only whole lines count (a torn last append is ignored)."""
    try:
        lines = f.read_text().split("\n")[:-1]
    except OSError:
        return set()
    return {int(x) for x in lines if x.isdigit() and int(x) < n}


def http_get_parallel(ctx, url, dest, cap=None, chunk=16 << 20, check=None):
    """One big file over parallel HTTP Range requests (PGDA: ~0.25 MB/s per connection, 8 measured 3.5x one).
    Connections are tuned live: Tuner hill-climbs on measured bytes/s and halves (once per burst) on errors;
    HostShare splits the server's max (SERVERS/config) between lmap processes on the same host. 16 MB chunks are
    written at their offsets into ONE file, dest.chunks/data (no join copy, so each byte is written once and the
    disk needs the file's size, not twice it). dest.chunks/done lists finished chunks (each synced to disk before it is
    listed), NNNNN.at the synced bytes of a partial one: a stop/abort resumes byte-wise. Each answer's
    Content-Range must match the request; the chunk set is tied to the server's size + ETag/Last-Modified (a
    republished file starts over). Joined file must match the size and pass `check`. One chunk failing for good
    stops the others at once. Returns True, None (404) or False (stopped). No ranges: plain http_get."""
    import concurrent.futures as cf
    total, ranges, tag = _head(url)
    if total is None:
        return None
    if not total or not ranges or total <= chunk:
        return http_get(url, dest, timeout=300, check=check)
    host = urllib.parse.urlsplit(url).hostname
    prof = server_profile(host); cap = max(1, min(int(cap or prof["max"]), prof["max"]))
    cd = Path(str(dest) + ".chunks"); cd.mkdir(parents=True, exist_ok=True)
    n = (total + chunk - 1) // chunk
    meta = cd / "size"; sig = f"{total} {chunk} {tag}"
    if meta.is_file() and meta.read_text().strip() != sig.strip():   # changed upstream / other chunk size
        ctx.log(f"  {dest.name}: changed upstream since the last run: starting over")
        shutil.rmtree(cd); cd.mkdir()
    meta.write_text(sig)
    want = lambda i: min(chunk, total - i * chunk)
    data, donef = cd / ("data" + dest.suffix), cd / "done"   # suffix kept: checks/drivers that go by name
    meter, tun = Meter(), Tuner(cap, window=30, min_units=0)
    fd = os.open(data, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size != total:               # new, or not ours: nothing in it counts. Progress records
            donef.unlink(missing_ok=True)               # go first (synced), so a crash mid-reset can't leave
            for f in cd.glob("*.at"):                   # them pointing at zeros
                f.unlink()
            fsync_dir(cd)
            os.ftruncate(fd, 0); os.ftruncate(fd, total); os.fsync(fd)
        have_done = done_list(donef, n)               # rewritten whole: a torn last line (power loss) must not
        atomic_write(donef, "".join(f"{i}\n" for i in sorted(have_done)))   # glue onto the next append
    except BaseException:
        os.close(fd); raise
    try:                                                # fd is closed on every way out
        return _parallel_body(ctx, url, dest, cap, chunk, check, total, tag, host, prof, cd, n, want, data, donef,
                              have_done, meter, tun, fd, cf)
    finally:
        os.close(fd)


def _parallel_body(ctx, url, dest, cap, chunk, check, total, tag, host, prof, cd, n, want, data, donef, have_done,
                   meter, tun, fd, cf):
    """http_get_parallel once the data file is open (the caller closes fd, after every thread here has ended)."""
    todo = [i for i in range(n) if i not in have_done]
    at = lambda i: cd / f"{i:05d}.at"

    def synced(i):                                      # bytes of chunk i already safe on disk
        try:
            v = int(at(i).read_text())
        except (OSError, ValueError):
            return 0
        return v if 0 <= v <= want(i) else 0
    need = sum(want(i) - synced(i) for i in todo)
    if need > free_bytes(cd):
        raise CliError(E_FAIL, f"{dest.name}: needs {need / 1e9:.1f} GB free on {cd.parent}, has {max(0, free_bytes(cd)) / 1e9:.1f} GB",
                       "free some space (`lmap clean`) and re-run; finished chunks are kept")

    step = min(4 << 20, max(256 << 10, chunk // 4))   # synced progress every 4 MB (1/4 chunk when small)

    class At:                                           # copy_body target: chunk i's bytes at their offset;
        def __init__(self, i, have):                    # every `step` synced + recorded, so a stop resumes there
            self.i, self.pos, self.got, self.mark = i, i * chunk + have, have, have

        def write(self, b):
            if self.got + len(b) > want(self.i):        # more than the range asked for: never into the next chunk
                raise Retry(f"chunk {self.i}: server sent more than {want(self.i)} bytes")
            pwrite_all(fd, b, self.pos); self.pos += len(b); self.got += len(b)
            if self.got - self.mark >= step:
                self.sync()

        def sync(self):
            os.fdatasync(fd); atomic_write(at(self.i), str(self.got)); self.mark = self.got
    done = [n - len(todo)]; t0 = time.time(); lk = threading.Lock(); stop = threading.Event()
    tun.c = max(1, min(cap, prof["start"]))

    def get(i):
        a = i * chunk; b = a + want(i) - 1; last = None
        k = -1; pol = RetryPolicy(8, 2, 120)
        while k < pol.attempts - 1:
            k += 1
            if ABORT.is_set():
                raise Aborted()
            if stop.is_set():
                return
            w = None
            try:
                have = synced(i)                               # resume a partial chunk byte-wise
                if have < want(i):
                    hdr = {"User-Agent": UA, "Range": f"bytes={a + have}-{b}"}
                    if tag:
                        hdr["If-Range"] = tag                  # changed meanwhile: server sends 200, we restart
                    with OPENER.open(urllib.request.Request(url, headers=hdr), timeout=120) as r:
                        net_ok_again(url)
                        if r.status != 206:
                            raise Retry(f"no partial content (HTTP {r.status}); file changed upstream?")
                        m = re.match(r"bytes (\d+)-(\d+)/", r.headers.get("Content-Range", ""))
                        if not m or (int(m.group(1)), int(m.group(2))) != (a + have, b):
                            raise Retry(f"wrong range back: {r.headers.get('Content-Range')!r}")
                        w = At(i, have)
                        copy_body(r, w, meter)
                got = w.got if w else have
                if got != want(i):                      # short (At refuses more): keep what came
                    if w:
                        w.sync()
                    raise Retry(f"chunk {i}: {got} of {want(i)} bytes")
                os.fdatasync(fd)                        # on disk before it is listed as done
                with lk:
                    with open(donef, "a") as o:
                        o.write(f"{i}\n"); o.flush(); os.fsync(o.fileno())
                    at(i).unlink(missing_ok=True)
                tun.unit(True)
                with lk:
                    done[0] += 1
                    if done[0] % 8 == 0 or done[0] == n:
                        ctx.log(f"  {dest.name}: {done[0]}/{n} chunks, {meter.bytes / max(1, time.time() - t0) / 1e6:.2f}"
                                f" MB/s, {tun.c} conns (server share 1/{share.n})")
                return
            except Aborted:
                if w:
                    w.sync()                            # keep what came: the next run continues there
                raise
            except urllib.error.HTTPError as x:
                last = x; net_ok_again(url)
                if RetryPolicy.permanent(x):
                    stop.set()                          # the other chunks would get the same answer
                    raise CliError(E_UPSTREAM, f"{dest.name}: chunk {i}: {url}: HTTP {x.code} {x.reason} (not retried)")
                if x.code in THROTTLE or x.code >= 500:
                    tun.unit(False)                     # server unhappy: halve (once per burst)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as x:
                last = x
                if net_wait(url, ctx.log, stop):        # outage waited out: not a failed try, not a slow server
                    k -= 1; continue
                tun.unit(False)
            except (Retry, http.client.HTTPException, OSError) as x:
                last = x; tun.unit(False)
            if k == pol.attempts - 1:
                break                                   # last try: no sleep
            end = time.time() + pol.delay(k, last)     # honours Retry-After
            while time.time() < end and not stop.is_set():
                if ABORT.wait(1):
                    raise Aborted()
        raise CliError(E_UPSTREAM, f"{dest.name}: chunk {i} failed after {pol.attempts} tries: {last}")

    ctx.log(f"  {dest.name}: {total / 1e9:.2f} GB, {len(todo)} of {n} chunks to get, {host} max {cap}, start {tun.c}")
    q = list(todo); run = set(); share = ex = None
    try:
        share = HostShare(host); ex = cf.ThreadPoolExecutor(cap)
        while q or run:
            others = share.count() - 1
            room = (cap - share.busy) // (others + 1)
            lim = min(int(tun.c), room) if room > 0 else (0 if (others or share.busy) else 1)
            share.publish(lim)
            while q and len(run) < lim and not (DRAIN.is_set() or ABORT.is_set()):
                run.add(ex.submit(get, q.pop(0)))
            tun.busy(len(run) + (int(tun.c) - lim if lim < tun.c else 0))   # held back by the share: not "unsaturated"
            if not run:
                if DRAIN.is_set() or ABORT.is_set() or not q:
                    break
                if ABORT.wait(2):                       # no room on the server right now: wait for a share
                    break
                continue
            fin, _ = cf.wait(run, timeout=2, return_when=cf.FIRST_COMPLETED)
            for fu in fin:
                run.discard(fu); fu.result()            # re-raise Aborted / CliError
            tun.tick(meter.bytes)
    except BaseException:
        stop.set()                                      # the other connections stop at their next try
        for fu in run:
            fu.cancel()
        raise
    finally:
        if ex:
            ex.shutdown(wait=True, cancel_futures=True)     # every writer done before the caller closes fd
        if share:
            share.close()
    if DRAIN.is_set() or ABORT.is_set():
        return False
    listed = done_list(donef, n)
    if listed != set(range(n)) or data.stat().st_size != total:
        raise CliError(E_FAIL, f"{dest.name}: {len(listed & set(range(n)))} of {n} chunks done at the end; re-run to resume")
    if check and not check(data):
        shutil.rmtree(cd); raise CliError(E_FAIL, f"{dest.name}: failed its check; chunks dropped, re-run")
    os.replace(data, dest); shutil.rmtree(cd)
    return True


def write_if_changed(p, text):
    """atomic_write, skipped when the file already holds exactly text (keeps its mtime: readers' caches stay valid)."""
    try:
        if Path(p).read_text() == text:
            return
    except (OSError, ValueError):
        pass
    atomic_write(p, text)


_VRT_STATS = re.compile(rb'[ \t]*<MDI key="STATISTICS_[^"]*">[^<]*</MDI>[ \t]*\r?\n?')
_VRT_EMPTY_MD = re.compile(rb'[ \t]*<Metadata>\s*</Metadata>[ \t]*\r?\n?')


def vrt_norm(b):
    """VRT bytes without the STATISTICS_* block GDAL writes back into it after a read (that write made the NSW
    DEM VRT differ each run -> its 10 min hillshade rebuilt every run). A VRT with no statistics is unchanged."""
    return _VRT_EMPTY_MD.sub(b"", _VRT_STATS.sub(b"", b))


def atomic_write(p, text):
    """Write text to p via a private tmp file, fsync'd before the rename and the directory after it (a power cut
    leaves old or new, never empty, and the rename itself survives)."""
    p = Path(p); tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        replace_retry(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True); raise
    fsync_dir(p.parent)


def fsync_dir(d):
    """Make a rename in d durable. Best effort: some filesystems (CIFS, some FUSE) refuse fsync on a directory."""
    try:
        fd = os.open(str(d), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ro_gpkg_opts(path):
    """Open options for READING a GeoPackage: IMMUTABLE=YES (no SQLite locking). Under load, SQLite's shared-lock
    probes on a CIFS share stall for minutes (seen: a `--dry-run` hung 280 s in fstat). Safe because lmap never
    edits a finished gpkg in place: it builds a part file and renames it over. A non-empty -wal = someone else is
    writing it (or a killed write left a hot -journal): then normal locking."""
    p = str(path)
    if not p.lower().endswith(".gpkg") or p.startswith("/vsi"):
        return []
    if any(os.path.exists(p + x) for x in ("-journal", "-wal", "-shm")):
        return []                                # a write in progress or a killed one (hot journal): SQLite must see it
    return ["IMMUTABLE=YES"]


def ro_sqlite(path, timeout=30, immutable=None):
    """sqlite3 connection for READING an lmap-made SQLite file (gpkg/mbtiles/sqlitedb): immutable=1 like
    ro_gpkg_opts (no lock probes on a share), unless a -journal/-wal/-shm says a write is (or was) in progress.
    The path is URI-quoted: '?', '#' or '%' in a folder name must not cut the file name.
    Safe because every lmap SQLite output is written as a .part file and renamed over (install_file invariant): a
    finished file is never written in place, so there is no side-file-less write to miss.
    A side file that read-only locking can't use (a hot -journal from a crashed writer that can't be rolled back
    read-only, or a -wal whose -shm can't be made on a read-only share): fall back to immutable."""
    import sqlite3, urllib.parse
    p = os.path.abspath(str(path)); u = f"file:{urllib.parse.quote(p)}?mode=ro"
    if immutable or (immutable is None and not any(os.path.exists(p + x) for x in ("-journal", "-wal", "-shm"))):
        return sqlite3.connect(u + "&immutable=1", uri=True)
    c = sqlite3.connect(u, uri=True, timeout=timeout)
    try:
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return c
    except sqlite3.OperationalError as e:
        c.close()
        if "locked" in str(e) or "busy" in str(e):
            raise                                 # a live writer: let the caller see it (not a stale read)
        return sqlite3.connect(u + "&immutable=1", uri=True)


def ro_ogr(path):
    """ogr.Open(path) for READING, with ro_gpkg_opts (no SQLite locking on a share). None if it can't be opened,
    or raises when ogr/gdal exceptions are on - as ogr.Open does. Returns a gdal.Dataset (same vector API)."""
    from osgeo import gdal, ogr
    try:
        ds = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_VERBOSE_ERROR, open_options=ro_gpkg_opts(path))
    except RuntimeError:
        if ogr.GetUseExceptions():
            raise
        return None
    if ds is None and ogr.GetUseExceptions():          # gdal exceptions off, ogr's on: raise as ogr.Open did
        raise RuntimeError(gdal.GetLastErrorMsg() or f"{path}: not recognized as a supported file format")
    return ds


def gdal_open(path, update=False, what=None, vector=False, hint=None):
    """Open a raster (or vector=True: any dataset via OpenEx) or raise CliError naming the file. gdal.Open returns
    None or raises depending on the process-wide exceptions switch; callers that went straight on to
    ds.GetRasterBand() then died with "'NoneType' has no attribute". The switch is restored as found."""
    from osgeo import gdal
    ex = gdal.GetUseExceptions(); gdal.UseExceptions(); why = None
    try:
        if vector:
            ds = gdal.OpenEx(str(path), gdal.OF_VECTOR | (gdal.OF_UPDATE if update else 0),
                             open_options=[] if update else ro_gpkg_opts(path))
        else:
            ds = gdal.Open(str(path), gdal.GA_Update if update else gdal.GA_ReadOnly)
    except RuntimeError as e:
        ds, why = None, str(e).strip()
    finally:
        if not ex:
            gdal.DontUseExceptions()
    if ds is None:
        raise CliError(E_FAIL, f"{what or Path(str(path)).name}: can't open ({why or 'not readable by GDAL'})",
                       hint or "check the file is there and complete; re-run the step that builds it")
    return ds


def file_check(name):
    """Integrity check for a downloaded file by its type (also used when the server sends no Content-Length, so
    a cut-short body can't pass): zip -> unzip -t; .pbf -> osmium fileinfo reads it to the end; .gpkg/.sqlite ->
    SQLite quick_check; raster -> full GDAL decode. Others: None (size check only)."""
    n = name.lower()
    if n.endswith(".zip"):
        return zip_ok
    if n.endswith(".pbf") and shutil.which("osmium"):
        return lambda p: _check_cmd(["osmium", "fileinfo", "-e", "-g", "data.count.nodes", str(p)])
    if n.endswith((".gpkg", ".sqlite", ".mbtiles")):
        def sq(p):
            import sqlite3
            try:
                c = ro_sqlite(p, immutable=True)   # a just-downloaded file nobody writes: never lock-probe CIFS
                try:
                    return c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                finally:
                    c.close()
            except sqlite3.Error:
                return False
        return sq
    if n.endswith((".tif", ".tiff")):
        return lambda p: gdal_ok(p, deep=True)
    return None


def zip_ok(p):
    """unzip -t, abort-aware (a big zip test no longer outlives stop --now)."""
    return _check_cmd(["unzip", "-tqq", str(p)])


def _check_cmd(cmd):
    """Run a file check -> exit 0? Abort-aware: stop --now kills it (its process group) instead of waiting."""
    if ABORT.is_set():
        raise Aborted()
    pr = child(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, preexec_fn=_pdeath))
    try:
        while True:
            try:
                return pr.wait(timeout=1) == 0
            except subprocess.TimeoutExpired:
                if ABORT.is_set():
                    try:
                        os.killpg(pr.pid, signal.SIGKILL)
                    except OSError:
                        pr.kill()
                    pr.wait(); raise Aborted()
    finally:
        unchild(pr)


def _same_inode(p, fd):
    """fd is still the file at path p (not unlinked or replaced since it was opened)."""
    try:
        a, b = os.stat(p), os.fstat(fd)
    except OSError:
        return False
    return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)


class Lock:
    """One fetch per source at a time (or, wait=ctx, wait for the holder instead of failing)."""
    def __init__(self, p, wait=None, drain=False):
        self.p, self.wait, self.drain = Path(p), wait, drain   # drain: a stop (DRAIN) also ends the wait

    def __enter__(self):
        self.p.parent.mkdir(parents=True, exist_ok=True)
        # Open WITHOUT truncating: a loser must not wipe the winner's pid.
        self.fd = os.open(self.p, os.O_RDWR | os.O_CREAT, 0o600)
        said = False
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if _same_inode(self.p, self.fd):
                    break
                # `lmap clean` unlinked this lock file (only ever while holding it): ours is an orphaned inode
                fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
                self.fd = os.open(self.p, os.O_RDWR | os.O_CREAT, 0o600); continue
            except (BlockingIOError, PermissionError):
                if self.wait is None:
                    os.close(self.fd)
                    raise CliError(E_FAIL, f"another fetch holds {self.p}", "see `lmap job list`")
                if not said:
                    said = True; self.wait.log(f"waiting for another lmap run ({self.p.name})")
                if ABORT.wait(3 + random.random()) or (self.drain and DRAIN.is_set()):
                    os.close(self.fd); raise Aborted()
        # pid written in one pwrite at offset 0 over the old one, then the tail trimmed: a reader never sees
        # an empty file mid-write (0.12 truncated first; `job start` could read "" and spawn a duplicate)
        b = str(os.getpid()).encode(); os.pwrite(self.fd, b, 0); os.ftruncate(self.fd, len(b))
        return self

    def __exit__(self, *e):
        # Empty it, don't unlink: only `lmap clean` unlinks a lock (old, while holding it; waiters re-check the inode).
        try:
            os.ftruncate(self.fd, 0)
        except OSError:
            pass
        fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)


def raster_info(p):
    """type / bands / nodata / scale / offset of band 1 (gdalinfo -json)."""
    j = json.loads(subprocess.run(["gdalinfo", "-json", str(p)], capture_output=True, text=True, check=True).stdout)
    b = j["bands"][0]
    nd = b.get("noDataValue")
    return {"type": b["type"], "bands": len(j["bands"]), "nodata": None if nd is None else str(nd),
            "scale": b.get("scale"), "offset": b.get("offset")}


def gdal_ok(p, deep=False):
    """Header check (fast). deep=True also decodes every block (-checksum): catches truncated tiles,
    which open fine but fail mid-read. gdalinfo exits 0 even then, so look for ERROR on stderr.
    Abort-aware: a stop kills gdalinfo and raises Aborted (never returns False, which would mark
    a good tile corrupt and delete it)."""
    if ABORT.is_set():
        raise Aborted()
    pr = child(subprocess.Popen(["gdalinfo"] + (["-checksum"] if deep else []) + [str(p)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=_pdeath))
    try:
        return _gdal_ok_wait(pr, deep)
    finally:
        unchild(pr)


def _gdal_ok_wait(pr, deep):
    while True:
        try:
            _, err = pr.communicate(timeout=1); break
        except subprocess.TimeoutExpired:
            if ABORT.is_set():
                try:
                    os.killpg(pr.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pr.kill()
                pr.communicate(); raise Aborted()
    return pr.returncode == 0 and not (deep and "ERROR" in err)


_DECODE = r'''
import sys
from osgeo import gdal
gdal.UseExceptions(); gdal.SetCacheMax(192 << 20)
p, k, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ds = gdal.Open(p); b0 = ds.GetRasterBand(1)
bw, bh = b0.GetBlockSize(); bh = max(bh, 256); cw = max(bw, 8192 // bw * bw)
levels = [[ds.GetRasterBand(i + 1) for i in range(ds.RasterCount)]]
levels += [[ds.GetRasterBand(i + 1).GetOverview(o) for i in range(ds.RasterCount)] for o in range(b0.GetOverviewCount())]
if b0.GetMaskFlags() & gdal.GMF_PER_DATASET and not b0.GetMaskFlags() & gdal.GMF_ALL_VALID:
    m = b0.GetMaskBand(); levels += [[m]] + [[m.GetOverview(o)] for o in range(m.GetOverviewCount())]
i = 0
for bands in levels:
    X, Y = bands[0].XSize, bands[0].YSize
    for y in range(0, Y, bh):
        for x in range(0, X, cw):
            if i % n == k:
                for b in bands:
                    b.ReadRaster(x, y, min(cw, X - x), min(bh, Y - y))
            i += 1
'''


def deep_decode(ctx, p, n=None):
    """Full block decode (base, every overview, mask + mask overviews) split over n processes: gdalinfo
    -checksum is one thread, ~40 min on a 37 GB WEBP COG. Same answer, ~n times faster. Abort-aware.
    Returns None if clean, else the first error text."""
    n = n or max(1, min(8, cpu_cap()))
    if ABORT.is_set():
        raise Aborted()
    env = {**os.environ, "GDAL_NUM_THREADS": "1"}
    # stderr to files, not pipes: a damaged file can make GDAL print a warning per block, and an unread pipe
    # fills at 64 KB and stalls the child for good
    errs = [tempfile.TemporaryFile("w+b") for _ in range(n)]
    ps = [child(subprocess.Popen([sys.executable, "-c", _DECODE, str(p), str(k), str(n)], stdout=subprocess.DEVNULL,
                                 stderr=errs[k], start_new_session=True, env=env,
                                 preexec_fn=_pdeath)) for k in range(n)]
    try:
        while any(pr.poll() is None for pr in ps):
            if ABORT.wait(1):
                raise Aborted()
            if any(pr.poll() not in (None, 0) for pr in ps):
                break                                       # one failed: no need to finish the rest
        bad = [k for k, pr in enumerate(ps) if pr.poll() not in (None, 0)]
        if bad:
            f = errs[bad[0]]; f.seek(0, 2); f.seek(max(0, f.tell() - 4096))     # the tail holds the error
            err = f.read().decode(errors="replace")
            return ((err or "decode failed").strip().splitlines() or ["decode failed"])[-1][-300:]
        return None
    finally:
        for pr in ps:
            if pr.poll() is None:
                try:
                    os.killpg(pr.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pr.kill()
            pr.wait(); unchild(pr)
        for f in errs:
            f.close()

def du(p):
    p = Path(p)
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.glob("*") if f.is_file()) if p.is_dir() else 0


def du_tree(p):
    """Bytes under p, subfolders included (build inputs keep their data in z<zoom>/ etc.)."""
    p = Path(p)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else du(p)


class Meter:
    def __init__(self):
        self.lock, self.bytes = threading.Lock(), 0

    def add(self, n):
        with self.lock:
            self.bytes += n


class Progress:
    """Writes ~/.local/state/lmap/<id>.json so `lmap job status` can show rate + ETA."""
    def __init__(self, s, total, have):
        self.p = STATE / f"{s['id']}.json"; self.total, self.have = total, have
        self.build_s = build_est_s(s) if s.get("out") else 0   # the COG build after the units (108a)
        self.start, self.recent, self.done, self.failed, self.skipped = time.time(), [], 0, 0, 0
        self.extra = {}; self.last_write = 0; self._lk = threading.RLock()
        self.wk = None; self.samples = [(self.start, 0.0)]; self.eta = None   # measured ETA (113): work samples
        STATE.mkdir(parents=True, exist_ok=True)

    def tick(self, ok=True, **extra):
        with self._lk:
            self._tick(ok, **extra)

    def _tick(self, ok=True, **extra):
        now = time.time()
        if ok is True:
            self.done += 1; self.recent.append(now)
        elif ok is False:
            self.failed += 1
        elif ok == "skip":                     # e.g. Copernicus ocean 404: progress, but not rate
            self.skipped += 1
        self.recent = [t for t in self.recent if now - t < 1200]   # 20 min window
        self.extra.update(extra)
        if ok is None and now - self.last_write < 10:
            return
        self.write(now)

    SLICE_S, WINDOW_S = 30, 1200

    def work(self, done, total):
        """Finer-than-unit progress (e.g. tiles fetched of tiles needed): the ETA is measured on this instead of
        whole units, so a run of a few huge units still gets an ETA before its first unit ends."""
        with self._lk:
            self.wk = (float(done), float(max(total, done, 1)))

    def measured_eta(self, now):
        """None ('estimating') until a first slice of real work has been timed; then work left / measured rate
        (20-min window), smoothed against the previous estimate counted down, so it neither jumps nor sticks."""
        if self.wk:
            w, tot = self.wk
        else:
            w, tot = float(self.done + self.skipped), float(max(self.total - self.have, 1))
        if not self.samples or w < self.samples[-1][1]:
            self.samples = [(now, w)]
        elif now - self.samples[-1][0] >= 5 or w != self.samples[-1][1]:
            self.samples.append((now, w))
        old = [s for s in self.samples if now - s[0] >= self.WINDOW_S]
        self.samples = old[-1:] + [s for s in self.samples if now - s[0] < self.WINDOW_S]
        t0, w0 = self.samples[0]
        if w >= tot:
            self.eta = 0; return 0
        if w <= w0 or now - t0 < self.SLICE_S:
            if not self.eta:
                return None                                # nothing timed yet: estimating
            return int(max(self.eta - (now - self._eta_t), 60))   # a stall: keep counting the last one down
        raw = (tot - w) / ((w - w0) / (now - t0))
        if self.eta:
            prev = max(self.eta - (now - self._eta_t), 0)
            raw = 0.6 * prev + 0.4 * raw
        self.eta, self._eta_t = raw, now
        return int(raw)

    def rate_h(self, now):
        span = min(now - self.start, 1200)
        return len(self.recent) / span * 3600 if span > 60 and self.recent else None

    def write(self, now=None, final=None):
        now = now or time.time(); r = self.rate_h(now)
        left = self.total - self.have - self.done - self.skipped
        d = {"updated": now, "started": self.start, "total": self.total, "have": self.have + self.done,
             "left": left, "this_run_done": self.done, "this_run_failed": self.failed,
             "rate_per_h": round(r, 1) if r else None,
             "eta_s": self.measured_eta(now) if left > 0 else None, "final": final, **self.extra}
        d["estimating"] = d["eta_s"] is None and left > 0
        self.last_write = now
        RUN.part(self.p.stem, done=d["have"], total=self.total,                  # run registry: units + the build
                 eta_s=(d["eta_s"] + self.build_s) if d["eta_s"] is not None else (self.build_s or None) if left <= 0 else None)
        with self._lk:
            try:                                        # progress is advisory: it must never fail a unit
                atomic_write(self.p, json.dumps(d))
            except OSError:
                pass


class Tuner:
    """Hill-climbs download concurrency on measured throughput. Starts at half the cap and doubles while
    that helps, then steps by 1: only up if the last step bought >=8% more bytes/s, back down when fewer
    is just as fast. Halves at once on a failed unit (server unhappy). A window with fewer than
    `min_units` finished units says nothing (slow units) and is extended, not judged.
    Re-probes every 10 min because link speed drifts. Never exceeds cap."""
    WINDOW, GAIN, FORGET = 40, 1.08, 600

    def __init__(self, cap, window=None, min_units=0):
        self.lk, self.last_cut = threading.RLock(), 0.0
        self.cap, self.c = cap, max(1, min(cap, max(2, cap // 2) if min_units else 2))
        if window:
            self.WINDOW = window
        self.min_units, self.units, self.doubling = min_units, 0, bool(min_units)
        self.t0, self.b0, self.sat = time.time(), 0, True
        self.best, self.rate, self.forgot = {}, None, time.time()

    def busy(self, n):
        if n < self.c:
            self.sat = False          # held back by shrink/disk: this window says nothing about c

    def unit(self, ok=True):
        """A work unit finished. ok=False (it failed) backs off at once - once per burst: 16 requests failing in
        the same network blip halve once, not 16 times (16 -> 1 took ~7 min to climb back)."""
        with self.lk:
            if ok:
                self.units += 1
                return
            now = time.time()
            if now - self.last_cut < max(5, self.WINDOW / 2):
                return
            self.last_cut = now
            self.c = max(1, self.c // 2); self.doubling = False; self.best = {}
            self.t0, self.sat, self.units = now, True, 0

    def tick(self, total_bytes):
        with self.lk:
            self._tick(total_bytes)

    def _tick(self, total_bytes):
        now = time.time()
        if now - self.t0 < self.WINDOW or self.units < self.min_units:
            return
        rate = (total_bytes - self.b0) / (now - self.t0)
        sat, self.t0, self.b0, self.sat, self.rate, self.units = self.sat, now, total_bytes, True, rate, 0
        if not sat:
            return
        if self.doubling:
            prev = self.best.get("last")
            if prev is None or rate > prev * self.GAIN:
                self.best["last"] = rate
                if self.c < self.cap:
                    self.c = min(self.cap, self.c * 2); return
            self.doubling = False; self.best = {}
            return
        if now - self.forgot > self.FORGET:
            self.best = {}; self.forgot = now
        old = self.best.get(self.c)
        self.best[self.c] = rate if old is None else (old + rate) / 2
        cur, up, dn = self.best[self.c], self.best.get(self.c + 1), self.best.get(self.c - 1)
        if self.c < self.cap and (up is None or up > cur * self.GAIN):
            self.c += 1
        elif self.c > 1 and dn is not None and dn * self.GAIN >= cur:
            self.c -= 1


DISK_CAPPED = set()       # st_dev of scratch filesystems (not the data root's): the [limits] disk cap applies there
DISK_MARGIN = 5e9         # everywhere else: write until the disk is full less this margin


def free_bytes(p, raw=False):
    """Space lmap may still use on p's filesystem. Scratch (when it is not on the data root's filesystem): free space
    down to the [limits] disk cap (default: keep 20% of the disk free) - runaway temp files can't fill the disk.
    The data root and other disks (often shared, holding other people's files): free space less a 5 GB margin;
    `lmap status` warns when the root is past the cap. Negative = no room. raw=True: the plain free space."""
    st = os.statvfs(p)
    free = st.f_bavail * st.f_frsize
    if raw:
        return free
    if _dev(p) in DISK_CAPPED:
        cap = (LIMITS or {}).get("disk", LIMIT_DEFAULT)
        return free - st.f_blocks * st.f_frsize * (100 - cap) / 100
    return free - DISK_MARGIN


def room(p):
    """'N GB free' for messages; adds what is usable when the disk cap or margin is what refuses."""
    raw, use = free_bytes(p, raw=True) / 1e9, max(0, free_bytes(p)) / 1e9
    lim = f"the {(LIMITS or {}).get('disk', LIMIT_DEFAULT)}% disk limit" if _dev(p) in DISK_CAPPED else "a 5 GB margin"
    return f"{raw:.1f} GB free ({use:.1f} GB usable within {lim})"


def _dev(p):
    """st_dev of p, or of its nearest existing parent (the root may not exist yet)."""
    p = Path(p).absolute()
    for q in (p, *p.parents):
        try:
            return os.stat(q).st_dev
        except OSError:
            continue
    return None



class _HotSkip(Exception):
    pass


HOT_MAX = 4e9      # bigger inputs (statewide contours, 5-16 GB) are read in place: the copy costs more than it saves


@contextlib.contextmanager
def hot_copy(ctx, src, why="", max_bytes=HOT_MAX):
    """`with hot_copy(ctx, src) as p:` a local copy of a big read-mostly input that lives on another filesystem (a
    network share): GDAL/OGR drivers that make many small reads run several times faster from local disk (OSM pbf over CIFS:
    101 s -> 25 s). Kept in <scratch>/hot/ as one file per source version (name = hash of the source path + hash of
    its size/mtime/inode), so a reader never sees a copy change or vanish: it holds a shared lock on its version for
    the whole `with`, and an old version is deleted only when nobody holds it. Yields the source itself (never fails
    the caller) when it is local, bigger than max_bytes, the copy wouldn't leave room under the disk limits, or the
    copy fails."""
    import hashlib
    src = Path(src); fd = None; use = src         # every yield is outside the try: an error in the caller's
    try:                                           # body must not land in this except (would yield twice)
        st = src.stat()
        if _dev(src) == _dev(ctx.scratch()) or st.st_size > max_bytes:
            raise _HotSkip()
        w_ = Path(str(src) + "-wal")                   # SQLite WAL not checkpointed: the file alone is stale
        if w_.is_file() and w_.stat().st_size:
            raise _HotSkip()
        hd = ctx.scratch() / "hot"; hd.mkdir(exist_ok=True)
        h = lambda x: hashlib.sha1(x.encode()).hexdigest()
        pre = h(str(src.absolute()))[:10]
        dst = hd / f"{pre}-{h(f'{st.st_size}/{st.st_mtime_ns}/{st.st_ino}')[:8]}-{src.name}"
        lk = os.open(hd / f"{pre}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)                     # one copier per source
            for old in hd.glob(f"{pre}-*"):                    # older versions + a killed copy's .part: drop the
                if old == dst or old.name.endswith(("-wal", "-shm", "-journal")):   # SQLite sidecars:
                    continue                           # fcntl-locked, not flock; readers open copies IMMUTABLE
                try:
                    o = os.open(old, os.O_RDONLY)
                except OSError:
                    continue
                try:
                    fcntl.flock(o, fcntl.LOCK_EX | fcntl.LOCK_NB); old.unlink()
                except OSError:
                    pass
                finally:
                    os.close(o)
            if not (dst.is_file() and dst.stat().st_size == st.st_size):
                if free_bytes(hd) - st.st_size < 2e9:
                    ctx.vlog(f"hot copy of {src.name} skipped: {room(hd)}")
                    raise _HotSkip()
                t0, part = time.time(), Path(str(dst) + ".part")
                try:
                    with open(src, "rb") as a, open(part, "wb") as b:
                        shutil.copyfileobj(a, b, 16 << 20)
                    s2 = src.stat()
                    if part.stat().st_size != st.st_size or (s2.st_mtime_ns, s2.st_ino) != (st.st_mtime_ns, st.st_ino):
                        raise OSError("source changed during the copy")
                    part.rename(dst)
                finally:
                    part.unlink(missing_ok=True)
                ctx.vlog(f"hot copy of {src.name} ({st.st_size / 1e9:.1f} GB{', ' + why if why else ''}) "
                         f"in {time.time() - t0:.0f} s")
            fd = os.open(dst, os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_SH)   # before the copy lock goes: no gap
            os.utime(dst)                                      # last use, for `lmap clean`
            use = dst
        finally:
            os.close(lk)
    except _HotSkip:
        pass
    except (OSError, ValueError) as e:
        if fd is not None:
            os.close(fd); fd = None
        ctx.vlog(f"hot copy of {src.name} failed ({e}); reading it in place")
    except BaseException:                          # Ctrl-C / exit during setup: don't hold the shared lock
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield use
    finally:
        if fd is not None:
            os.close(fd)
            if use != src:                             # last reader gone: delete it (scratch must not accumulate)
                try:
                    lk2 = os.open(use.parent / (use.name.split("-")[0] + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        fcntl.flock(lk2, fcntl.LOCK_EX)    # no copier/reader between its check and its shared lock
                        o = os.open(use, os.O_RDONLY)
                        try:
                            fcntl.flock(o, fcntl.LOCK_EX | fcntl.LOCK_NB); use.unlink()
                        except OSError:
                            pass                       # another reader still has it: the last one deletes it
                        finally:
                            os.close(o)
                    finally:
                        os.close(lk2)
                except OSError:
                    pass



def disk_used_pct(p):
    st = os.statvfs(p)
    return round(100 * (1 - st.f_bavail / max(1, st.f_blocks)), 1)


def is_tmpfs(p):
    """True if path p lives on a RAM filesystem (tmpfs/ramfs)."""
    try:
        p = os.path.realpath(p); best, fs = "", ""
        for line in open("/proc/self/mounts"):
            mp, t = line.split()[1], line.split()[2]
            if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                best, fs = mp, t
        return fs in ("tmpfs", "ramfs")
    except OSError:
        return False


# ---------------------------------------------------------------- config show
# Every setting: (config key, env var, command flag attr or None). Precedence everywhere: flag > env > config > default.
CONF_KEYS = (("root", "LMAP_ROOT", "root"), ("catalog", "LMAP_CATALOG", "catalog"), ("tmp", "LMAP_TMP", "tmp"),
             ("tiles", "LMAP_TILES", None), ("jobs", "LMAP_JOBS", "jobs"), ("shrink", "LMAP_SHRINK", "shrink"),
             ("builds", "LMAP_BUILDS", None), ("bandwidth", "LMAP_BANDWIDTH", "bandwidth"),
             ("webp", "LMAP_WEBP", None), ("gpu", "LMAP_GPU", None), ("queue", "LMAP_QUEUE", None),
             ("ua", "LMAP_UA", None), ("proxy", "LMAP_PROXY", None), ("quality", "LMAP_QUALITY", None),
             ("contour_mm", "LMAP_CONTOUR_MM", None), ("index_ratio", "LMAP_INDEX_RATIO", None), ("photo_dpi", "LMAP_PHOTO_DPI", None))


def cmd_config_show(ctx):
    """Each setting's value in effect and where it came from, so a wrong root/scratch/limit is easy to trace. Works
    from the raw file when lmap rejects the config (ctx.error): the rows show what is set and the error says why."""
    try:
        raw = tomllib.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CliError(E_USAGE, f"{CONFIG}: {e}", "fix the file (TOML: [paths] root = \"...\", [limits] cpu = 80)")
    err0 = None
    try:
        cfg = read_config(raw)
    except CliError as e:                         # show what is there anyway; the error says why
        err0 = e.msg; cfg = {}
        for k, v in raw.items():
            if k not in CONF_SECTIONS:
                cfg.setdefault(k, v)
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    cfg[kk if kk in CONF_WHERE else f"{k}.{kk}"] = vv
    err = getattr(ctx, "error", None) or err0; a = ctx.a
    cfg_in = lambda k: k in cfg if k in ("webp", "gpu", "queue") else bool(cfg.get(k))   # as Ctx reads each key
    if err is None:
        eff = {"root": str(ctx.root), "catalog": str(ctx.catalog), "tmp": str(ctx.tmp),
               "tiles": str(ctx.tiles_dir or Path(ctx.tmp) / f"lmap-{os.getuid()}" / "tiles"),
               "jobs": ctx.jobs, "shrink": ctx.shrink, "builds": BUILD_SLOTS,
               "bandwidth": f"{BW.rate / 1e6:.3g} MB/s" if BW.rate else "unlimited", "webp": "yes" if ctx.webp else "no",
               "quality": ctx.quality,
               "gpu": str(os.environ.get("LMAP_GPU") or ctx.gpu), "queue": ctx.queue_mode, "ua": UA,
               "proxy": os.environ.get("LMAP_PROXY") or cfg.get("proxy") or "none"}
        try:
            _cw = contour_widths()
            eff.update(contour_mm=_cw[0], index_ratio=round(_cw[1] / _cw[0], 3))
        except CliError as e:
            eff.update(contour_mm=f"bad: {e.msg}", index_ratio=f"bad: {e.msg}")
        eff["photo_dpi"] = os.environ.get("LMAP_PHOTO_DPI") or cfg.get("photo_dpi") or 1200   # photo sheets (topo 300)
    else:
        eff = {k: (getattr(a, f, None) if f else None) or os.environ.get(env) or cfg.get(k, "(default)")
               for k, env, f in CONF_KEYS}
    if eff["gpu"] == "auto":
        eff["gpu"] = "auto (used when nvcc + an NVIDIA driver work)"
    eff["proxy"] = mask_url(eff["proxy"]) if eff["proxy"] not in (None, "none") else "none"
    rows = []
    for k, env, flag in CONF_KEYS:
        src = ("flag" if flag and getattr(a, flag, None) else "env " + env if os.environ.get(env)
               else "config" if cfg_in(k) else "default")
        rows.append({"key": k, "value": eff[k], "from": src, "section": CONF_WHERE[k]})
    lt = cfg.get("limits") if isinstance(cfg.get("limits"), dict) else {}
    for k in LIMIT_KEYS:
        env = f"LMAP_LIMIT_{k.upper()}"
        src = "env " + env if os.environ.get(env) else "config" if lt.get(k) else "default"
        v = ctx.limits[k] if err is None else (os.environ.get(env) or lt.get(k) or LIMIT_DEFAULT)
        rows.append({"key": f"limits.{k}", "value": f"{v}%", "from": src, "section": "limits"})
    for t in ("hosts", "servers"):
        tab = cfg.get(t) if isinstance(cfg.get(t), dict) else {}
        for h, v in sorted(tab.items()):
            v = ", ".join(f"{x}={y}" for x, y in v.items()) if isinstance(v, dict) else v
            rows.append({"key": f"{t}.{h}", "value": v, "from": "config", "section": t})
    known = {k for k, _, _ in CONF_KEYS} | set(CONF_TABLES) | set(CONF_SECTIONS)
    unknown = sorted(set(cfg) - known) + sorted(f"limits.{x}" for x in set(lt) - set(LIMIT_KEYS))
    res = {"ok": not unknown and err is None, "action": "config.show", "file": str(CONFIG), "exists": CONFIG.is_file(),
           "settings": rows}
    if err:
        res["error"] = err
        if getattr(ctx, "error_hint", None):
            res["hint"] = ctx.error_hint
    if unknown:
        res["unknown"] = unknown
        res["hint"] = ((res["hint"] + "; ") if res.get("hint") else "") + (f"{CONFIG}: unknown key(s) {unknown} are ignored (typo?). Known: "
                       + "; ".join(f"[{sec}] {', '.join(ks)}" for sec, ks in CONF_SECTIONS.items())
                       + f"; [limits] {', '.join(LIMIT_KEYS)}; [hosts]; [servers.\"host\"]")
    return res
