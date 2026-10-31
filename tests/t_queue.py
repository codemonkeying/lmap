#!/usr/bin/env python3
"""queue end to end against a local web server: add two url sources, `queue run --detach` returns at once with the
supervisor's pid (a second --detach finds it, no duplicate), both files arrive whole, status shows them done,
stop on an idle/finished queue is a no-op, and a slow source stopped with `queue stop` resumes on the next run.
Usage: t_queue.py <scratch>"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


A = os.urandom(300_000); B = os.urandom(200_000); SLOW = os.urandom(4_000_000)
slow_go = threading.Event()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        body = {"/a.bin": A, "/b.bin": B, "/slow.bin": SLOW}.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404); return
        rng = self.headers.get("Range"); start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body) - start)); self.send_header("Accept-Ranges", "bytes")
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        if head:
            return
        try:
            for i in range(start, len(body), 65536):
                if self.path.startswith("/slow") and not slow_go.is_set():
                    time.sleep(0.05)                       # ~1.3 MB/s until released
                self.wfile.write(body[i:i + 65536])
        except (BrokenPipeError, ConnectionResetError):
            pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
cat = tmp / "sources.toml"
cat.write_text("".join(f'''
[source.{i}]
title = "test {i}"
kind = "url"
urls = ["{base}/{f}"]
dest = "data/t/{i}"
licence = "test"
est_gb = 0.001
''' for i, f in (("ta", "a.bin"), ("tb", "b.bin"), ("ts", "slow.bin"))))
root = tmp / "root"; root.mkdir()
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_CATALOG": str(cat), "LMAP_TMP": str(tmp / "scr"),
       "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "LMAP_BACKOFF": "0.02",
       "LMAP_SCOPE": "0"}


def lmap(*a, timeout=120):
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), *a, "--json"], capture_output=True, text=True,
                       env=env, timeout=timeout)
    try:
        return r.returncode, json.loads(r.stdout or "{}"), r.stderr
    except ValueError:
        return r.returncode, {}, r.stderr


def wait(pred, t=60):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.3)
    return False


def status():
    return {r["id"]: r for r in lmap("queue", "status")[1].get("queue", [])}


rc, j, err = lmap("queue", "add", "ta", "tb", "--yes")
check(rc == 0, f"queue add ({err[-200:]})")
t0 = time.time()
rc, j, err = lmap("queue", "run", "--detach", "--yes")
pid = j.get("pid")
check(rc == 0 and pid and time.time() - t0 < 25, f"--detach returns quickly with the supervisor pid ({pid}; {err[-200:]})")
got = lambda i, b: (root / "data/t" / i / ("a.bin" if i == "ta" else "b.bin")).is_file() and \
    (root / "data/t" / i / ("a.bin" if i == "ta" else "b.bin")).read_bytes() == b
check(wait(lambda: got("ta", A) and got("tb", B)), "both files arrive, byte-identical")
check(wait(lambda: all(r["state"] in ("done", "ok") for r in status().values())), f"status: both done ({ {k: v['state'] for k, v in status().items()} })")
check(not list(root.rglob("*.part")), "no .part files left")
log = tmp / "state" / "lmap" / "queue.log"
check(log.is_file() and (log.stat().st_mode & 0o777) == 0o600, "queue.log written, private (0600)")

# stop + resume: a slow source
check(wait(lambda: lmap("queue", "status")[1].get("supervisor") is None, 90), "idle queue supervisor exits")
lmap("queue", "add", "ts", "--yes")
rc, j, err = lmap("queue", "run", "--detach", "--yes"); pid = j.get("pid")
part = lambda: [p for p in (tmp / "scr").rglob("*") if p.is_file() and "slow" in p.name] + \
    [p for p in root.rglob("*") if p.is_file() and "slow" in p.name]
check(wait(lambda: any(p.stat().st_size > 200_000 for p in part()), 60), "slow download under way")
rc2, j2, _ = lmap("queue", "run", "--detach", "--yes")
check(j2.get("pid") == pid and j2.get("unchanged"), f"second --detach finds the running supervisor ({j2.get('pid')} vs {pid})")
rc, j, err = lmap("queue", "stop", "--now", "--yes")
check(rc == 0 and j.get("mode") == "now", f"queue stop --now ({err[-200:]})")
check(wait(lambda: lmap("queue", "status")[1].get("supervisor") is None, 60), "supervisor gone after stop")
have = max((p.stat().st_size for p in part()), default=0)
check(0 < have < len(SLOW), f"partial kept for resume ({have:,} of {len(SLOW):,} B)")
slow_go.set()
rc, j, err = lmap("queue", "run", "--detach", "--yes")
f = root / "data/t/ts/slow.bin"
check(wait(lambda: f.is_file() and f.read_bytes() == SLOW, 90), "resumed run finishes the file, byte-identical")
rc, j, err = lmap("queue", "stop", "--yes")
check(rc == 0, "stop when idle/finished: no error")
wait(lambda: lmap("queue", "status")[1].get("supervisor") is None, 60)

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
