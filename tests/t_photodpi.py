#!/usr/bin/env python3
"""photo-sheet dpi precedence (no QGIS): config show reports photo_dpi = env > config > 1200."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
L = Path(__file__).resolve().parent.parent / "bin" / "lmap"; fails = 0
def show(env_dpi=None, cfg=None):
    d = tempfile.mkdtemp(); c = Path(d) / "lmap" / "config.toml"; c.parent.mkdir(); c.write_text(cfg or "")
    e = {k: v for k, v in os.environ.items() if k != "LMAP_PHOTO_DPI"}; e["XDG_CONFIG_HOME"] = d
    if env_dpi: e["LMAP_PHOTO_DPI"] = env_dpi
    r = subprocess.run([sys.executable, str(L), "--json", "config", "show"], env=e, capture_output=True, text=True)
    j = json.loads(r.stdout)
    return str(next((s["value"] for s in j["settings"] if s["key"] == "photo_dpi"), None))
def ck(name, got, want):
    global fails
    ok = got == want; fails += not ok; print(("PASS " if ok else "FAIL ") + f"{name} ({got})")
ck("default 1200", show(), "1200")
ck("config [build] photo_dpi", show(cfg="[build]\nphoto_dpi = 600\n"), "600")
ck("env beats config", show("900", "[build]\nphoto_dpi = 600\n"), "900")
print("ALL PASS" if not fails else f"{fails} FAIL"); sys.exit(1 if fails else 0)
