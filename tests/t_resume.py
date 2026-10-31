#!/usr/bin/env python3
"""Fault tolerance of http_get_parallel resume, with real process kills. Run: python3 tests/t_resume.py <empty state dir>
A throttled local server logs every byte it sends. A download runs in a child process and is killed mid-chunk
(SIGKILL = power cut / OOM, SIGTERM = lmap job stop). The resume must: fetch only the bytes not on disk
(partial .part chunks continue byte-wise), build a byte-exact file, and leave no chunk dir behind.
Also: a .part longer than its chunk (corrupt) is dropped, and the file changing upstream starts over."""
import http.server, importlib.machinery, importlib.util, os, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path

os.environ["XDG_STATE_HOME"] = sys.argv[1]
HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)

CH = 1 << 20
DATA = os.urandom(6 * CH + 777)
S = {"sent": 0, "etag": '"v1"', "rate": 0, "gen": 0}   # gen: only count requests made after a reset          # rate: bytes/s per connection, 0 = full speed
lk = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200); self.send_header("Content-Length", str(len(DATA)))
        self.send_header("Accept-Ranges", "bytes"); self.send_header("ETag", S["etag"]); self.end_headers()

    def do_GET(self):
        rg = self.headers.get("Range"); ir = self.headers.get("If-Range"); gen = S["gen"]
        a, b = 0, len(DATA) - 1
        if rg and (ir is None or ir == S["etag"]):
            x, y = rg[6:].split("-"); a = int(x); b = int(y) if y else b
            self.send_response(206); self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[a:b + 1]
        self.send_header("Content-Length", str(len(body))); self.send_header("ETag", S["etag"]); self.end_headers()
        step = 16384
        try:
            for k in range(0, len(body), step):
                self.wfile.write(body[k:k + step]); self.wfile.flush()
                with lk:
                    if gen == S["gen"]:                   # a dead client's connection still draining: not counted
                        S["sent"] += len(body[k:k + step])
                if S["rate"]:
                    time.sleep(step / S["rate"])
        except (BrokenPipeError, ConnectionResetError):
            pass


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


CHILD = r'''
import importlib.machinery, importlib.util, sys
ld = importlib.machinery.SourceFileLoader("lmap", sys.argv[1])
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
L.install_signals()
class C:
    def log(self, m): pass
L.http_get_parallel(C(), sys.argv[2], L.Path(sys.argv[3]), chunk=int(sys.argv[4]))
'''


def on_disk(cd):
    """Bytes kept for resume: listed chunks + the synced part of partial ones (one sparse data file, 0.86)."""
    if not cd.exists():
        return 0
    try:
        done = {int(x) for x in (cd / "done").read_text().split()}
    except OSError:
        done = set()
    n = (len(DATA) + CH - 1) // CH
    return sum(min(CH, len(DATA) - i * CH) for i in done if i < n) + sum(int(f.read_text() or 0) for f in cd.glob("*.at"))


def killed_then_resumed(name, sig):
    d = Path(tempfile.mkdtemp()); dest = d / "f"; cd = Path(str(dest) + ".chunks")
    S["rate"] = 400_000                                   # 6 MB at 0.4 MB/s/conn: killed well before the end
    p = subprocess.Popen([sys.executable, "-c", CHILD, str(HERE / "bin" / "lmap"), URL, str(dest), str(CH)],
                         env={**os.environ, "LMAP_ABORT_GRACE": "5"})
    t = time.time()
    while on_disk(cd) < 2.5 * CH and time.time() - t < 60:
        time.sleep(0.2)
    os.kill(p.pid, sig); p.wait(timeout=30)
    kept = on_disk(cd); parts = [f.name for f in cd.glob("*.at")]
    S["rate"] = 0
    with lk:
        S["sent"] = 0; S["gen"] += 1
    r = L.http_get_parallel(Ctx(), URL, dest, chunk=CH)
    need = len(DATA) - kept
    check(f"{name}: file byte-exact after resume", r is True and dest.read_bytes() == DATA and not cd.exists())
    check(f"{name}: only missing bytes fetched (partials continue)", S["sent"] == need and (parts or sig != signal.SIGKILL),
          f"kept {kept} B on disk ({len(parts)} partial chunks), resume fetched {S['sent']} B, needed {need} B")


killed_then_resumed("SIGKILL mid-chunk", signal.SIGKILL)
killed_then_resumed("SIGTERM (job stop)", signal.SIGTERM)

