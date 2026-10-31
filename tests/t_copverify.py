#!/usr/bin/env python3
"""copernicus tiles: each is downloaded as <tile>.tif.unverified and decoded on a separate pool before it is renamed
into place. A good tile arrives; one the server sends corrupt once is fetched again (round 2) and arrives whole;
a 404 is listed as no-data; a complete .unverified left by a stopped run is checked and installed without a GET.
Usage: t_copverify.py <scratch>"""
import http.server, json, os, subprocess, sys, tempfile, threading
from pathlib import Path
from osgeo import gdal

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


def tif(v):
    p = tmp / f"src{v}.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(p), 64, 64, 1, gdal.GDT_Int16, ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform([140, 1 / 64, 0, -10, 0, -1 / 64]); ds.GetRasterBand(1).Fill(v); ds = None
    return p.read_bytes()


def name(la, lo):
    return f"Copernicus_DSM_COG_30_S{la:02d}_00_E{lo:03d}_00_DEM"


GOOD, FLAKY, LEFT = tif(1), tif(2), tif(3)
BAD = GOOD[:len(GOOD) // 2] + os.urandom(len(GOOD) - len(GOOD) // 2)
T_GOOD, T_FLAKY, T_NONE, T_LEFT = name(10, 140), name(10, 141), name(11, 140), name(11, 141)
hits, lk = {}, threading.Lock()


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        t = self.path.strip("/").split("/")[0]
        with lk:
            hits[t] = hits.get(t, 0) + 1; n = hits[t]
        body = {T_GOOD: GOOD, T_LEFT: LEFT, T_FLAKY: BAD if n == 1 else FLAKY}.get(t)
        if body is None:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
cat = tmp / "sources.toml"
cat.write_text(f'''
[source.tc]
title = "test dem"
kind = "copernicus"
res = 90
lat = [10, 11]
lon = [140, 141]
base = "http://127.0.0.1:{srv.server_address[1]}"
dest = "data/t/tc"
licence = "test"
est_gb = 0.001
''')
root = tmp / "root"; dest = root / "data/t/tc"; dest.mkdir(parents=True)
(dest / f"{T_LEFT}.tif.unverified").write_bytes(LEFT)          # a stopped run's finished, unchecked download
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_CATALOG": str(cat), "LMAP_TMP": str(tmp / "scr"),
       "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "LMAP_BACKOFF": "0.02",
       "LMAP_SCOPE": "0"}
r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "tc", "--yes", "--json"],
                   capture_output=True, text=True, env=env, timeout=240)
f = lambda t: dest / f"{t}.tif"
check(f(T_GOOD).is_file() and f(T_GOOD).read_bytes() == GOOD, f"good tile installed ({r.stderr[-300:]})")
check(f(T_FLAKY).is_file() and f(T_FLAKY).read_bytes() == FLAKY and hits.get(T_FLAKY) == 2,
      f"corrupt once -> round 2 -> installed ({hits.get(T_FLAKY)} GETs)")
check(not f(T_NONE).exists() and T_NONE in (dest / "_missing.txt").read_text(), "404 -> no-data listed")
check(f(T_LEFT).is_file() and f(T_LEFT).read_bytes() == LEFT and not hits.get(T_LEFT),
      f"leftover .unverified checked and installed without a GET ({hits.get(T_LEFT, 0)} GETs)")
check(not list(dest.glob("*.unverified*")) and not list(dest.glob("*.part")), "no .unverified / .part left")
check(r.returncode == 0, f"run ok (rc {r.returncode})")
print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
