#!/usr/bin/env python3
"""Network outage: a download whose server can't be reached waits (tries not counted) and finishes once it is back;
a server reachable but failing still uses up its tries; an outage longer than LMAP_NET_WAIT stops with a clear error.
Usage: t_netdown.py <scratch>"""
import http.server, importlib.machinery, importlib.util, os, socket, sys, tempfile, threading, time
from pathlib import Path

os.environ["LMAP_BACKOFF"] = "0.01"
os.environ["LMAP_NET_WAIT"] = "60"
HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


BODY = os.urandom(200_000)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/bad":
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(BODY))); self.end_headers()
        self.wfile.write(BODY)


s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
url = f"http://127.0.0.1:{port}/f"
logs = []
L._NETLOG[0] = logs.append
srv = {}


def start():
    srv["s"] = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv["s"].serve_forever, daemon=True).start()


# 1. server down for ~20 s (way more than 8 quick tries), then up: the download finishes
threading.Timer(20, start).start()
t0 = time.time()
ok = L.http_get(url, tmp / "f", retries=3)
check(ok is True and (tmp / "f").read_bytes() == BODY, f"download finished after the outage ({time.time() - t0:.0f} s)")
check(any("can't reach" in m for m in logs) and any("reachable again" in m for m in logs), "outage logged once + recovery")
check(sum("can't reach" in m for m in logs) == 1, "one 'can't reach' line, not one per try")

# 2. reachable, server error: tries are counted (fails quickly, no waiting)
t0 = time.time()
try:
    L.http_get(f"http://127.0.0.1:{port}/bad", tmp / "b", retries=3)
    check(False, "server error still fails")
except L.CliError as e:
    check("after 3 tries" in e.msg and time.time() - t0 < 30, f"server error: 3 tries, no outage wait ({time.time() - t0:.0f} s)")

# 3. down longer than LMAP_NET_WAIT: clear error
srv["s"].shutdown(); srv["s"].server_close()
L.NET_WAIT_S = 20
L.net_wait.__globals__["NET_WAIT_S"] = 20
t0 = time.time()
try:
    L.http_get(url, tmp / "g", retries=3)
    check(False, "long outage stops")
except L.CliError as e:
    check("unreachable for" in e.msg, f"long outage stops with a clear error ({e.msg[:70]}, {time.time() - t0:.0f} s)")

# 4. an env proxy carries the request: no outage wait (the probe would wrongly say "down")
os.environ["http_proxy"] = "http://127.0.0.1:9"; os.environ.pop("no_proxy", None); os.environ.pop("NO_PROXY", None)
check(L.net_wait("http://unreachable.invalid/x") is False, "env http_proxy: no outage wait")
del os.environ["http_proxy"]

# 5. a server that comes back but keeps failing: free waits are limited, then tries count
L._net["down"].clear()
st = [time.time(), L.NET_FREE_WAITS, False]
L._net["down"]["127.0.0.1"] = st
check(L.net_wait(url) is False, f"after {L.NET_FREE_WAITS} free waits without an answer: tries count again")
L.net_ok_again(url)
check("127.0.0.1" not in L._net["down"], "an answer clears the outage state")

# 6. a configured proxy reaches GDAL too (children inherit it), and GDAL gets lmap's UA (not GDAL's own)
import subprocess
code = ("import importlib.machinery as m, importlib.util as u, os, sys\n"
        "ld = m.SourceFileLoader('lmap', sys.argv[1]); L = u.module_from_spec(u.spec_from_loader('lmap', ld)); ld.exec_module(L)\n"
        "class A: pass\n"
        "a = A()\n"
        "for k in ('root', 'catalog', 'tmp', 'jobs', 'shrink', 'dry_run', 'yes', 'verbose', 'quiet', 'json'): setattr(a, k, None)\n"
        "a.root = sys.argv[2]; L.Ctx(a)\n"
        "print(os.environ.get('GDAL_HTTP_PROXY'), os.environ.get('GDAL_HTTP_USERAGENT') == L.UA, 'GDAL/' not in L.UA)\n")
env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower() and k != "GDAL_HTTP_USERAGENT"}
env["LMAP_PROXY"] = "http://user:pw@127.0.0.1:3128"
r = subprocess.run([sys.executable, "-c", code, str(HERE / "bin" / "lmap"), str(tmp / "root")], env=env,
                   capture_output=True, text=True, timeout=60)
check(r.stdout.split() == ["http://user:pw@127.0.0.1:3128", "True", "True"],
      f"config proxy + UA passed to GDAL children ({r.stdout.strip() or r.stderr.strip()[-200:]})")

# 7. in-memory GETs reuse one kept-alive connection; 404, redirects and a restarted server still work
conns = set()


class K(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        conns.add(self.client_address[1])
        if self.path == "/404":
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        if self.path == "/redir":
            self.send_response(302); self.send_header("Location", "/ok"); self.send_header("Content-Length", "0")
            self.end_headers(); return
        b = b'{"a":1}'; self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)


k = http.server.ThreadingHTTPServer(("127.0.0.1", 0), K); threading.Thread(target=k.serve_forever, daemon=True).start()
ku = f"http://127.0.0.1:{k.server_port}"
check(all(L.http_get(ku + "/ok") == b'{"a":1}' for _ in range(20)) and len(conns) == 1,
      f"20 GETs on one kept-alive connection ({len(conns)})")
check(L.http_get(ku + "/404") is None, "keep-alive: 404 -> None")
check(L.http_get(ku + "/redir") == b'{"a":1}', "keep-alive: redirect followed")
kp = k.server_port; k.shutdown(); k.server_close()
k = http.server.ThreadingHTTPServer(("127.0.0.1", kp), K); threading.Thread(target=k.serve_forever, daemon=True).start()
check(L.http_get(ku + "/ok") == b'{"a":1}', "keep-alive: server restarted -> reconnects")
k.shutdown()

# 8. one set of GDAL HTTP retry/timeout settings; overrides win; as --config args
g = L.gdal_env()
check(g["GDAL_HTTP_RETRY_CODES"] == "429,500,502,503,504" and g["GDAL_HTTP_TIMEOUT"] == "90"
      and g["GDAL_HTTP_CONNECTTIMEOUT"] == "20" and g["GDAL_HTTP_TCP_KEEPALIVE"] == "YES"
      and g["GDAL_HTTP_MAX_RETRY"] == "5" and g["GDAL_HTTP_RETRY_DELAY"] == "5", "gdal_env defaults")
z = L.gdal_env({"GDAL_HTTP_MAX_RETRY": 0})
check(z["GDAL_HTTP_MAX_RETRY"] == "0" and L.gdal_env()["GDAL_HTTP_MAX_RETRY"] == "5", "gdal_env override, no shared state")
os.environ["GDAL_HTTP_TIMEOUT"] = "300"
u = L.gdal_env({"GDAL_HTTP_MAX_RETRY": 0})
check("GDAL_HTTP_TIMEOUT" not in u and u["GDAL_HTTP_MAX_RETRY"] == "0", "gdal_env: user's env setting is left alone")
del os.environ["GDAL_HTTP_TIMEOUT"]
check(L.gdal_config({"A": "1", "B": "2"}) == ["--config", "A", "1", "--config", "B", "2"], "gdal_config args")

print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