# no room for the rest: a clear error before any byte is fetched (one data file, no second copy for a join)
d = Path(tempfile.mkdtemp()); real = L.free_bytes
L.http_get_parallel.__globals__["free_bytes"] = lambda p, raw=False: 1
with lk:
    S["sent"] = 0
try:
    L.http_get_parallel(Ctx(), URL, d / "t", chunk=CH); check("no room: refused", False)
except L.CliError as e:
    check("no room: refused up front, nothing fetched", "GB free" in e.msg and S["sent"] == 0, e.msg)
finally:
    L.http_get_parallel.__globals__["free_bytes"] = real
r = L.http_get_parallel(Ctx(), URL, d / "t", chunk=CH)
check("disk freed: same call completes byte-exact, written once (no .part join)",
      r is True and (d / "t").read_bytes() == DATA and not Path(str(d / "t") + ".chunks").exists())

# resume: chunk 0 done + 1000 bytes of chunk 2 synced (done list + .at): only the rest is fetched
d = Path(tempfile.mkdtemp()); cd = Path(str(d / "m") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v1"')
(cd / "data").write_bytes(DATA[:CH] + bytes(CH) + DATA[2 * CH:2 * CH + 1000] + bytes(len(DATA) - 2 * CH - 1000))
(cd / "done").write_text("0\n"); (cd / "00002.at").write_text("1000")
with lk:
    S["sent"] = 0
r = L.http_get_parallel(Ctx(), URL, d / "m", chunk=CH)
check("resumed from done + .at, not refetched", r is True and (d / "m").read_bytes() == DATA and S["sent"] == len(DATA) - CH - 1000,
      f"sent {S['sent']}")

# a .at progress past its chunk (corrupt) is ignored: chunk fetched whole
d = Path(tempfile.mkdtemp()); cd = Path(str(d / "k") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v1"'); (cd / "00001.at").write_text(str(CH + 9))
r = L.http_get_parallel(Ctx(), URL, d / "k", chunk=CH)
check("bad .at ignored", r is True and (d / "k").read_bytes() == DATA)

# progress records without their data file (deleted, crash before it was sized): reset, never trusted over zeros
d = Path(tempfile.mkdtemp()); cd = Path(str(d / "z") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v1"'); (cd / "00001.at").write_text(str(CH // 2)); (cd / "done").write_text("0\n2\n")
r = L.http_get_parallel(Ctx(), URL, d / "z", chunk=CH)
check("stale .at/done without data reset", r is True and (d / "z").read_bytes() == DATA)

# a torn last line in done ("1" of "12\n" cut by a power loss) does not count chunk 1 as done
d = Path(tempfile.mkdtemp()); cd = Path(str(d / "w") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v1"')
fz = cd / "data"; fz.write_bytes(DATA[:CH] + bytes(len(DATA) - CH)); (cd / "done").write_text("0\n1")
with lk:
    S["sent"] = 0
r = L.http_get_parallel(Ctx(), URL, d / "w", chunk=CH)
check("torn done line ignored", r is True and (d / "w").read_bytes() == DATA and S["sent"] == len(DATA) - CH, f"sent {S['sent']}")

# short writes (nearly full disk) are completed, never left as a hole
calls = []
real_pw = os.pwrite
def short_pw(fd, b, off):
    calls.append(len(b)); return real_pw(fd, bytes(b)[:max(1, len(b) // 3)], off)
L.pwrite_all.__globals__["os"].pwrite = short_pw
try:
    d = Path(tempfile.mkdtemp()); r = L.http_get_parallel(Ctx(), URL, d / "s", chunk=CH)
finally:
    os.pwrite = real_pw
check("short pwrite completed byte-exact", r is True and (d / "s").read_bytes() == DATA and len(calls) > 3)

# file republished upstream (new ETag) between runs: old chunks discarded, no mixed file
d = Path(tempfile.mkdtemp()); cd = Path(str(d / "h") + ".chunks"); cd.mkdir()
(cd / "size").write_text(f'{len(DATA)} {CH} "v0"'); (cd / "data").write_bytes(os.urandom(len(DATA))); (cd / "done").write_text("0\n")
r = L.http_get_parallel(Ctx(), URL, d / "h", chunk=CH)
check("upstream change starts over", r is True and (d / "h").read_bytes() == DATA)

print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
