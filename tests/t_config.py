#!/usr/bin/env python3
"""lmap config show: value + origin per setting (flag > env > config > default), [limits]/[hosts] tables,
proxy password hidden, unknown config keys reported (exit 1), a broken config file is a usage error."""
import json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what)
    fails += not ok


tmp = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
xc = tmp / "xdg"; (xc / "lmap").mkdir(parents=True)
env0 = {k: v for k, v in os.environ.items() if not k.startswith("LMAP_")}
env0.update(XDG_CONFIG_HOME=str(xc), XDG_CACHE_HOME=str(tmp / "cache"), XDG_DATA_HOME=str(tmp / "data"),
            XDG_STATE_HOME=str(tmp / "state"), LMAP_SCOPE="0")


def show(*flags, **env):
    r = subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "--json", *flags, "config", "show"],
                       capture_output=True, text=True, env={**env0, **env}, timeout=60)
    try:
        return r.returncode, json.loads(r.stdout or r.stderr)
    except ValueError:
        return r.returncode, {"raw": r.stdout + r.stderr}


def row(d, k):
    """The setting's row without its section (checked on its own below)."""
    r = next((r for r in d.get("settings", []) if r["key"] == k), {})
    return {x: y for x, y in r.items() if x != "section"}


def lmap_run(*a):
    return subprocess.run([sys.executable, str(HERE / "bin" / "lmap"), "--json", *a], capture_output=True, text=True,
                          env=env0, timeout=60)


rc, d = show()
check(rc == 0 and d.get("exists") is False, "no config file: ok, exists false")
check(row(d, "root").get("from") == "default" and row(d, "limits.disk") == {"key": "limits.disk", "value": "80%",
                                                                           "from": "default"}, "defaults shown")

cfgf = xc / "lmap" / "config.toml"
cfgf.write_text(f'[paths]\nroot = "{tmp / "r1"}"\n[net]\njobs = 5\n[limits]\ncpu = 50\n[hosts]\n"a.example" = 2\n')
rc, d = show()
check(rc == 0 and row(d, "root") == {"key": "root", "value": str(tmp / "r1"), "from": "config"}, "root from config")
check(row(d, "jobs").get("value") == 5 and row(d, "jobs").get("from") == "config", "jobs from config")
check(row(d, "limits.cpu") == {"key": "limits.cpu", "value": "50%", "from": "config"}, "[limits] cpu from config")
check(row(d, "hosts.a.example").get("value") == 2, "[hosts] entries listed")

rc, d = show(LMAP_ROOT=str(tmp / "r2"), LMAP_LIMIT_CPU="40", LMAP_JOBS="7")
check(row(d, "root") == {"key": "root", "value": str(tmp / "r2"), "from": "env LMAP_ROOT"}, "env beats config")
check(row(d, "limits.cpu").get("from") == "env LMAP_LIMIT_CPU" and row(d, "limits.cpu").get("value") == "40%",
      "env limit beats config")
check(row(d, "jobs").get("value") == 7, "env jobs beats config")

rc, d = show("--root", str(tmp / "r3"), LMAP_ROOT=str(tmp / "r2"))
check(row(d, "root") == {"key": "root", "value": str(tmp / "r3"), "from": "flag"}, "flag beats env")

rc, d = show(LMAP_PROXY="http://user:s3cret@proxy.example:3128")
check("s3cret" not in json.dumps(d) and row(d, "proxy").get("value") == "http://***@proxy.example:3128",
      "proxy password hidden")

for raw, want in (("http://user:pa@ss@proxy.example:3128", "http://***@proxy.example:3128"),
                  ("http://user:p/w@proxy.example", "http://***@proxy.example"),
                  ("socks5://user@proxy.example", "socks5://***@proxy.example"),
                  ("http://proxy.example:3128", "http://proxy.example:3128")):
    rc, d = show(LMAP_PROXY=raw)
    check(row(d, "proxy").get("value") == want, f"proxy shown as {want}")

