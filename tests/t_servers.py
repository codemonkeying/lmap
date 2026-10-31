#!/usr/bin/env python3
"""One per-server table (SERVERS: start/max/slots), sane for every server; config [hosts] / [servers] apply to
every path. Offline."""
import importlib.machinery, importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

# every listed server: sane numbers; an unlisted host gets the defaults
bad = []
for h in sorted(set(L.SERVERS) | {"unknown.example"}):
    p = L.server_profile(h)
    if not (1 <= p["start"] <= p["max"] and p["slots"] >= 1 and L.host_cap(h) == p["slots"]):
        bad.append((h, p))
check(not bad, f"every server has 1 <= start <= max, slots >= 1 ({bad})")
p = L.server_profile("unknown.example")
check((p["start"], p["max"], p["slots"]) == (4, 8, 8), f"unlisted host: defaults 4/8/8 ({p})")
check(L.server_profile("services.slip.wa.gov.au")["max"] <= 4, "Landgate SLIP stays at its measured 4")

import copy
H0, S0 = copy.deepcopy(L.HOSTS_CFG), copy.deepcopy(L.SERVER_CFG)
def reset():
    L.HOSTS_CFG.clear(); L.HOSTS_CFG.update(H0); L.SERVER_CFG.clear(); L.SERVER_CFG.update(S0)
L.HOSTS_CFG["services.ga.gov.au"] = 3
check(L.server_profile("services.ga.gov.au")["max"] == 3 and L.host_cap("services.ga.gov.au") == 3,
      "config [hosts] caps both the tile path and the slot paths")
L.SERVER_CFG["tiles.maps.eox.at"] = {"max": 10}
check(L.server_profile("tiles.maps.eox.at")["max"] == 10 and L.host_cap("tiles.maps.eox.at") == 10,
      "config [servers] max= below slots also caps host_slot / the GDAL path (it used to miss them)")
reset(); L.SERVER_CFG["spatial-img.information.qld.gov.au"] = {"max": 128}
check(L.host_cap("spatial-img.information.qld.gov.au") == 12, "[servers] max= above slots never raises the machine-wide cap")
reset(); L.HOSTS_CFG["services.ga.gov.au"] = 4; L.SERVER_CFG["services.ga.gov.au"] = {"max": 32}
check(L.host_cap("services.ga.gov.au") == 4, "[hosts] cap is not loosened by a bigger [servers] max=")
reset()
L.SERVER_CFG["tiles.maps.eox.at"] = {"max": 10, "slots": 2, "start": 99}
p = L.server_profile("tiles.maps.eox.at")
check((p["start"], p["max"], p["slots"]) == (10, 10, 2), f"[servers] slots= separately; start clamped to max ({p})")
L.SERVER_CFG["x.example"] = {"max": 0}
check(L.server_profile("x.example")["max"] == 1 and L.host_cap("x.example") == 1, "never below 1")
reset()
src = "".join(f.read_text() for f in [HERE / "bin" / "lmap", HERE / "sources.toml", *sorted((HERE / "lmaplib").glob("*.py"))])
check("HOST_LIMITS" not in src and not hasattr(L, "HOST_LIMITS"), "the second table is gone")
print("ALL OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
