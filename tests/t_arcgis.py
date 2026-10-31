#!/usr/bin/env python3
"""ArcGIS keyset download against a local fake MapServer (1,000 points, page 50, quantized geometry): a full fetch
stores every row once with the right coordinates; a fetch stopped part way keeps its rows + checkpoint and the next
run resumes (no duplicates, no gaps, fewer requests than starting over); a layer name that isn't a plain SQL
identifier is refused before any SQL is built. Usage: t_arcgis.py <scratch>"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


N = 1000
ROWS = {i: (146.0 + (i % 40) * 0.01, -19.0 - (i // 40) * 0.01, f"pt{i}") for i in range(1, N + 1)}
SX = 1e-7
STATE = {"queries": 0}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, d):
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/MapServer/0") and "/query" not in u.path:
            return self.send_json({"type": "Feature Layer", "name": "points", "maxRecordCount": 50,
                                   "objectIdField": "OBJECTID", "geometryType": "esriGeometryPoint",
                                   "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                                              {"name": "name", "type": "esriFieldTypeString"}]})
        if not u.path.endswith("/MapServer/0/query"):
            self.send_error(404); return
        w = q.get("where", "1=1")
        if q.get("returnCountOnly"):
            return self.send_json({"count": N})
        if q.get("outStatistics"):
            return self.send_json({"features": [{"attributes": {"min": 1, "max": N, "count": N}}]})
        STATE["queries"] += 1
        import re
        lo = int(re.search(r"OBJECTID > (-?\d+)", w)[1]); hi = int(re.search(r"OBJECTID <= (-?\d+)", w)[1])
        ids = [i for i in sorted(ROWS) if lo < i <= hi][:int(q.get("resultRecordCount") or 50)]
        tx, ty = 140.0, -10.0
        feats = [{"attributes": {"OBJECTID": i, "name": ROWS[i][2]},
                  "geometry": {"x": round((ROWS[i][0] - tx) / SX), "y": round((ty - ROWS[i][1]) / SX)}} for i in ids]
        self.send_json({"objectIdFieldName": "OBJECTID", "geometryType": "esriGeometryPoint",
                        "spatialReference": {"wkid": 4326},
                        "transform": {"originPosition": "upperLeft", "scale": [SX, SX, 0, 0], "translate": [tx, ty, 0, 0]},
                        "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "name", "type": "esriFieldTypeString"}],
                        "features": feats})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/arcgis/rest/services/T/MapServer"


def catalog(layer="points"):
    c = tmp / "sources.toml"
    c.write_text(f'''
[source.tarc]
title = "test arcgis"
kind = "arcgis"
url = "{base}"
layers = {{ {json.dumps(layer)} = 0 }}
bbox = [145.0, -21.0, 148.0, -18.0]
dest = "data/t/tarc.gpkg"
licence = "test"
''')
    return c


root = tmp / "root"; root.mkdir()
env = {**os.environ, "LMAP_ROOT": str(root), "LMAP_CATALOG": str(catalog()), "LMAP_TMP": str(tmp / "scr"),
       "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "cfg"), "LMAP_BACKOFF": "0.02",
       "LMAP_SCOPE": "0", "LMAP_HOSTS": ""}


def fetch(timeout=240):
    return subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "tarc", "--yes", "--json"],
                          capture_output=True, text=True, env=env, timeout=timeout)


def rows(p):
    from osgeo import ogr
    ogr.UseExceptions()
    ds = ogr.Open(str(p)); L = ds.GetLayerByName("points"); out = {}
    for f in L:
        g = f.GetGeometryRef(); out[f.GetFID()] = (round(g.GetX(), 6), round(g.GetY(), 6), f["name"])
    ds = None
    return out


out = root / "data/t/tarc.gpkg"
r = fetch()
check(r.returncode == 0 and out.is_file(), f"full fetch ({r.stderr.strip().splitlines()[-1:] if r.returncode else ''})")
full_q = STATE["queries"]
if out.is_file():
    got = rows(out)
    check(len(got) == N and set(got) == set(ROWS), f"every row once ({len(got)} of {N})")
    bad = [i for i in ROWS if got.get(i) and (abs(got[i][0] - ROWS[i][0]) > 1e-6 or abs(got[i][1] - ROWS[i][1]) > 1e-6)]
    check(not bad, f"coordinates de-quantized right ({len(bad)} off)")
    check(all(got[i][2] == ROWS[i][2] for i in got), "attributes kept")

# stopped part way: kill the client after some pages, then resume
out.unlink(); STATE["queries"] = 0
p = subprocess.Popen([sys.executable, str(HERE / "bin" / "lmap"), "data", "fetch", "tarc", "--yes", "--json"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
import time
end = time.time() + 60
while STATE["queries"] < 6 and time.time() < end and p.poll() is None:
    time.sleep(0.01)
p.kill(); p.wait()
part = tmp / "scr" / f"lmap-{os.getuid()}" / "tarc.part.gpkg"
first = STATE["queries"]
check(not out.exists() and part.is_file(), f"killed after {first} pages: nothing installed, partial kept "
      "(its SQLite journal is rolled back on resume)")
STATE["queries"] = 0
r = fetch()
check(r.returncode == 0 and out.is_file(), f"resumed fetch ({r.stderr.strip().splitlines()[-1:] if r.returncode else ''})")
check("resuming" in r.stderr, "it said it was resuming")
if out.is_file():
    got = rows(out)
    check(len(got) == N and set(got) == set(ROWS), f"after resume: every row exactly once ({len(got)} of {N})")
check(STATE["queries"] < full_q, f"resume fetched fewer pages than a full run ({STATE['queries']} vs {full_q})")
check(not part.exists(), "partial removed after install")

# a layer name that would go into SQL text unquoted
env["LMAP_CATALOG"] = str(catalog('x; DROP TABLE y'))
out.unlink(missing_ok=True)
r = fetch()
check(r.returncode == 2 and "not a plain name" in r.stderr, f"unsafe layer name refused (rc {r.returncode})")

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else "SOME FAILED")
sys.exit(1 if fails else 0)
