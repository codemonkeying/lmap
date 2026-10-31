# lmap: base. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# HERE (the repo dir) is set by bin/lmap before this file runs.
# Defaults for a fresh clone: finished data in XDG data, scratch in XDG cache (a local disk, not RAM /tmp).
# Point them anywhere (same disk is fine): [paths] root = / tmp = in config, LMAP_ROOT / LMAP_TMP, --root / --tmp.
try:    # GDAL >= 3.9 warns "Neither UseExceptions() nor DontUseExceptions()" on every verb: lmap's code expects
    # the classic None-returning mode (spots that want exceptions switch them on and back), so say so once.
    from osgeo import gdal as _g, ogr as _o, osr as _s
    _g.DontUseExceptions(); _o.DontUseExceptions(); _s.DontUseExceptions()
    del _g, _o, _s
    if "PYTHONWARNINGS" not in os.environ:          # embedded `python3 -c` children don't load this file
        os.environ["PYTHONWARNINGS"] = "ignore::FutureWarning:osgeo.gdal,ignore::FutureWarning:osgeo.ogr,ignore::FutureWarning:osgeo.osr"
except ImportError:
    pass


def _xdg(var, fallback):
    v = os.environ.get(var)
    return Path(v) if v and os.path.isabs(v) else Path.home() / fallback    # XDG spec: ignore relative values


def _c_locale():
    """Children (gdalinfo, ogrinfo, 7z, grass, gdaltransform) print numbers and messages that lmap parses: run them
    in the C locale, so "1,5" decimals or translated text can't change what we read. UTF-8 kept for file names."""
    import locale
    for name in ("C.UTF-8", "C.utf8"):
        try:
            old = locale.setlocale(locale.LC_CTYPE); locale.setlocale(locale.LC_CTYPE, name)
            locale.setlocale(locale.LC_CTYPE, old); return name
        except locale.Error:
            pass
    return "C"


os.environ["LC_ALL"] = _c_locale(); os.environ.pop("LANGUAGE", None)


