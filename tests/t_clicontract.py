#!/usr/bin/env python3
"""CLI contract (README "Every command takes --json..."), checked from the real parser + real runs in an EMPTY
data root: every subcommand has a help example that still parses, takes the global flags, rejects a bad verb with
exit 2 + one JSON error line, and every example that mutates (has --yes) refuses without --yes (exit 5, or 2/4
before that) and writes nothing; with --dry-run it writes nothing either. Usage: t_clicontract.py <scratch>"""
import argparse, atexit, concurrent.futures as cf, importlib.machinery, importlib.util, json, os, re, shlex, shutil
import subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
BIN = str(HERE / "bin" / "lmap")
T = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
atexit.register(shutil.rmtree, T, True)
ROOT = T / "root"; ROOT.mkdir()
USB = T / "usb"; USB.mkdir()                     # examples' /media/usb -> an empty dir of ours (never a real stick)
ENV = {**os.environ, "LMAP_ROOT": str(ROOT), "HOME": str(T / "home"), "XDG_CONFIG_HOME": str(T / "cfg"),
       "XDG_STATE_HOME": str(T / "xs"), "XDG_CACHE_HOME": str(T / "xc"), "XDG_DATA_HOME": str(T / "xd"),
       "LMAP_SCOPE": "1",                        # no systemd scope: leave the user's real lmap.slice caps alone
       "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",   # a slipped fetch fails, offline
       "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}
os.environ.update({k: ENV[k] for k in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "HOME")})
# commands allowed to answer "nothing to do" ({"ok": true, "unchanged": true}) without --yes on an empty root
NOOP_OK = {"job stop", "queue run", "queue stop", "pyramid fix", "cache prune", "data rm"}
ld = importlib.machinery.SourceFileLoader("lmap", BIN)
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = 0


def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += not c


def subs(p):
    return next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)


def leaves(p, path=()):
    s = subs(p)
    if s is None:
        yield path, p; return
    for name, sp in s.choices.items():
        yield from leaves(sp, path + (name,))


P = L.build_parser()
LEAVES = list(leaves(P))
ok(len(LEAVES) > 30, f"{len(LEAVES)} subcommands found")


def cmds(text):
    """every `lmap ...` command in a help example (examples chain with &&, add notes after # or in (...))"""
    return [shlex.split(m) for m in re.findall(r"\blmap((?: +[^\s#&;()]+)+)", text)]


def parses(argv):
    try:
        P.parse_args(argv); return True
    except SystemExit:
        return False


# --- static: help example present, every example parses, global flags accepted after the subcommand
# a leaf whose help mentions --yes anywhere (incl. "# add --yes to delete ...") is mutating: its own example is run
mutating = []
for path, sp in LEAVES:
    name = " ".join(path)
    ex = cmds(sp.epilog or "")
    if not ex:
        ok(False, f"{name}: help has an example"); continue
    bad = [c for c in ex if not parses(c)]
    ok(not bad, f"{name}: examples parse ({len(ex)})" + (f" BAD {bad}" if bad else ""))
    own = [c for c in ex if tuple(c[:len(path)]) == path and parses(c)]
    for c in own:
        ok(parses(c + ["--json", "--dry-run", "-y", "-v", "-q"]), f"{' '.join(c)}: global flags after it")
    if "--yes" in (sp.description or "") + (sp.epilog or ""):     # not the global flag list every leaf has
        ok(bool(own), f"{name}: mutating, has a runnable example of its own")
        seen = set()
        for c in own:
            k = tuple(a for a in c if a not in ("--yes", "-y", "--dry-run"))
            if k not in seen:
                seen.add(k); mutating.append((name, [str(USB) if a == "/media/usb" else a for a in k]))

top = P.format_help()
ok(all(c in top for c in ("0 ok", "1 failure", "2 bad usage", "4 not found", "5 refused", "6 upstream")),
   "exit codes listed in lmap --help")


def run(argv):   # a refusal/plan is quick: 60 s = hung (reported as rc -9, a FAIL, not a crash of the test)
    try:
        r = subprocess.run([BIN, "--json", *argv], capture_output=True, text=True, timeout=60, env=ENV,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -9, "", "timeout 60 s"


def json_err(err):
    lines = [l for l in err.splitlines() if l.startswith("{")]
    try:
        return bool(lines) and "error" in json.loads(lines[-1])
    except ValueError:
        return False


def as_json(out):
    try:
        d = json.loads(out)
        return d if isinstance(d, dict) else {"_list": d}
    except ValueError:
        return None


def tree():   # everything a command could write: the data root, the fake USB, HOME, config, data dirs
    return sorted(str(p.relative_to(T)) for d in (ROOT, USB, T / "home", T / "cfg", T / "xd") if d.exists()
                  for p in d.rglob("*"))


# --- runtime (empty root, no stdin): bad verb/noun = 2 + JSON error, nothing on stdout
nouns = sorted({p[0] for p, _ in LEAVES if len(p) > 1}) + ["nosuchnoun"]
with cf.ThreadPoolExecutor(4) as ex_:
    for n, (c, out, err) in zip(nouns, ex_.map(lambda n: run([n, "nosuchverb"]), nouns)):
        ok(c == 2 and not out.strip() and json_err(err), f"{n} nosuchverb: exit 2, JSON error, clean stdout (rc {c})")

# --- runtime: every mutating command's example, WITHOUT --yes, refuses; with --dry-run it plans; nothing written
before = tree()
jobs = [(n, c, False) for n, c in mutating] + [(n, c + ["--dry-run"], True) for n, c in mutating]
with cf.ThreadPoolExecutor(4) as ex_:
    res = list(ex_.map(lambda j: run(j[1]), jobs))
for (n, argv, dry), (c, out, err) in zip(jobs, res):
    d = as_json(out) if c == 0 else None
    if dry:   # rc 0 must SAY it's a plan (dry_run) or nothing to do; else a JSON usage/not-found error
        good = (d is not None and (d.get("dry_run") is True or d.get("unchanged") is True)) or \
               (c in (2, 4) and json_err(err) and not out.strip())
        ok(good, f"{' '.join(argv)}: dry-run = JSON plan marked dry_run, or a JSON error (rc {c})")
    else:     # "nothing to do" only for the listed commands; everything else must refuse
        noop = n in NOOP_OK and d is not None and d.get("unchanged") is True
        ok(noop or (c in (2, 4, 5) and json_err(err) and not out.strip()),
           f"{' '.join(argv)}: no --yes = refused with a JSON error" + (" or no-op" if n in NOOP_OK else "") + f" (rc {c})")
after = tree()
ok(after == before, f"nothing written after {len(jobs)} runs" + (f": {sorted(set(after) - set(before))[:5]}" if after != before else ""))

import warnings                                       # GDAL >= 3.9 nags on every call unless lmap picks a mode
with warnings.catch_warnings(record=True) as w_:
    warnings.simplefilter("always"); from osgeo import gdal as _g; _g.OpenEx(str(T / "nope.gpkg"))
ok(not [x for x in w_ if "UseExceptions" in str(x.message)], "no GDAL 'UseExceptions' FutureWarning")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
