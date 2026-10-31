#!/usr/bin/env python3
"""Review fixes (0.13.4), against a local HTTP server. Run: python3 tests/t_review.py <empty state dir>
Checks: parallel download + resume, wrong Content-Range rejected, stall detected, 416 on a complete .part,
Tuner halves once per burst, one failed chunk stops the rest quickly, abort exits within the grace even with
a hung read, job_pid ignores a stale pid, raster_info/scale, Progress from many threads."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import http.server, importlib.machinery, importlib.util, os, signal, subprocess, sys, tempfile, threading, time
os.environ.setdefault("LMAP_BACKOFF", "0.02")   # same retry paths, waits of ms not minutes
from pathlib import Path

os.environ["XDG_STATE_HOME"] = sys.argv[1]
HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)

DATA = os.urandom(5 * (1 << 20) + 12345)
MODE = {"m": "ok"}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Length", str(len(DATA)))
        self.send_header("Accept-Ranges", "bytes"); self.send_header("ETag", '"v1"'); self.end_headers()

    def do_GET(self):
        m = MODE["m"]; rg = self.headers.get("Range")
        if m == "fail" and rg and not rg.startswith("bytes=0-"):
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        if m == "hang":
            time.sleep(3600); return
        a, b = 0, len(DATA) - 1
        if rg:
            x, y = rg[6:].split("-"); a = int(x); b = int(y) if y else b
            if a >= len(DATA):
                self.send_response(416); self.send_header("Content-Length", "0"); self.end_headers(); return
        start = 0 if m == "badrange" else a
        body = DATA[start:start + b - a + 1]
        self.send_response(206 if rg else 200)
        if rg:
            self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if m == "stall":                               # trickle: 200 bytes/s keeps the socket timeout from firing
            for k in range(0, len(body), 200):
                self.wfile.write(body[k:k + 200]); self.wfile.flush(); time.sleep(1)
            return
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/f.bin"
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else "")); ok &= bool(cond)


class Ctx:
    def log(self, m):
        pass


d = Path(tempfile.mkdtemp()); CH = 1 << 20

# 1. plain parallel download
r = L.http_get_parallel(Ctx(), URL, d / "a", chunk=CH)
check("parallel download", r is True and (d / "a").read_bytes() == DATA)

# 2. resume: half a chunk + 2 whole chunks on disk already
cd = Path(str(d / "b") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v1"')
(cd / "data").write_bytes(DATA[:CH + 300000] + bytes(len(DATA) - CH - 300000))
(cd / "done").write_text("0\n"); (cd / "00001.at").write_text("300000")
r = L.http_get_parallel(Ctx(), URL, d / "b", chunk=CH)
check("resume byte-wise", r is True and (d / "b").read_bytes() == DATA and not cd.exists())

# 3. wrong Content-Range is refused (never written into a chunk)
SLOW = not os.environ.get("SKIP_SLOW")
if SLOW:
    MODE["m"] = "badrange"; L.STALL_S = 2
    cd = Path(str(d / "c") + ".chunks"); cd.mkdir(); (cd / "size").write_text(f'{len(DATA)} {CH} "v1"')
    (cd / "data").write_bytes(bytes(2 * CH) + DATA[2 * CH:2 * CH + 1000] + bytes(len(DATA) - 2 * CH - 1000))
    (cd / "00002.at").write_text("1000")
    t = time.time()
    try:
        L.http_get_parallel(Ctx(), URL, d / "c", chunk=CH); check("bad Content-Range refused", False)
    except L.CliError as e:
        check("bad Content-Range refused", "wrong range" in e.msg and (cd / "00002.at").read_text() == "1000",
              f"{time.time() - t:.0f}s")
    MODE["m"] = "ok"

    # 4. one chunk failing for good stops the others fast (not 17 min)
    MODE["m"] = "fail"; t = time.time()
    try:
        L.http_get_parallel(Ctx(), URL, d / "e", chunk=CH); check("failed chunk", False)
    except L.CliError:
        pass
    dt = time.time() - t
    check("one failed chunk ends the download", dt < 600, f"{dt:.0f}s (8 tries' backoff, others stopped)")
    MODE["m"] = "ok"

# 5. stall detection
MODE["m"] = "stall"; L.STALL_S = 3; t = time.time()
try:
    with open(d / "s", "wb") as f, L.OPENER.open(L.urllib.request.Request(URL), timeout=60) as rr:
        L.copy_body(rr, f)
    check("stall detected", False)
except L.Retry as e:
    check("trickle detected as a stall", time.time() - t < 15, str(e))
MODE["m"] = "ok"

# 6. http_get: complete .part + 416 -> renamed, not re-downloaded
(d / "g.part").write_bytes(DATA)
r = L.http_get(URL, d / "g", check=lambda p: p.stat().st_size == len(DATA))
check("416 on a complete .part keeps it", r is True and (d / "g").read_bytes() == DATA)

# 7. Tuner: a burst of failures halves once
tn = L.Tuner(16, window=30); tn.c = 16
for _ in range(16):
    tn.unit(False)
check("Tuner halves once per burst", tn.c == 8, f"c={tn.c}")

# 8. Progress from 32 threads at once
pr = L.Progress({"id": "t-prog"}, 10000, 0)
ths = [threading.Thread(target=lambda: [pr.tick(True) for _ in range(200)]) for _ in range(32)]
[x.start() for x in ths]; [x.join() for x in ths]
check("Progress thread-safe", pr.done == 6400 and not list(L.STATE.glob(".*.tmp")))

# 9. job_pid: a stale pid in an unheld lock file is not a running job
lp = L.STATE / "locks" / "x.lock"; lp.parent.mkdir(parents=True, exist_ok=True); lp.write_text(str(os.getpid()))
check("stale lock pid ignored", L.lock_held(lp) is False)

# 10. raster_info keeps scale/offset; deep_decode on a good COG
tif = d / "s.tif"
subprocess.run(["gdal_create", "-of", "GTiff", "-outsize", "300", "200", "-ot", "Int16", "-burn", "7",
                "-a_srs", "EPSG:4326", "-a_ullr", "0", "1", "1", "0", str(tif)], check=True)
subprocess.run(["gdal_edit.py", "-scale", "0.5", "-offset", "0", str(tif)], check=True)
cog = d / "s.cog.tif"
subprocess.run(["gdal_translate", "-q", "-of", "COG", "-co", "PREDICTOR=2", str(tif), str(cog)], check=True)
i = L.raster_info(cog)
check("scale kept by gdal_translate", i["scale"] == 0.5 and i["type"] == "Int16", str(i))
check("deep_decode ok", L.deep_decode(Ctx(), cog, n=2) is None)
# a damaged block: reported (not a crash: a second communicate() on the failed child used to raise)
import shutil as _sh
from osgeo import gdal
_bad = Path(str(cog) + ".bad.tif"); _sh.copy(cog, _bad)
_d = gdal.Open(str(_bad)); _b = _d.GetRasterBand(1); _bx, _by = _b.GetBlockSize()
_off = int(_b.GetMetadataItem("BLOCK_OFFSET_0_0", "TIFF")); _n = int(_b.GetMetadataItem("BLOCK_SIZE_0_0", "TIFF")); _d = None
with open(_bad, "r+b") as _f:
    _f.seek(_off + 8); _f.write(b"\xff" * max(1, _n - 16))
try:
    _why = L.deep_decode(Ctx(), _bad, n=2)
except Exception as _x:
    _why = None; print("  raised", type(_x).__name__, _x)
check("deep_decode reports a damaged block", bool(_why))
_bad.unlink()
_code = lmap_source()
check("tile warps: 4 encode threads + the parallel decode check (not single-thread gdalinfo -checksum)",
      '["NUM_THREADS=4"]' in _code and "deep_decode(ctx, part, 4) if part.is_file()" in _code
      and "if not checked and deep_decode(ctx, part, 4):" in _code)

# 11. abort with a hung read: the process exits within the grace (child process, real signal)
code = f'''
import importlib.machinery, importlib.util, os, sys
os.environ["LMAP_ABORT_GRACE"] = "5"
ld = importlib.machinery.SourceFileLoader("lmap", {str(HERE / "bin" / "lmap")!r})
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
L.install_signals()
import urllib.request
r = L.OPENER.open(urllib.request.Request({URL!r}), timeout=3600)
'''
MODE["m"] = "hang"
p = subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True)
time.sleep(2); p.send_signal(signal.SIGUSR1); t = time.time()
try:
    p.wait(20); check("abort exits within the grace despite a hung read", p.returncode == 130, f"{time.time() - t:.1f}s")
except subprocess.TimeoutExpired:
    p.kill(); check("abort exits within the grace despite a hung read", False, "still alive after 20s")
p2 = subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True)
time.sleep(2); p2.send_signal(signal.SIGUSR1); time.sleep(0.3); p2.send_signal(signal.SIGUSR1); t = time.time()
p2.wait(10); check("second abort exits at once", p2.returncode == 130 and time.time() - t < 3, f"{time.time() - t:.1f}s")
MODE["m"] = "ok"

srv.shutdown()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