DEFAULT_ROOT = str(_xdg("XDG_DATA_HOME", ".local/share") / "lmap")
DEFAULT_TMP = str(_xdg("XDG_CACHE_HOME", ".cache") / "lmap")
STATES_GPKG = HERE / "share" / "au-states.gpkg"     # OSM admin_level 4, simplified: files areas/maps by state
CONFIG = _xdg("XDG_CONFIG_HOME", ".config") / "lmap" / "config.toml"
TCC_BLOB = "https://tccspatialbulkassets.blob.core.windows.net/aerial"
# Identifies the tool to servers, as their usage policies ask. Override: LMAP_UA / config ua=.
DEFAULT_UA = "lmap/1.0 (+https://github.com/codemonkeying/lmap)"
UA = DEFAULT_UA
class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    """Refuse an https -> http redirect: the rest of the download would travel unencrypted and could be swapped."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.full_url.lower().startswith("https:") and urllib.parse.urljoin(req.full_url, newurl).lower().startswith("http:"):
            raise urllib.error.HTTPError(req.full_url, code, f"refused redirect from https to {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def mask_url(u):
    """A URL with any user:password replaced by ***, for logs and JSON (proxy settings carry credentials)."""
    u = str(u); m = re.match(r"([a-z][a-z0-9+.-]*://)?(.*)", u, re.I | re.S)
    pre, rest = m.group(1) or "", m.group(2)
    return pre + "***@" + rest.rsplit("@", 1)[1] if "@" in rest else u   # last @: a password may hold @ or /


SQL_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def sql_ident(x, what):
    """A table/field name going into SQL text (from the catalog or a server's metadata): plain identifiers only."""
    if not isinstance(x, str) or not SQL_IDENT.fullmatch(x):
        raise CliError(E_USAGE, f"{what} {x!r} is not a plain name (letters, digits, _)", "fix the catalog entry")
    return x


OPENER = urllib.request.build_opener(_NoDowngrade())   # replaced in Ctx if a proxy is configured
STATE = _xdg("XDG_STATE_HOME", ".local/state") / "lmap"
DRAIN = threading.Event()   # stop: finish in-flight tiles, start nothing new, exit
ABORT = threading.Event()   # stop --now: abandon in-flight tiles, clean up, exit
STOP_BUILDS = threading.Event()   # kill our GDAL children (Aborted) without ABORT's exit-hard watchdog


class Aborted(Exception):
    pass


BW_IDLE_S = 30      # a process that took no download bytes for this long gets no share of the bandwidth cap


class TokenBucket:
    """Shared download cap for the whole process (all jobs, all threads). rate 0 = unlimited."""
    def __init__(self, rate=0):
        self.lock, self.rate, self.tokens, self.t = threading.Lock(), float(rate), float(rate), time.time()
        self.reg, self._n = threading.Lock(), 1

    def share(self):
        """The cap is machine-wide: split it over the lmap processes downloading right now (a live process
        (flock'd file) counts only while it took bytes in the last BW_IDLE_S s - its file's mtime - so one that
        moved on to building no longer halves the others' share)."""
        now = time.time()
        if now - getattr(self, "_touch", 0) > BW_IDLE_S:     # back from idle: others dropped us, re-count now
            self._st = 0                                    # (they see us within their 15 s cache)
        if now - getattr(self, "_touch", 0) >= 5 and getattr(self, "_fd", None) is not None:
            try:
                os.utime(self._fd); self._touch = now           # "still downloading"
            except OSError:
                pass
        if now - getattr(self, "_st", 0) < 15:
            return self._n
        d = STATE / "bw"; d.mkdir(parents=True, exist_ok=True)
        with self.reg:                                     # once per process (threads race here otherwise)
            if getattr(self, "_fd", None) is None:
                tmp = d / f".new-{os.getpid()}-{threading.get_ident()}"
                fd = os.open(tmp, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.rename(tmp, d / f"p{os.getpid()}"); self._fd = fd; self._touch = now
            if now - getattr(self, "_st", 0) < 15:
                return self._n
            self._st = now
        mine, n = d / f"p{os.getpid()}", 1
        for f in d.glob("p*"):
            if f == mine:
                continue
            try:
                fd = os.open(f, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    if os.fstat(fd).st_ino == os.stat(f).st_ino:
                        f.unlink()                         # its process is gone
                except OSError:
                    pass
            except BlockingIOError:
                try:
                    if now - os.fstat(fd).st_mtime < BW_IDLE_S:   # (STATE is local: one clock)
                        n += 1                             # alive and downloading
                except OSError:
                    pass
            finally:
                os.close(fd)
        self._n = n
        return n

    def take(self, n):
        if self.rate <= 0:
            return
        k = self.share()
        with self.lock:
            now = time.time(); rate = self.rate / k
            self.tokens = min(rate, self.tokens + (now - self.t) * rate); self.t = now
            self.tokens -= n
            wait = -self.tokens / rate if self.tokens < 0 else 0
        if wait and ABORT.wait(wait):
            raise Aborted()


def parse_rate(v):
    """'20MiB' / '20MB' / '160Mbit' / bytes -> bytes per second. 0/''/none = unlimited."""
    if v in (None, "", 0, "0", "none", "off"):
        return 0
    m = re.fullmatch(r"\s*([\d.]+)\s*(k|ki|m|mi|g|gi)?(b|bit|byte)?(/s)?\s*", str(v), re.I)
    if not m:
        raise CliError(E_USAGE, f"bad bandwidth {v!r}", "e.g. 20MiB, 160Mbit, 0 = unlimited")
    num, pre, unit = float(m.group(1)), (m.group(2) or "").lower(), (m.group(3) or "B")
    mult = {"": 1, "k": 1e3, "ki": 1024, "m": 1e6, "mi": 1 << 20, "g": 1e9, "gi": 1 << 30}[pre]
    return num * mult / (8 if unit.lower() == "bit" or unit == "b" and m.group(3) == "b" else 1)


BW = TokenBucket(0)          # set from config `bandwidth` in Ctx
# ONE per-server table. start/max: lmap's own multi-connection paths (tile fetcher, Range chunks, file
# lists): start = connections to open with, max = never more. slots (default = max): the machine-wide cap on the
# slot files that single requests (http_get), the GDAL tile path and the cop/arc/STAC unit tuners take.
# Two numbers only where the old two tables differed (kept as they were). One change: Landgate SLIP (WA) tile
# path now uses its measured 4 too (it fell back to the default 8).
# Measured 2026-10-09/10 (40 s per level, fresh tiles, 0 errors at 16): nsw 41-47 tiles/s, tas-img 50, sa 60.
#   spatial-img (QLD aerial) linear to 48 (616 tiles/s, p50 0.07 s); 2026-10-16 re-probe: 48 318/s, 64 483/s,
#     96 509/s, 128 610/s; p95 0.15 -> 0.35 s from 64: max 64 (GDAL path: 12)
#   tiles.maps.eox.at 65 tiles/s at 24, a flood of 403s at 32: max 20 (GDAL path: 16)
#   TAS topo flattens above 8 (same host as TAS imagery: 12 is the compromise). DEA tops out ~12 (512px max).
#   Landgate SLIP halves its speed above 4. QLD topo (spatial-gis) is a small server: keep it low.
#   PGDA ~0.25 MB/s per connection, 8 gave 3.5x one; USGS ~2 MB/s on one (2026-10-15, Range requests).
#   Copernicus buckets are in Frankfurt (~335 ms away): ~0.15 MB/s per connection. Measured 2026-10-17:
#     16 -> 4 MB/s, 64 -> 9, 128 -> 15-17, 192 -> 20-21 (1 error), 256 -> 10 (worse). 160 = below the edge.
# Config: [servers."host"] start=, max=, slots=  ([hosts] "host" = N sets max and slots, older setting).
# [servers] max= lowers slots too but never raises them: raising the machine-wide cap takes an explicit slots=.
SERVERS = {"spatial-img.information.qld.gov.au": {"start": 16, "max": 64, "slots": 12},
           "tiles.maps.eox.at": {"start": 8, "max": 20, "slots": 16},
           "maps.six.nsw.gov.au": {"start": 8, "max": 16}, "location.sa.gov.au": {"start": 8, "max": 16},
           "services.thelist.tas.gov.au": {"start": 6, "max": 12}, "services.ga.gov.au": {"start": 4, "max": 8},
           "spatial-gis.information.qld.gov.au": {"start": 2, "max": 4},
           "services.slip.wa.gov.au": {"start": 4, "max": 4},
           "esa-worldcover.s3.eu-central-1.amazonaws.com": {"start": 8, "max": 32},   # S3: tuner finds the knee
           "esa-worldcover-s2.s3.eu-central-1.amazonaws.com": {"start": 4, "max": 8},
           "pgda.gsfc.nasa.gov": {"start": 8, "max": 16, "slots": 8},
           "planetarymaps.usgs.gov": {"start": 4, "max": 12, "slots": 8},
           "ows.dea.ga.gov.au": {"start": 4, "max": 8, "slots": 12},
           "dea-public-data.s3.ap-southeast-2.amazonaws.com": {"start": 4, "max": 8, "slots": 16},
           "copernicus-dem-30m.s3.amazonaws.com": {"start": 4, "max": 8, "slots": 160}}   # 90 m bucket: default
HOST_DEFAULT = 8
SERVER_CFG, HOSTS_CFG = {}, {}         # config [servers."host"] tables, [hosts] numbers (set in Ctx)


def server_profile(host):
    """{start, max, slots} for a server: SERVERS (or the default 4/8), then config [hosts], then [servers]."""
    p = dict(SERVERS.get(host, {"start": 4, "max": HOST_DEFAULT}))
    p.setdefault("slots", p["max"])
    if host in HOSTS_CFG:                                    # config [hosts]: older setting, caps both
        p["max"] = p["slots"] = int(HOSTS_CFG[host])
    c = SERVER_CFG.get(host, {})
    p.update(c)
    if "max" in c and "slots" not in c:                      # [servers] max= can only LOWER the slot cap
        p["slots"] = min(int(p["slots"]), int(c["max"]))
    p["max"] = max(1, int(p["max"])); p["slots"] = max(1, int(p["slots"]))
    p["start"] = max(1, min(p["max"], int(p["start"])))
    return p


def host_cap(host):
    """Machine-wide connection cap on a server for single requests and the GDAL / unit-tuner paths."""
    return server_profile(host)["slots"]
DEM30 = ("dem-au-30m", "dem-qld-30m", "dem-se-aus-30m")   # 30 m elevation, preferred first (national, then old state files)
_hosts, _hosts_lock = {}, threading.Lock()


class HostSlot:
    """One connection to a server for http_get, counted against the SAME machine-wide budget as the GDAL path
    (HostBudget's flock'd slot files STATE/hosts/<h>/slot<i>) minus what lmap's tile downloader holds there
    (tms_load).
    A crashed process's flocks vanish with it. The in-process semaphore stops this process's own threads
    spinning on files they can't get."""
    def __init__(self, h, n):
        self.h, self.n, self.sem = h, n, threading.BoundedSemaphore(n)
        self.d = STATE / "hosts" / (h or "_")
        self.tl = threading.local()

    def __enter__(self):
        self.sem.acquire()
        try:
            self.d.mkdir(parents=True, exist_ok=True)
            wait = 0.05
            while True:
                lim = max(1, self.n - tms_load(self.d))
                for i in random.sample(range(lim), lim):
                    fd = os.open(self.d / f"slot{i}", os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except (BlockingIOError, PermissionError):
                        os.close(fd); continue
                    self.tl.__dict__.setdefault("fds", []).append(fd)
                    return self
                if ABORT.is_set():
                    raise Aborted()
                time.sleep(wait * (0.5 + random.random())); wait = min(wait * 1.5, 1.0)
        except BaseException:
            self.sem.release(); raise

    def __exit__(self, *e):
        fd = self.tl.fds.pop()
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd); self.sem.release()


def host_slot(url):
    """Per-server concurrency limit across all lmap processes on this machine (polite to small servers)."""
    h = urllib.parse.urlsplit(url).hostname or ""
    with _hosts_lock:
        if h not in _hosts:
            _hosts[h] = HostSlot(h, host_cap(h))
        return _hosts[h]


def tms_load(d):
    """Connections lmap's own tile downloader (other processes, live ones) is using on this server now."""
    n = 0
    for f in Path(d).glob("tms-*"):
        try:
            fd = os.open(f, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN)   # got it: stale
        except BlockingIOError:
            try:
                n += int(os.pread(fd, 8, 0).decode().strip() or 0)
            except ValueError:
                pass
        finally:
            os.close(fd)
    return n


class HostBudget:
    """Connection slots for one server shared by ALL lmap processes (flock'd slot files in the state dir),
    so two jobs on one server can't each take the full limit. take(want) blocks until >=1 slot is free and
    returns how many it got (<= want); give() releases them."""
    def __init__(self, host, limit):
        self.d = STATE / "hosts" / host; self.limit = limit; self.d.mkdir(parents=True, exist_ok=True)

    def take(self, want, stop=None):
        while True:
            got = []
            lim = max(1, self.limit - tms_load(self.d))       # lmap's own tile downloader on the same server
            for i in range(lim):
                if len(got) >= want:
                    break
                fd = os.open(self.d / f"slot{i}", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); got.append(fd)
                except (BlockingIOError, PermissionError):
                    os.close(fd)
            if got:
                return got
            if stop is not None and stop.wait(2 + random.random()):
                return []

    @staticmethod
    def give(fds):
        for fd in fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
            except OSError:
                pass


CPU = threading.BoundedSemaphore(4)   # heavy GDAL raster work at once (all jobs); resized in Ctx
HEAVY = {"gdal_translate", "gdalwarp"}

LIMIT_KEYS = ("cpu", "ram", "gpu", "disk")
LIMITS = {}                 # this process's caps once a Ctx is made (limits(); empty = defaults)
LIMIT_DEFAULT = 80          # percent of each the whole of lmap may use (all lmap processes together)


CONF_SECTIONS = {"paths": ("root", "catalog", "tmp", "tiles"),
                 "net": ("jobs", "bandwidth", "ua", "proxy"),
                 "build": ("shrink", "builds", "webp", "gpu", "queue", "quality", "contour_mm", "index_ratio", "photo_dpi")}
CONF_TABLES = ("limits", "hosts", "servers", "style")
CONF_WHERE = {k: s for s, ks in CONF_SECTIONS.items() for k in ks}


def read_config(raw=None):
    """The config file as one flat dict: [paths]/[net]/[build] keys lifted to the top level (where every reader
    looks), [limits]/[hosts]/[servers] kept as tables. A setting outside its section, or in the wrong one, is a
    usage error. Unknown keys are left in place: `lmap config show` reports them."""
    if raw is None:
        try:
            raw = tomllib.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise CliError(E_USAGE, f"config file {CONFIG}: {e}", 'fix it (TOML: [paths] root = "...", [limits] '
                           "cpu = 80); `lmap config show` lists the settings")
    out = {}
    for k, v in raw.items():
        if k in CONF_SECTIONS:
            if not isinstance(v, dict):
                raise CliError(E_USAGE, f"config [{k}] must be a table", f"e.g. [{k}] {CONF_SECTIONS[k][0]} = ...")
            for kk, vv in v.items():
                if kk not in CONF_WHERE:                 # typo: kept as "net.jbos", `config show` reports it
                    out[f"{k}.{kk}"] = vv; continue
                if CONF_WHERE[kk] != k:
                    raise CliError(E_USAGE, f"config [{k}] {kk}: not a [{k}] setting", f"it belongs in [{CONF_WHERE[kk]}]")
                out[kk] = vv
        elif k in CONF_WHERE:
            raise CliError(E_USAGE, f"config {k} = must be inside its section", f"write it as [{CONF_WHERE[k]}] {k} = ...")
        else:
            out[k] = v
    return out


CONTOUR_MM, INDEX_RATIO = 0.12, 2.0   # printed contour line (mm) and index line = ratio x it (topo practice ~2x)
STYLE_TOML = HERE / "share" / "style.toml"


def map_style(cfg=None):
    """The map style table (share/style.toml) with config [style] overrides merged in (nested tables merge key by
    key). Checked: sizes are numbers in range, every line row has a colour, a dash list and a legend name."""
    def merge(a_, b_):
        o = dict(a_)
        for k, v in b_.items():
            o[k] = merge(o[k], v) if isinstance(v, dict) and isinstance(o.get(k), dict) else v
        return o
    st = tomllib.loads(STYLE_TOML.read_text())
    if cfg is None:
        try:
            cfg = read_config()
        except CliError:
            cfg = {}
    user = cfg.get("style") or {}
    if not isinstance(user, dict):
        raise CliError(E_USAGE, "config [style] must be a table", "e.g. [style] symbol_mm = 2.0")
    st = merge(st, user)
    for k, lo, hi in (("symbol_mm", 0.2, 10.0), ("peak_scale", 0.5, 3.0), ("line_mm", 0.03, 2.0), ("text_pt", 0.5, 20.0), ("place_pt", 0.5, 20.0), ("halo_mm", 0.0, 3.0)):
        v = st.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise CliError(E_USAGE, f"style {k} must be a number {lo}-{hi}", f"[style] {k} = ... in {CONFIG}")
    if not isinstance(st.get("line"), dict):
        raise CliError(E_USAGE, "style line must be a table of [style.line.<name>] rows", f"fix [style] in {CONFIG}")
    _need = {"watercourse", "watercourse_dry", "track", "track_4wd", "power", "pipeline", "fence"} - set(st["line"])
    if _need:
        raise CliError(E_USAGE, f"style line rows missing: {', '.join(sorted(_need))}", f"restore them in {CONFIG} or share/style.toml")
    for n, r in st["line"].items():
        bad = (not isinstance(r, dict) or not isinstance(r.get("colour"), str) or not re.fullmatch(r"\d{1,3}(,\d{1,3}){2,3}", r["colour"].replace(" ", "")) or not isinstance(r.get("dash"), list)
               or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in r["dash"])
               or not isinstance(r.get("legend"), str) or not r["legend"].strip()
               or ("width" in r and (isinstance(r["width"], bool) or not isinstance(r["width"], (int, float))
                                     or not 0.03 <= r["width"] <= 5)))
        if bad:
            raise CliError(E_USAGE, f"style line.{n}: needs colour = \"R,G,B\", dash = [mm, ...], legend = \"name\"",
                           f"fix [style.line.{n}] in {CONFIG}")
    return st


def contour_widths(a=None):
    """(normal, index) contour widths in mm: --contour-mm / --index-ratio > LMAP_CONTOUR_MM / LMAP_INDEX_RATIO >
    config [build] contour_mm / index_ratio > CONTOUR_MM / INDEX_RATIO. Bad values are a usage error."""
    try:
        cfg = read_config()
    except CliError:
        cfg = {}
    out = []
    for k, env, dflt, lo, hi in (("contour_mm", "LMAP_CONTOUR_MM", CONTOUR_MM, 0.03, 1.0),
                                 ("index_ratio", "LMAP_INDEX_RATIO", INDEX_RATIO, 1.0, 5.0)):
        v = getattr(a, k, None) if a is not None else None
        v = v if v is not None else os.environ.get(env) or cfg.get(k) or dflt
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = None
        if v is None or not lo <= v <= hi:
            raise CliError(E_USAGE, f"{k} must be a number {lo}-{hi}", f"e.g. [build] {k} = {dflt} or --{k.replace('_', '-')} {dflt}")
        out.append(v)
    return round(out[0], 3), round(out[0] * out[1], 3)


def limits(cfg=None):
    """{cpu, ram, gpu, disk}: percent caps from LMAP_LIMIT_<KEY> > config [limits] > 80. Whole numbers 10-100."""
    if cfg is None:
        try:
            cfg = read_config()
        except CliError:      # Ctx reads the same file first and reports the error; nothing runs on these defaults
            cfg = {}
    tab = cfg.get("limits") or {}
    if not isinstance(tab, dict):
        raise CliError(E_USAGE, "config [limits] must be a table, e.g. [limits] cpu = 80")
    out = {}
    for k in LIMIT_KEYS:
        v = os.environ.get(f"LMAP_LIMIT_{k.upper()}") or tab.get(k) or LIMIT_DEFAULT
        try:
            v = int(str(v).rstrip("%"))
        except ValueError:
            raise CliError(E_USAGE, f"limit {k} must be a whole percent, got {v!r}")
        if not 10 <= v <= 100:
            raise CliError(E_USAGE, f"limit {k} must be 10-100 (percent), got {v}")
        out[k] = v
    bad = set(tab) - set(LIMIT_KEYS)
    if bad:
        raise CliError(E_USAGE, f"config [limits]: unknown {sorted(bad)}", f"keys: {', '.join(LIMIT_KEYS)}")
    return out


def cpu_cap(lim=None):
    """Cores lmap may keep busy (all processes): floor(cpu% x cores), at least 1."""
    return max(1, int((os.cpu_count() or 2) * (lim or LIMITS or limits())["cpu"] / 100))


FORCE = threading.Event()      # a second abort signal: exit now, don't wait for the grace period
CHILDREN, LIVE, _reg = set(), set(), threading.Lock()   # running child processes / open download sockets
try:
    import ctypes; _prctl = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None


def _pdeath():
    """preexec_fn: the child gets SIGKILL when lmap dies (kill -9, OOM, crash), so no orphan gdalwarp keeps
    writing into a .part the next run deletes. Only a C call: safe in a forked child of a threaded process."""
    if _prctl:
        _prctl(1, 9, 0, 0, 0)          # PR_SET_PDEATHSIG, SIGKILL


def child(pr):
    with _reg:
        CHILDREN.add(pr)
    return pr


def unchild(pr):
    with _reg:
        CHILDREN.discard(pr)


def kill_children():
    with _reg:
        ps = list(CHILDREN)
    for pr in ps:
        try:
            os.killpg(pr.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                pr.kill()
            except OSError:
                pass


def _sock(o):
    """The socket under an http.client connection or response (to shut it down from another thread)."""
    for path in (("sock",), ("fp", "raw", "_sock")):
        x = o
        for a in path:
            x = getattr(x, a, None)
        if x is not None:
            return x
    return None


MAX_BODY = int(os.environ.get("LMAP_MAX_BODY", 256 << 20))   # in-memory responses (JSON, lists, tiles); files stream


def read_capped(r, cap=None):
    """r.read() with a ceiling: a broken or hostile server streaming forever can't fill RAM. Over it = an error,
    never a silently truncated answer."""
    cap = MAX_BODY if cap is None else cap
    b = r.read(cap + 1)
    if len(b) > cap:
        raise ValueError(f"response over {cap >> 20} MB: refusing it" if cap >= 1 << 20 else
                         f"response over {cap} bytes: refusing it")
    return b


class live:
    """with live(resp_or_conn): registers its socket; on abort it's shut down, so a read blocked on a dead or
    trickling server fails at once instead of holding the exit for minutes."""
    def __init__(self, o):
        self.s = _sock(o)

    def __enter__(self):
        if self.s is not None:
            with _reg:
                LIVE.add(self.s)
            if ABORT.is_set():
                self.cut()
        return self

    def __exit__(self, *e):
        if self.s is not None:
            with _reg:
                LIVE.discard(self.s)

    def cut(self):
        import socket
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


ABORT_GRACE = int(os.environ.get("LMAP_ABORT_GRACE", "30"))
try:                                                  # retry-wait scale; tests set it tiny, users never need to
    BACKOFF = max(0.0, float(os.environ.get("LMAP_BACKOFF", "1")))
except ValueError:
    BACKOFF = 1.0


NET_WAIT_S = int(os.environ.get("LMAP_NET_WAIT", "7200"))   # how long a lost connection is waited out (s)
_net = {"lock": threading.Lock(), "down": {}}               # host -> [first unreachable, outages waited out, up now]
_NETLOG = [None]                                            # where net_wait reports (the command's log)
NET_FREE_WAITS = 3          # a server reachable again but failing again: after this many free waits, tries count


def _reach(host, port):
    import socket
    try:
        socket.create_connection((host, port), 6).close(); return True
    except OSError:
        return False


def _proxied(u):
    """A proxy carries this request (config proxy, or http(s)_proxy in the environment and the host not
    bypassed): the server itself isn't reachable directly, so a probe would say 'down' wrongly."""
    if PROXY_SET[0]:
        return True
    try:
        return bool(urllib.request.getproxies().get(u.scheme)) and not urllib.request.proxy_bypass(u.hostname or "")
    except Exception:
        return True


def net_ok_again(url):
    """A request to url got an answer: the outage (if any) is over for its host."""
    h = urllib.parse.urlsplit(url).hostname or ""
    if h in _net["down"]:
        with _net["lock"]:
            _net["down"].pop(h, None)


def net_wait(url, log=None, stop=None):
    """After a connection-level failure (no HTTP answer): if the server can't be reached at all (network down,
    DNS gone, server off), wait here - every thread for that server shares one probe - until it answers again,
    up to LMAP_NET_WAIT (2 h) counted from when it went down. The wait is not charged as a failed try, so a long
    outage doesn't burn every unit's retries at once. A server that keeps coming back and failing gets only
    NET_FREE_WAITS free waits until a request gets an answer (net_ok_again). Returns True if it waited (caller
    retries without counting), False if reachable / proxied / out of free waits, or `stop` (an Event) was set."""
    u = urllib.parse.urlsplit(url); host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    if not host or _proxied(u) or _reach(host, port):
        return False
    with _net["lock"]:
        st = _net["down"].get(host)
        first = st is None
        if first:
            st = _net["down"][host] = [time.time(), 0, False]
        elif st[2]:                                    # it came back, but no answer since: a new outage
            st[2] = False
        if st[1] >= NET_FREE_WAITS:
            return False
        t0 = st[0]
    if first and log:
        log(f"{host}: can't reach it (network or server down): waiting up to {NET_WAIT_S // 60} min, tries not counted")
    while time.time() - t0 < NET_WAIT_S:
        if ABORT.wait(15):
            raise Aborted()
        if stop is not None and stop.is_set():
            return False
        if _reach(host, port):
            with _net["lock"]:
                if _net["down"].get(host) is st and not st[2]:    # once per outage, not per waiting thread
                    st[1] += 1; st[2] = True
                    if log and st[1] == 1:
                        log(f"{host}: reachable again after {time.time() - t0:.0f} s")
            return True
    with _net["lock"]:
        if _net["down"].get(host) is st:
            _net["down"].pop(host, None)               # the next job waits afresh
    raise CliError(E_UPSTREAM, f"{host}: unreachable for {time.time() - t0:.0f} s (network or server down)",
                   "re-run when the connection is back: downloads resume where they stopped")


def _watchdog():
    """After an abort: cut every open download socket at once, then give the clean-up ABORT_GRACE seconds.
    Still alive after that (or a second abort signal): kill our children and exit hard (code 130)."""
    import socket
    ABORT.wait()
    while not FORCE.is_set():
        with _reg:
            ss = list(LIVE)
        for s in ss:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if FORCE.wait(1) or time.time() - _watchdog.t0 > ABORT_GRACE:
            break
    kill_children()
    try:
        os.write(2, f'{{"error": "abort: exited hard after {ABORT_GRACE}s grace or a second signal", '
                    f'"hint": "re-run to resume; partial downloads are kept"}}\n'.encode())
    except OSError:
        pass
    os._exit(130)


_signals = [False]


def abort_signal(sig=None, frm=None):
    """stop --now (and a SIGTERM where a stop can't finish in-flight work): abort with the full clean-up grace.
    The grace counts from THIS signal (a handler that set ABORT without t0 let the watchdog kill at once)."""
    if ABORT.is_set():
        FORCE.set()
    _watchdog.t0 = time.time(); ABORT.set(); DRAIN.set()


_TERM_ESC = [False]


def install_signals():
    """Handlers only set flags (printing from a handler can hit a reentrant-I/O error).
    SIGTERM = drain (pause). Repeating it never escalates (except where a command maps SIGTERM to abort_signal, e.g. pyramid fix/create: there a second one exits now). SIGUSR1 / SIGINT = abort; a second one exits now.
    A watchdog thread makes sure an abort ends the process within LMAP_ABORT_GRACE seconds (default 30)."""
    def term(sig, frm):
        if DRAIN.is_set() and _TERM_ESC[0]:
            abort_signal()                       # a registered build (run_begin): 2nd SIGTERM aborts, so
        DRAIN.set()                              # `timeout`/systemd stop can't wait a whole step out
    signal.signal(signal.SIGTERM, term)
    if hasattr(signal, "SIGUSR2") and signal.getsignal(signal.SIGUSR2) in (signal.SIG_DFL, None):
        signal.signal(signal.SIGUSR2, term)      # a stray pause request drains, never kills (default action)
    if "RUN" in globals():
        RUN.update(drain=True)                   # registry: `job stop <pid>` drains this run (else it is killed)
    signal.signal(signal.SIGUSR1, abort_signal)
    signal.signal(signal.SIGINT, abort_signal)
    if not _signals[0]:
        _signals[0] = True; _watchdog.t0 = time.time()
        threading.Thread(target=_watchdog, daemon=True, name="abort-watchdog").start()


_said = set()
def say_stop(ctx):
    """Called from work loops: log a stop request once, from normal (non-handler) context."""
    if ABORT.is_set() and "abort" not in _said:
        _said.add("abort"); ctx.log("abort received: dropping in-flight work, cleaning up")
    elif DRAIN.is_set() and "drain" not in _said:
        _said.add("drain"); ctx.log("stop received: finishing in-flight tiles, then exiting")

E_OK, E_FAIL, E_USAGE, E_AUTH, E_NOTFOUND, E_REFUSED, E_UPSTREAM = 0, 1, 2, 3, 4, 5, 6
E_DRAINED, E_ABORTED = 75, 130          # stopped by `stop` (resume later) / by `stop --now`


class CliError(Exception):
    def __init__(self, code, msg, hint=None):
        super().__init__(msg); self.code, self.msg, self.hint = code, msg, hint
