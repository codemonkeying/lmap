# Run registry (97a): every command that does work and runs longer than RUN_AFTER seconds is listed under
# ~/.local/state/lmap/runs/: <pid>.json (what, since when, progress, ETA, final), <pid>.lock (flock held while it
# lives: a crash or kill -9 reads as "died") and <pid>.log (its log lines). `lmap status`, `lmap job list|status|log|
# stop <pid>` read it, so area/map/refresh builds are visible and stoppable like downloads.
# Fed by Dag.run (steps done/total/running) and Progress.write (downloads). Writes throttled to one per RUN_EVERY s.

RUN_AFTER = float(os.environ.get("LMAP_RUN_AFTER", "10"))     # short commands are never registered
RUN_EVERY, RUN_KEEP_S, RUN_RECENT_S = 10.0, 7 * 86400, 86400   # write throttle; prune age; `status` shows done runs this recent
RUN_SKIP_VERBS = {"list", "show", "status", "info", "log", "help", "doctor", "check", "stop"}


def runs_dir():
    return STATE / "runs"


class RunReg:
    """This process's registry record. start() arms a timer; the record + lock appear only after RUN_AFTER s."""
    def __init__(self):
        self.d = None; self.on = False; self.fd = None; self.logf = None; self.t_w = 0.0
        self.lk = threading.RLock(); self.timer = None

    def start(self, argv, cmd):
        self.d = {"pid": os.getpid(), "argv": list(argv), "cmd": cmd, "started": time.time(), "updated": time.time(),
                  "final": None, "exit": None, "done": None, "total": None, "running": [], "eta_s": None,
                  "drain": False, "parts": {}, "log": None}
        if RUN_AFTER <= 0:
            self._open()
        else:
            self.timer = threading.Timer(RUN_AFTER, self._open); self.timer.daemon = True; self.timer.start()

    def _open(self):
        with self.lk:
            if self.on or self.d is None or self.d["final"] is not None:
                return
            try:
                R = runs_dir(); R.mkdir(parents=True, exist_ok=True); pid = self.d["pid"]
                fd = os.open(R / f"{pid}.lock", os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); os.ftruncate(fd, 0); os.write(fd, str(pid).encode())
                self.fd = fd; self.d["log"] = str(R / f"{pid}.log")
                self.logf = open(R / f"{pid}.log", "w", encoding="utf-8")
                self.on = True; self._write()
                run_prune()
            except OSError:                               # the registry is advisory: never fail the command
                pass

    def _write(self):
        self.d["updated"] = self.t_w = time.time()
        try:
            atomic_write(runs_dir() / f"{self.d['pid']}.json", json.dumps(self.d, default=str))
        except OSError:
            pass

    def update(self, force=False, **kw):
        if self.d is None:
            return
        with self.lk:
            self.d.update(kw)
            if self.on and (force or time.time() - self.t_w >= RUN_EVERY):
                self._write()

    def part(self, key, **kw):
        """Progress of one piece (a source in a fetch/queue): totals are summed when no step DAG reports."""
        if self.d is None:
            return
        with self.lk:
            self.d["parts"][key] = kw
            if not self.d.get("dag"):
                ps = self.d["parts"].values()
                self.d["done"] = sum(p.get("done") or 0 for p in ps); self.d["total"] = sum(p.get("total") or 0 for p in ps)
                e = [p.get("eta_s") for p in ps]
                self.d["eta_s"] = max(e) if e and None not in e else None
            if self.on and time.time() - self.t_w >= RUN_EVERY:
                self._write()

    def logline(self, line):
        if self.on and self.logf:
            try:
                self.logf.write(line + "\n"); self.logf.flush()
            except (OSError, ValueError):
                pass

    def finish(self, code):
        if self.d is None:
            return
        if self.timer:
            self.timer.cancel()
        with self.lk:
            self.d["exit"] = code; self.d["running"] = []; self.d.pop("eta_s", None)
            self.d["final"] = ("ok" if code == E_OK else "stopped" if code in (E_DRAINED, E_ABORTED) or DRAIN.is_set()
                               else "failed")
            if self.on:
                self._write()
                try:
                    self.logf.close(); os.close(self.fd)          # releases the flock
                except OSError:
                    pass
            self.on = False


