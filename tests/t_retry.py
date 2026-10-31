#!/usr/bin/env python3
"""RetryPolicy: http_get/_head give up at once on a permanent HTTP answer (401/410...), still
retry 403/429/5xx, honour Retry-After, and never sleep after the last try. Local HTTP server, no internet."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
_o.environ["LMAP_BACKOFF"] = "0.01"
import importlib.machinery, importlib.util, threading, time, http.server, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

hits = {}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self, body=True):
        p = self.path; hits[p] = hits.get(p, 0) + 1
        if p.startswith("/big/"):
            return self._big(body)
        code = int(p.strip("/").split("/")[0])
        if code == 503 and hits[p] >= 3:
            code = 200
        self.send_response(code)
        if code == 429 or (code == 503):
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", "2"); self.end_headers()
        if body:
            self.wfile.write(b"ok")
    def _big(self, body):
        data = bytes(range(256)) * 400                     # 102400 bytes; chunk 16384 -> 7 chunks
        rg = self.headers.get("Range")
        if rg and body and rg != "bytes=0-0":
            a, b = (int(x) for x in rg.split("=")[1].split("-"))
            if a >= 3 * 16384 and "/401" in self.path:     # one chunk refused for good
                self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(206); self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            self.send_header("Content-Length", str(b - a + 1)); self.end_headers(); self.wfile.write(data[a:b + 1]); return
        self.send_response(200); self.send_header("Accept-Ranges", "bytes"); self.send_header("ETag", '"e1"')
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        if body:
            self.wfile.write(data)
    do_GET = _go
    def do_HEAD(self): self._go(False)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
if hasattr(L, "net_wait"):
    L.net_wait = lambda *a, **k: False

for code in (401, 410):
    u = f"{base}/{code}/x"
    try:
        L.http_get(u, retries=5); check(False, f"{code} raises")
    except L.CliError as e:
        check(hits.get(f"/{code}/x") == 1 and "not retried" in e.msg, f"http_get {code}: one try, not retried")
for code in (400, 403, 500):     # 400: S3 RequestTimeout is transient
    u = f"{base}/{code}/y"
    try:
        L.http_get(u, retries=3)
    except L.CliError:
        pass
    check(hits.get(f"/{code}/y") == 3, f"http_get {code}: retried (3 tries)")
t = time.time(); b = L.http_get(f"{base}/503/z", retries=5); dt = time.time() - t
check(b == b"ok" and hits["/503/z"] == 3 and dt >= 1.9, f"503 + Retry-After: 1 s honoured twice ({dt:.1f}s)")
pol = L.RetryPolicy(3)
t = time.time(); r = pol.wait(2); check(r is False and time.time() - t < 0.05, "no sleep after the last try")
check(L.RetryPolicy(3, 2, 300).delay(20) <= 300 * 1.5 * L.BACKOFF + 1e-9, "backoff capped")
try:
    L._head(f"{base}/401/h"); check(False, "_head 401 raises")
except L.CliError:
    check(hits.get("/401/h") == 2, "_head 401: HEAD + range GET once, then gives up")
class Ctx:
    def log(self, m): pass
T = Path(tempfile.mkdtemp(prefix="lmap-trt-"))
try:
    d = T / "ok.bin"
    check(L.http_get_parallel(Ctx(), f"{base}/big/ok", d, chunk=16384) and d.stat().st_size == 102400,
          "parallel download completes")
    d = T / "bad.bin"
    try:
        L.http_get_parallel(Ctx(), f"{base}/big/401", d, chunk=16384); check(False, "parallel 401 raises")
    except L.CliError as e:
        check("not retried" in e.msg and "chunk" in e.msg, "parallel: a 401 chunk fails at once, says which chunk")
        check((T / "bad.bin.chunks").is_dir() and not d.exists(), "parallel: finished chunks kept for resume")
    d = T / "gone.bin"; Path(str(d) + ".part").write_bytes(b"xx")
    try:
        L.http_get(f"{base}/410/f", d, retries=4); check(False, "410 dest raises")
    except L.CliError:
        check(not Path(str(d) + ".part").exists() and hits["/410/f"] == 1, "410 with dest: one try, .part removed")
finally:
    import shutil; shutil.rmtree(T, ignore_errors=True)
import email.utils
class E: pass
def hd(v):
    e = E(); e.headers = {"Retry-After": v}; return e
pol = L.RetryPolicy(5, 2, 300)
check(abs(pol.delay(0, hd("7")) - 7) < 0.05 or pol.delay(0, hd("7")) >= 7, "Retry-After seconds")
check(6 <= pol.delay(0, hd(email.utils.formatdate(time.time() + 8, usegmt=True))) <= 9, "Retry-After as an HTTP date")
check(pol.delay(0, hd("99999")) == 600, "Retry-After capped at 10 min")
check(pol.delay(0, hd("junk")) < 1, "bad Retry-After ignored")
# GA catalogue search (POST) and ArcGIS non-JSON: behaviour against the stub
def do_POST(self):
    p = self.path; hits[p] = hits.get(p, 0) + 1; code = int(p.strip("/").split("/")[0])
    self.send_response(code); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
H.do_POST = do_POST
class SC:
    def log(self, m): pass
T2 = Path(tempfile.mkdtemp(prefix="lmap-trg-")); L.sheets_dir = lambda ctx: T2
for code, n in ((401, 1), (503, 4)):
    L.ECAT_SEARCH = f"{base}/{code}/ga"
    try:
        L.sheet_url(SC(), "12345"); check(False, f"GA {code} raises")
    except L.CliError as e:
        check(hits.get(f"/{code}/ga") == n and str(code) in e.msg, f"GA search {code}: {n} request(s), then fails")
import shutil; shutil.rmtree(T2, ignore_errors=True)
def html(self):
    hits[self.path] = hits.get(self.path, 0) + 1
    self.send_response(200); self.send_header("Content-Length", "6"); self.end_headers(); self.wfile.write(b"<html>")
H._go_orig = H._go
H.do_GET = lambda self: html(self) if self.path.startswith("/arc/") else H._go_orig(self)
t = time.time()
try:
    L.arcgis_query(f"{base}/arc/x", {"f": "json"}); check(False, "non-JSON raises")
except L.CliError as e:
    n = sum(v for k, v in hits.items() if k.startswith("/arc/"))
    check("non-JSON" in e.msg and n == 4, f"ArcGIS non-JSON: 4 tries then fails ({n})")
check(time.time() - t < 5, "ArcGIS retry waits short (scaled by BACKOFF)")
L.ABORT.set()
try:
    L.RetryPolicy(4, 5, 60).wait(0); check(False, "abort during a retry wait raises")
except L.Aborted:
    check(True, "abort during a retry wait raises Aborted")
finally:
    L.ABORT.clear()
L.ABORT.set()
try:
    check(L.RetryPolicy(4).wait(3) is False, "last try: no wait, no raise even while aborting")
finally:
    L.ABORT.clear()
k = (HERE / "lmaplib" / "02_kinds.py").read_text()
check(k.count("if DRAIN.wait(random.uniform(30, 60)):") == 2 and "if ABORT.wait(random.uniform(30, 60)):" not in k,
      "WMS + TMS retry waits end on `lmap stop` (DRAIN)")
srv.shutdown()
print("FAILED: " + "; ".join(fails) if fails else "OK")
raise SystemExit(1 if fails else 0)
