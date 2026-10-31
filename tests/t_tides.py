#!/usr/bin/env python3
"""qld-tides datastore fallback (404 download link) + du_tree for build-input status. Offline."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import importlib.machinery, importlib.util, json, sys, tempfile
from pathlib import Path

ld = importlib.machinery.SourceFileLoader("lmap", str(Path(__file__).resolve().parent.parent / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


d = Path(tempfile.mkdtemp())
(d / "q100248_golding_2026_hilo.csv").write_text(
    "Tidal readings provided on 29 Oct 2025\nIDENTIFICATION\nTidal Station Number                       =,100248\n"
    "Tidal Station Name                         =,GOLDING\nLatitude  Degrees Minutes                  =,-23 56\n"
    "Longitude Degrees Minutes                  =, 151 27\nREADINGS\nNumber of valid readings =,2\n"
    "01/01/2026 , 00:24 , -1 ,   0.680\n01/01/2026 , 06:52 ,  1 ,   3.870\n")
recs = [{"Date": "01/01/2027", "Time": "03:53", "Ind": "1", "Reading": "3.060"},
        {"Date": "01/01/2027", "Time": "10:02", "Ind": "-1", "Reading": "1.550"}]
L.http_get = lambda u, *a, **k: json.dumps({"result": {"records": recs, "total": len(recs)}}).encode()
n = "q100248_golding_2027_hilo.csv"
L.tides_from_datastore(d, n, "rid")
f = d / n
rows = L.tide_rows(f)
check(f.read_text().startswith("Tidal readings"), "rebuilt file passes the fetch's header check")
check([(t.strftime("%Y-%m-%d %H:%M"), h, i) for t, h, i in rows] ==
      [("2027-01-01 03:53", 3.06, 1), ("2027-01-01 10:02", 1.55, -1)], "readings parsed back exactly")
check("Latitude  Degrees Minutes                  =,-23 56" in f.read_text(), "station header taken from the other year")
check(not list(d.glob(".*.part")), "no part file left")

L.http_get = lambda u, *a, **k: json.dumps({"result": {"records": recs[:1], "total": 2}}).encode()
try:
    L.tides_from_datastore(d, "q100248_golding_2028_hilo.csv", "rid"); ok = False
except ValueError:
    ok = True
check(ok and not (d / "q100248_golding_2028_hilo.csv").exists(), "short datastore answer refused, no file written")
try:
    L.tides_from_datastore(d, "q999_nosib_2027_hilo.csv", "rid"); ok = False
except ValueError:
    ok = True
check(ok, "no other year of the gauge -> refused (no header to give it)")

code = lmap_source()
check('is None:\n                raise CliError(E_UPSTREAM, "404")' in code, "a 404 (http_get -> None) triggers the fallback")

t = Path(tempfile.mkdtemp()); (t / "z17").mkdir(); (t / "z17" / "a.tif").write_bytes(b"x" * 1000); (t / "c").write_bytes(b"y" * 10)
check(L.du_tree(t) == 1010 and L.du(t) == 10, "du_tree counts subfolders (build-input status), du doesn't")
print("ALL PASS" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
