#!/usr/bin/env python3
"""An ArcGIS tile service's own export permission (exportTilesAllowed) is read once per service and logged at
fetch start; a non-ArcGIS url is skipped; a broken/odd answer never blocks the fetch. Local HTTP server, no internet."""
import os as _o
_o.environ["LMAP_BACKOFF"] = "0.01"
import importlib.machinery, importlib.util, threading, http.server, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

hits = {}
BODY = {"/a/MapServer": b'{"exportTilesAllowed": true, "maxExportTilesCount": 100000}',
        "/b/ImageServer": b'{"exportTilesAllowed": false}',
        "/c/MapServer": b'{"capabilities": "Map"}',
        "/d/MapServer": b'<html>gateway</html>',
        "/e/MapServer": b'{"exportTilesAllowed": "yes"}', "/f/MapServer": b'[]', "/g/MapServer": b'null',
        "/q/MapServer": b'{"exportTilesAllowed": true, "maxExportTilesCount": "lots"}'}
import time
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]; hits[p] = hits.get(p, 0) + 1
        if p == "/slow/MapServer": time.sleep(15)
        b = BODY.get(p)
        self.send_response(200 if b else 503 if p == "/u/MapServer" else 404); self.send_header("Content-Length", str(len(b or b"")))
        self.end_headers(); self.wfile.write(b or b"")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
u = f"http://127.0.0.1:{srv.server_address[1]}"
T = "/tile/${z}/${y}/${x}"

check(L.arcgis_export_terms(u + "/a/MapServer" + T) == (True, 100000), "allowed + max read from the service root")
check(L.arcgis_export_terms(u + "/a/MapServer/tile/1/2/3") == (True, 100000) and hits["/a/MapServer"] == 1,
      "asked once per service (cached)")
check(L.arcgis_export_terms(u + "/b/ImageServer" + T) == (False, None), "not allowed")
check(L.arcgis_export_terms(u + "/c/MapServer" + T) == (None, None), "field absent: unknown, not 'allowed'")
check(L.arcgis_export_terms(u + "/d/MapServer" + T) == (None, None), "non-JSON answer: unknown, no crash")
check(L.arcgis_export_terms(u + "/e/MapServer" + T) == (None, None), "non-boolean field: unknown")
check(L.arcgis_export_terms(u + "/x/MapServer" + T) == (None, None), "404: unknown, no crash")
for k in ("f", "g"):
    check(L.arcgis_export_terms(u + f"/{k}/MapServer" + T) == (None, None), f"JSON {BODY['/'+k+'/MapServer']!r}: unknown, no crash")
check(L.arcgis_export_terms(u + "/q/MapServer?token=x") == (True, None), "url with a query string; non-integer max dropped")
t0 = time.time(); r = L.arcgis_export_terms(u + "/u/MapServer" + T)
check(r == (None, None) and hits["/u/MapServer"] == 1 and time.time() - t0 < 3, "503: one request, no retry/backoff")
t0 = time.time(); r = L.arcgis_export_terms(u + "/slow/MapServer" + T)
check(r == (None, None) and time.time() - t0 < 13, f"slow server: gives up within ~10 s ({time.time() - t0:.1f}s)")
n = sum(hits.values())
check(L.arcgis_export_terms(u + "/tiles/${z}/${x}/${y}.png") is None and sum(hits.values()) == n,
      "non-ArcGIS url: no request")

class C:
    def __init__(self): self.out = []
    def log(self, m): self.out.append(m)
for path, want in (("/a/MapServer", "allows offline tile export"), ("/b/ImageServer", "does not enable"),
                   ("/c/MapServer", "could not read")):
    c = C(); L.log_export_terms(c, {"id": "x"}, u + path + T)
    check(len(c.out) == 1 and want in c.out[0], f"logged: {want!r}")
c = C(); L.log_export_terms(c, {"id": "x"}, u + "/tiles/${z}/${x}/${y}.png")
check(c.out == [], "non-ArcGIS: nothing logged")
src = (HERE / "lmaplib" / "02_kinds.py").read_text() + (HERE / "lmaplib" / "05_area.py").read_text()
check(len(__import__("re").findall(r"(?<!def )log_export_terms\(ctx, s,", src)) == 2, "called at both tile-fetch starts (wms-tiles, LiDAR tiles)")
print("ALL OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
