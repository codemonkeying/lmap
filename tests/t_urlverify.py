#!/usr/bin/env python3
"""url sources with several files: each is downloaded as <name>.unverified and checked on a separate pool before it
is renamed into place. A good zip arrives; a zip the server sends corrupt once is fetched again and arrives whole;
one that is always corrupt FAILs and leaves nothing behind; a complete .unverified left by a dead run is checked
and installed without being downloaded again.
Usage: t_urlverify.py <scratch>"""
import http.server, io, json, os, subprocess, sys, tempfile, threading, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


def zbytes(name):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name + ".txt", os.urandom(50_000).hex())
    return b.getvalue()


GOOD, FLAKY, LEFT = zbytes("good"), zbytes("flaky"), zbytes("left")
BAD = b"PK\x03\x04" + os.urandom(40_000)
hits = {}
lk = threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        with lk:
            hits[p] = hits.get(p, 0) + 1; n = hits[p]
        body = {"/good.zip": GOOD, "/bad.zip": BAD, "/left.zip": LEFT,
                "/flaky.zip": BAD if n == 1 else FLAKY}.get(p)
        if body is None:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
cat = tmp / "sources.toml"
cat.write_text(f'''
[source.tz]
title = "test zips"
kind = "url"
urls = ["{base}/good.zip", "{base}/flaky.zip", "{base}/bad.zip", "{base}/left.zip"]
dest = "data/t/tz"
licence = "test"
est_gb = 0.001
''')
root = tmp / "root"; dest = root / "data/t/tz"; dest.mkdir(parents=True)
(dest / "left.zip.unverified").write_bytes(LEFT)               # a dead run's finished, unchecked download
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_CATALOG": str(cat), "LMAP_TMP": str(tmp / "scr"),
       "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "LMAP_BACKOFF": "0.02",
       "LMAP_SCOPE": "0"}
r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "tz", "--yes", "--json"],
                   capture_output=True, text=True, env=env, timeout=240)
try:
    j = json.loads(r.stdout or "{}")
except ValueError:
    j = {}
blob = json.dumps(j) + r.stderr
check((dest / "good.zip").is_file() and (dest / "good.zip").read_bytes() == GOOD, "good zip installed, byte-identical")
check((dest / "flaky.zip").is_file() and (dest / "flaky.zip").read_bytes() == FLAKY and hits.get("/flaky.zip") == 2,
      f"corrupt once -> fetched again -> installed ({hits.get('/flaky.zip')} GETs)")
check(not (dest / "bad.zip").exists() and "bad.zip" in blob, "always corrupt -> FAIL, not installed")
check((dest / "left.zip").is_file() and (dest / "left.zip").read_bytes() == LEFT and not hits.get("/left.zip"),
      f"leftover .unverified checked and installed without a GET ({hits.get('/left.zip', 0)} GETs)")
check(not list(dest.glob("*.unverified")) and not list(dest.glob("*.part")), "no .unverified / .part left")
check(r.returncode != 0, f"run reports the failure (rc {r.returncode})")

# --refresh: an old leftover .unverified must not be installed as the "refreshed" file
(dest / "good.zip.unverified").write_bytes(zbytes("old"))
hits.clear()
r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "tz", "--refresh", "--yes", "--json"],
                   capture_output=True, text=True, env=env, timeout=240)
check((dest / "good.zip").read_bytes() == GOOD and hits.get("/good.zip") == 1,
      f"--refresh drops an old leftover and fetches the server's copy ({hits.get('/good.zip', 0)} GETs)")
check(not list(dest.glob("*.unverified")), "no .unverified left after --refresh")
print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