cfgf.write_text('[limits]\ncpuu = 50\n')                      # Ctx rejects it: show still runs and says why
rc, d = show()
check(rc == 1 and "limits.cpuu" in d.get("unknown", []) and "error" in d and d.get("settings"),
      "a config lmap rejects is still shown, with the error")
cfgf.write_text('[net]\njobs = 0\n[build]\nqueue = "serial"\n')
rc, d = show()
check(row(d, "jobs").get("from") == "default" and row(d, "queue") == {"key": "queue", "value": "serial", "from": "config"},
      "origins follow how lmap reads each key (jobs = 0 -> default)")

rc, d = show(LMAP_QUALITY="original")
check(row(d, "quality") == {"key": "quality", "value": "original", "from": "env LMAP_QUALITY"}, "quality shown from env")
rc, d = show(LMAP_QUALITY="best")
check(rc == 1 and "quality" in d.get("error", ""), "a bad quality is reported")
rc, d = show(LMAP_QUALITY="original", LMAP_WEBP="no")
check(rc == 1 and "lossy" in d.get("error", ""), "quality = original with webp = no is refused")

cfgf.write_text('rooot = "/x"\n')
rc, d = show()
check(rc == 1 and d.get("unknown") == ["rooot"] and "typo" in d.get("hint", ""), "unknown key reported, exit 1")

cfgf.write_text('root = \n')
rc, d = show()
check(rc == 2 and "error" in d, f"broken config file: usage error ({rc})")

# [paths] / [net] / [build] sections; a setting outside its section is an error
cfgf.write_text(f'[paths]\nroot = "{tmp / "r5"}"\n[net]\njobs = 4\n[build]\nquality = "original"\n')
rc, d = show()
sec = {r["key"]: r.get("section") for r in d.get("settings", [])}
check(rc == 0 and row(d, "root") == {"key": "root", "value": str(tmp / "r5"), "from": "config"}
      and row(d, "jobs").get("value") == 4 and row(d, "quality").get("value") == "original",
      f"[paths] root, [net] jobs, [build] quality read ({d.get('error') or ''})")
check((sec.get("root"), sec.get("jobs"), sec.get("builds"), sec.get("proxy")) == ("paths", "net", "build", "net"),
      "each row names its section")
check("flat_keys" not in d and "unknown" not in d, "sections only: no flat-key note, nothing unknown")
(tmp / "r6").mkdir(); cfgf.write_text(f'root = "{tmp / "r6"}"\n[net]\njobs = 6\n')
r = lmap_run("list")
check(r.returncode == 2 and "[paths] root" in r.stderr, f"a setting outside its section: usage error naming it ({r.stderr[-160:]})")
rc, d = show()
check(row(d, "root").get("value") is not None and "section" in str(d.get("error")), "config show still lists it, with the error")
cfgf.write_text('[net]\njbos = 3\n')
r = lmap_run("--dry-run", "list"); rc, d = show()
check(r.returncode == 0 and "net.jbos" in d.get("unknown", []), f"a typo inside a section is reported, not fatal ({d.get('unknown')})")
cfgf.write_text('[net]\nroot = "/x"\n')
r = lmap_run("list")
check(r.returncode == 2 and "[paths]" in r.stderr, f"a key in the wrong section says where it belongs ({r.stderr[-160:]})")
rc, d = show()
check("[paths]" in d.get("hint", "") and "not a [net]" in str(d.get("error")), "config show keeps the error's hint")
cfgf.write_text('net = 3\n')
r = lmap_run("list")
check(r.returncode == 2 and "table" in r.stderr, "a section name used as a plain key: usage error")
cfgf.write_text('[limits]\ncpu = 50\n[net]\nbandwidth = "1MiB"\n')
rc, d = show()
check(row(d, "limits.cpu").get("value") == "50%" and row(d, "bandwidth").get("value") != "unlimited",
      "[limits] beside the new sections still read")

import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