RUN = RunReg()


try:
    DRAIN_GRACE = float(os.environ.get("LMAP_DRAIN_GRACE", "120"))
except ValueError:                                   # a bad value must not break every command
    DRAIN_GRACE = 120.0


PAUSE = threading.Event()     # `lmap job stop` on a step build (SIGUSR2): drain with no deadline


def _drain_deadline():
    """Two stop modes on a step build. PAUSE (`lmap job stop`, SIGUSR2): start nothing new, let the running step
    finish however long it takes. A plain SIGTERM (`timeout`, systemd, kill): the same, but a step still running
    DRAIN_GRACE s later is aborted (one aerial clip can take hours). Finished steps are kept; a re-run resumes.
    Any stop after a pause aborts at once (a 2nd stop always does)."""
    DRAIN.wait()
    RUN.update(force=True, stopping="pause" if PAUSE.is_set() else "stop")
    if PAUSE.is_set():                               # pause: no deadline; a 2nd stop aborts (signal handler)
        return
    if not ABORT.wait(DRAIN_GRACE):
        try:
            os.write(2, f'{{"stop": "step still running {DRAIN_GRACE:.0f} s after the stop: aborting it '
                        f'(finished steps are kept; re-run to resume)"}}\n'.encode())
        except OSError:
            pass
        abort_signal()


def run_begin(a, argv):
    """Called by main for a real command line: register unless read-only / dry-run / help."""
    if getattr(a, "dry_run", False) or getattr(a, "verb", None) in RUN_SKIP_VERBS or a.fn in (cmd_status,):
        return
    words = [str(w) for w in (getattr(a, "noun", None), getattr(a, "verb", None),   # parsed, not argv: option
                              getattr(a, "id", None) or getattr(a, "name", None)) if w]   # values must not shift the key
    RUN.start(argv, " ".join(words[:3]))
    if a.fn in (cmd_refresh, cmd_area_create, cmd_map):  # step builds: `job stop <pid>` drains (start no new step),
        _TERM_ESC[0] = True                              # a 2nd stop / --now aborts; Ctrl-C stays as it was
        def term(sig, frm):                              # (install_signals replaces this with the same rule:
            if DRAIN.is_set():                           #  a 2nd stop, also after a pause, aborts at once)
                abort_signal()
            DRAIN.set()
        def pause(sig, frm):
            if DRAIN.is_set():                           # a 2nd `job stop` (or one after a SIGTERM): abort
                abort_signal(); return
            PAUSE.set(); DRAIN.set()
        signal.signal(signal.SIGTERM, term)
        if hasattr(signal, "SIGUSR2"):
            signal.signal(signal.SIGUSR2, pause)
        signal.signal(signal.SIGUSR1, abort_signal)
        if not _signals[0]:
            _signals[0] = True; _watchdog.t0 = time.time()
            threading.Thread(target=_watchdog, daemon=True, name="abort-watchdog").start()
        RUN.update(drain=True, pause=True)               # `job stop` sends SIGUSR2 only to runs that say pause
        threading.Thread(target=_drain_deadline, daemon=True, name="drain-deadline").start()
    else:                                                # others: SIGTERM ends at once, but recorded as stopped
        def end(sig, frm):                               # (a command's own handlers, e.g. fetch, replace this)
            RUN.finish(E_ABORTED); os._exit(128 + sig)
        signal.signal(signal.SIGTERM, end)


def run_read(pid):
    """The record of run <pid> with its live state: running / died / ok / failed / stopped. None if unknown."""
    p = runs_dir() / f"{pid}.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if d.get("final"):
        d["state"] = d["final"]
    else:
        alive = lock_held(runs_dir() / f"{pid}.lock")
        try:
            alive = alive and b"lmap" in Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            alive = False
        d["state"] = "running" if alive else "died"
    d["age_s"] = int(time.time() - (d.get("updated") or 0))
    return d


def run_list(recent_s=RUN_RECENT_S):
    """Registered runs, newest first: every running/died one, finished ones from the last recent_s."""
    out = []
    for p in runs_dir().glob("*.json") if runs_dir().is_dir() else []:
        if not p.stem.isdigit():
            continue
        d = run_read(int(p.stem))
        if d and (d["state"] in ("running", "died") or time.time() - (d.get("updated") or 0) < recent_s):
            out.append(d)
    return sorted(out, key=lambda d: -(d.get("started") or 0))


def run_prune():
    """Drop finished/died records older than RUN_KEEP_S (never a live one)."""
    for p in runs_dir().glob("*.json"):
        try:
            if p.stem.isdigit() and time.time() - p.stat().st_mtime > RUN_KEEP_S and not lock_held(p.with_suffix(".lock")):
                for q in (p, p.with_suffix(".lock"), p.with_suffix(".log")):
                    q.unlink(missing_ok=True)
        except OSError:
            pass


BUILD_RATE_S_GB = {"ovr": 390.0, "cog": 800.0, "copy": 250.0, "single": 600.0}   # s per GB of source; measured
#   2026-10-23 (QLD S2 4.5 GB: overviews 29 min, COG 60 min, verify+copy ~20 min); replaced by history as builds run


def build_rate(stage):
    return step_hist().get(f"rate|{stage}") or BUILD_RATE_S_GB[stage]


def build_est_s(s=None, gb=None, rgb=None):
    """Seconds a COG build of source s (or gb of source data) will take: from past builds' s/GB, else defaults."""
    gb = gb if gb is not None else float((s or {}).get("est_gb") or 0)
    rgb = rgb if rgb is not None else str((s or {}).get("compress", "")).upper() in ("JPEG", "WEBP") or "sat" in str((s or {}).get("id", ""))
    stages = ("ovr", "cog", "copy") if rgb else ("single", "copy")
    return int(gb * sum(build_rate(x) for x in stages))


class BuildEta:
    """build_cog's stages -> the run registry: stage name + seconds left (updated per stage and during the copy);
    on each stage's end its s/GB goes into the history so the next estimate is measured, not guessed."""
    def __init__(self, gb, rgb):
        self.gb = max(gb, 0.001); self.stages = ["ovr", "cog", "copy"] if rgb else ["single", "copy"]; self.cur = None

    def left(self, frac=0.0):
        i = self.stages.index(self.cur) if self.cur else 0
        rest = self.stages[i + 1:] if self.cur else self.stages
        here = build_rate(self.cur) * self.gb * (1 - frac) if self.cur else 0
        return int(here + sum(build_rate(x) * self.gb for x in rest))

    def stage(self, name, label):
        self.end()
        if name not in self.stages:                    # e.g. a tiny photo: no staged overviews, one step
            self.stages = [name, "copy"]
        self.cur, self.t0 = name, time.time(); RUN.update(force=True, stage=label, eta_s=self.left(), eta_guess=False)

    def progress(self, frac, label):
        RUN.update(stage=label, eta_s=self.left(frac))

    def end(self):
        if self.cur and self.gb >= 0.5:               # small builds are all overhead: they'd skew the rate
            old = step_hist().get(f"rate|{self.cur}")
            new = (time.time() - self.t0) / self.gb
            step_hist_add(f"rate|{self.cur}", new if old is None else 0.5 * old + 0.5 * new)
        self.cur = None

    def done(self):
        self.end(); RUN.update(force=True, stage=None)


STEP_GUESS_S = (("area-", 900), ("-project", 1800), ("", 3600))   # first-run guesses (refresh steps), marked "~"


def run_brief(d):
    """One line for status/list. ETA counts down from the last update; past it while running = 'overdue', never 0m."""
    prog = f"{d['done']}/{d['total']} {100 * (d['done'] or 0) // d['total']}%" if d.get("total") else "-"
    eta = ""
    if d["state"] == "running":
        e = d.get("eta_s")
        if e is None:
            eta = "no ETA yet"
        else:
            left = e - (time.time() - (d.get("updated") or time.time()))
            eta = (f"over usual time: {', '.join(d['overdue'][:2])}" if d.get("overdue")
                   else ("eta ~" if d.get("eta_guess") else "eta ") + fmt_dur(left) if left >= 60
                   else "eta <1m" if left > 0
                   else "finishing" if d.get("stage") or (d.get("total") and d.get("done") == d.get("total"))
                   else "eta overdue")
        if d.get("stopping"):
            eta = ("pausing: waiting for the running step, " if d["stopping"] == "pause"
                   else "stopping: running step aborted 2 min after the stop, ") + eta
        if d.get("stage"):
            eta += f" ({d['stage']})"
    run_ = f" running: {', '.join(d['running'][:4])}" if d.get("running") else ""
    return f"{d['pid']:>7} {d['state']:<8} {d.get('cmd', '')[:40]:<40} {prog:>14} {eta}{run_}"


def step_hist_path():
    return runs_dir() / "steps.json"


def step_hist():
    try:
        return json.loads(step_hist_path().read_text())
    except (OSError, ValueError):
        return {}


def step_hist_add(key, secs):
    """Remember how long a step took (per cmd + step id), for the next run's ETA."""
    try:                                   # read-modify-write under a lock: parallel steps must not drop entries
        runs_dir().mkdir(parents=True, exist_ok=True)
        with open(runs_dir() / ".steps.lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            h = step_hist(); h[key] = float(f"{secs:.4g}")   # 4 digits: s/tile rates are ~0.05
            atomic_write(step_hist_path(), json.dumps(h))
    except OSError:
        pass


def child_eta(sid):
    """A running step's own run record (refresh children register too): its live ETA in seconds, or None."""
    for d in run_list():
        try:                                           # only this process's own children (refresh steps)
            if int(Path(f"/proc/{d['pid']}/stat").read_text().rsplit(")", 1)[1].split()[1]) != os.getpid():
                continue
        except (OSError, ValueError, IndexError, KeyError):
            continue
        if d.get("state") == "running" and d.get("eta_s") is not None and sid in (d.get("cmd") or "").split() + [
                "area-" + w for w in (d.get("cmd") or "").split()]:
            return max(d["eta_s"] - (time.time() - (d.get("updated") or time.time())), 60)
    return None


def dag_eta(steps, width, cmd, guess=None, over=None):
    """Seconds left: a running step's own live ETA, else past step times (same cmd + step id), else a first-run
    guess (STEP_GUESS_S; `guess` list gets True so the line says ~). 0 only when nothing is left."""
    h = step_hist(); now = time.time(); left = []
    for s in steps.values():
        if s.state in ("wait", "running"):
            t = child_eta(s.id) if s.state == "running" else None
            if t is not None:
                left.append(t); continue
            t = h.get(f"{cmd}|{s.id}")
            if t is None:
                t = next(v for k, v in STEP_GUESS_S if k in s.id)
                if guess is not None:
                    guess.append(True)
            if s.state == "running" and now - s.t0 > t and over is not None and f"{cmd}|{s.id}" in h:
                over.append(f"{s.id} {fmt_dur(now - s.t0)} (usually {fmt_dur(t)})")   # no ETA for it: say so
            left.append(max(t - (now - s.t0), 60.0) if s.state == "running" else t)
    if not left:
        return 0
    return int(max(max(left), sum(left) / max(width, 1)))


def run_stop(pid, now=False, wait=0):
    """Signal run <pid> (SIGTERM = drain, SIGUSR1 = abort); wait up to `wait` s. -> (exited, waited_s)."""
    try:
        d = run_read(pid) or {}                       # step builds: a pause (no deadline); others: SIGTERM
        os.kill(pid, signal.SIGUSR1 if now else signal.SIGUSR2 if d.get("pause") and hasattr(signal, "SIGUSR2")
                else signal.SIGTERM)
    except ProcessLookupError:
        return True, 0
    waited = 0
    while wait and waited < wait and (run_read(pid) or {}).get("state") == "running":
        time.sleep(1); waited += 1
    return (run_read(pid) or {}).get("state") != "running", waited


MEASURE_MIN_S = 30.0          # this run's own rate is trusted after this long in a phase (before: past runs' rate)


def fit_line(xy):
    """Least squares secs = a + b * units over [(units, secs)]; None unless 2+ different unit counts."""
    if len({x for x, _ in xy}) < 2:
        return None
    n = len(xy); mx = sum(x for x, _ in xy) / n; my = sum(y for _, y in xy) / n
    b = sum((x - mx) * (y - my) for x, y in xy) / sum((x - mx) ** 2 for x, _ in xy)
    b = max(b, 0.0); a = max(my - b * mx, 0.0)
    return a, b


class NullRun:
    """A RUN stand-in that keeps nothing (a print inside `lmap map`: the map's own steps report)."""
    def __init__(self, started=None):
        self.d = {"started": started} if started else None   # WorkEta times from here

    def update(self, force=False, **kw):
        pass


class WorkEta:
    """Phased work -> the run registry: done/total units, stage text and seconds left.
    phases = [(key, units, label, children)]: a phase is `children` child runs (QGIS processes, one per zoom for a
    phone map), each costing a + b * its units (a = start-up, b = s per unit: a phone map's low zooms are nearly all
    start-up). a, b come from this run once it has run MEASURE_MIN_S in the phase and has 2+ children of different
    sizes, else from past runs (history start|<key>, rate|<key>). A rate nobody has measured -> None ("no ETA yet"),
    never a guess. Each phase's fitted a, b go into the history at its end, for the next run's pre-start ETA."""
    def __init__(self, phases, run=None):
        self.ph = [(p[0], float(p[1]), p[2], int(p[3]) if len(p) > 3 else 1) for p in phases]
        self.run = run if run is not None else RUN
        self.i = -1; self.done_prev = 0.0; self.note = ""
        self.samp = []; self.tp = self.tc = None; self.nc = 0.0; self.f = 0.0; self.cdone = 0; self.udone = 0.0
        self.lk = threading.RLock(); self.h = step_hist()   # progress comes from a reader thread; history read once
        d = getattr(self.run, "d", None) or {}
        d = d if isinstance(d, dict) else {}           # the command's start: set-up before phase 0 is real cost
        self.t_start = d.get("started") or time.time(); self.pend = []

    def hist(self, key):
        b = self.h.get(f"rate|{key}")
        return None if b is None else (self.h.get(f"start|{key}") or 0.0, b)

    def total(self):
        return sum(u for _, u, _, _ in self.ph)

    def _ab(self, now):
        k = self.ph[self.i][0]
        if self.tp is not None and now - self.tp >= MEASURE_MIN_S:
            fit = fit_line(self.samp); h = self.hist(k)
            xs = {x for x, _ in self.samp}           # few small children are noise: past runs win until then
            if fit and (h is None or (len(xs) >= 3 and max(xs) >= 10)):
                return fit
            if self.samp and h:                       # 1 child timed: keep the past start-up, measure the rate
                a = h[0]; n = sum(x for x, _ in self.samp); s = sum(y for _, y in self.samp) - a * len(self.samp)
                if n > 0 and s > 0:
                    return a, s / n
            if self.tc is not None and self.f > 0 and self.nc > 0 and self.ph[self.i][3] == 1:   # one child, still running
                return 0.0, (now - self.tc) / (self.nc * self.f)
        return self.hist(k)

    def left(self, now=None):
        with self.lk:
            return self._left(now)

    def _left(self, now=None):
        if self.i >= len(self.ph):
            return 0
        now = time.time() if now is None else now; out = 0.0
        for j, (k, u, _, ch) in enumerate(self.ph):
            if j < self.i:
                continue
            ab = self._ab(now) if j == self.i else self.hist(k)
            if ab is None:
                return None
            a, b = ab
            if j == self.i:
                if self.tc is not None:                 # the running child, then the rest of this phase
                    out += max(a + b * self.nc - (now - self.tc), b * self.nc * (1 - self.f), 0.0)
                rest_c = ch - self.cdone - (1 if self.tc is not None else 0)
                rest_u = u - self.udone - (self.nc if self.tc is not None else 0)
                out += a * max(rest_c, 0) + b * max(rest_u, 0)
            else:
                out += a * ch + b * u
        return int(out)

    def _push(self, force=False):
        lb = self.ph[self.i][2]; d = self.done_prev + self.udone + self.nc * self.f
        st = f"{lb}{self.note} ({self.i + 1}/{len(self.ph)})" if len(self.ph) > 1 else f"{lb}{self.note}"
        self.run.update(force=force, done=int(d), total=int(self.total()), stage=st, eta_s=self._left(), eta_guess=False)

    def phase(self, i, auto=True, t0=None):
        """Start phase i (the current one is complete: its a, b are recorded). auto: a 1-child phase starts its
        child here (callers that start each child themselves pass auto=False). t0: when it really began
        (the command's start for a set-up phase)."""
        with self.lk:
            self.end(); self.i, self.tp, self.samp, self.note = i, t0 or time.time(), [], ""
            self.cdone, self.udone, self.tc, self.nc, self.f = 0, 0.0, None, 0.0, 0.0
            if auto and self.ph[i][3] == 1:
                self.child(self.ph[i][1], t0=t0)
            else:
                self._push(force=True)

    def child(self, units, t0=None):
        with self.lk:
            self.child_end(); self.tc, self.nc, self.f = t0 or time.time(), float(units), 0.0; self._push(force=True)

    def progress(self, frac, note=""):
        """frac = how far the running child is (a QGIS child's 'tile n/m' line). Ignored between children."""
        with self.lk:
            if self.tc is None:
                return
            self.f = min(max(frac, 0.0), 1.0); self.note = note; self._push()

    def child_end(self):
      with self.lk:
        if self.tc is None:
            return
        self.samp.append((self.nc, time.time() - self.tc)); self.cdone += 1; self.udone += self.nc
        self.tc, self.nc, self.f = None, 0.0, 0.0; self._push()

    def end(self):
      with self.lk:
        if self.i < 0 or self.tp is None:
            return
        self.child_end(); k = self.ph[self.i][0]; old = self.hist(k)
        ab = fit_line(self.samp)
        if ab is None and self.samp:                    # one size only: past start-up kept, the rest is the rate
            a = old[0] if old else 0.0; n = sum(x for x, _ in self.samp); s = sum(y for _, y in self.samp) - a * len(self.samp)
            ab = (a, s / n) if n > 0 and s > 0 else None
        if ab:
            a, b = ab if old is None else (0.5 * old[0] + 0.5 * ab[0], 0.5 * old[1] + 0.5 * ab[1])
            self.pend.append((f"rate|{k}", b))     # written by finish(): a failed/stopped run records nothing
            if self.ph[self.i][3] > 1 or old:
                self.pend.append((f"start|{k}", a))
        self.done_prev += self.udone; self.udone = 0.0; self.tp = None

    def finish(self):
        with self.lk:
            self.end(); self.i = len(self.ph)
            for k, v in self.pend:
                step_hist_add(k, v)
            self.pend = []; self.run.update(force=True, stage=None, eta_s=0)


def tile_line_frac(line):
    """'tile 3/12' (a QGIS child's progress) -> 0.25; None for anything else."""
    m = re.match(r"tile (\d+)/(\d+)", line or "")
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def status_overall(runs, jobs, queued=0, now=None):
    """One line for everything running + queued: count, % (time done / time done + left) and when it all ends.
    Running things run side by side, so the end is the latest one. Anything without an ETA -> 'no ETA yet'."""
    now = time.time() if now is None else now
    run_p = {r.get("pid") for r in runs if r.get("state") == "running"}
    items = []
    for r in runs:
        if r.get("state") == "running":
            e = r.get("eta_s")
            items.append((now - (r.get("started") or now),
                          None if e is None else max(e - (now - (r.get("updated") or now)), 0)))
    for j in jobs:
        if j.get("running") and j.get("pid") not in run_p:   # a queue supervisor is also a registered run
            items.append((j.get("elapsed_s") or 0, j.get("eta_s")))
    if not items and not queued:
        return {"running": 0, "queued": 0, "pct": None, "eta_s": None, "text": "nothing running or queued"}
    known = all(l is not None for _, l in items) and not queued
    el = sum(e for e, _ in items)
    left = max((l for _, l in items if l is not None), default=0)
    pct = int(100 * el / (el + sum(l for _, l in items))) if known and el + sum(l for _, l in items) > 0 else None
    eta = int(left) if known else None
    why = "" if known else (f" ({queued} queued not started)" if queued and all(l is not None for _, l in items)
                            else " (still measuring)")
    txt = (f"{len(items)} running, {queued} queued: " +
           (f"{pct}%  all done {time.strftime('%a %H:%M', time.localtime(now + eta))} (in {fmt_dur(eta) if eta >= 60 else '<1m'})"
            if known else f"no ETA yet{why}"))
    return {"running": len(items), "queued": queued, "pct": pct, "eta_s": eta, "text": txt}
