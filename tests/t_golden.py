#!/usr/bin/env python3
"""Characterisation test for the package split (phase 1): read-only answers for EVERY catalog source must not change
while code moves. The reference lmap (git GOLDEN_REF, default the 1.0.0 commit) and this tree are run side by side on
the same library, so live data (sizes, tile counts) can't make it flaky. Compared: status_of, detail_of (minus live job fields), outdated_of for offline kinds, scratch_need,
clean_stale patterns (dry), area_plan for each level on a fixed box, refresh_plan step ids/deps, the CLI parser tree,
and the sha1 of every embedded script (they feed build signatures). Needs LMAP_ROOT (real library) - skips when absent.
usage: t_golden.py TMPDIR            (internal: t_golden.py --snap LMAP_PATH OUT.json)"""
import argparse, hashlib, importlib.machinery, importlib.util, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REF = os.environ.get("GOLDEN_REF", "c8eba70")   # lmap 1.0.0
if not os.environ.get("LMAP_ROOT") or not Path(os.environ["LMAP_ROOT"]).is_dir():
    print("SKIP no LMAP_ROOT"); sys.exit(0)
if sys.argv[1:2] != ["--snap"]:
    tmp = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp")
    ref = tmp / "lmap-ref"; ref.mkdir(exist_ok=True)       # whole tree: lmap finds share/ next to itself
    import io, tarfile                                     # (no shell: paths with spaces / quotes are fine)
    tar = subprocess.run(["git", "-C", str(HERE), "archive", REF, "bin", "lmaplib", "share"], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(ref, filter="data")
    env = {**os.environ, "LMAP_CATALOG": str(HERE / "sources.toml")}   # same catalog for both
    out = {}
    for tag, path in (("ref", ref / "bin" / "lmap"), ("cur", HERE / "bin" / "lmap")):
        r = subprocess.run([sys.executable, __file__, "--snap", str(path), str(tmp / f"g-{tag}.json")], env=env,
                           capture_output=True, text=True)
        if r.returncode:
            print(f"FAIL snapshot {tag}: {r.stderr[-800:]}"); sys.exit(1)
        out[tag] = json.loads((tmp / f"g-{tag}.json").read_text())
    old, cur = out["ref"], out["cur"]
    fails = 0
else:
    old = cur = None
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


VOLATILE = {"running", "pid", "log", "age_s", "elapsed_s", "job", "updated", "started", "eta_s", "rate"}


def clean(v):
    if isinstance(v, dict):
        return {k: clean(x) for k, x in sorted(v.items()) if k not in VOLATILE}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    if callable(v):
        return getattr(v, "__name__", "fn")
    return v


def safe(f, *a):
    try:
        return clean(f(*a))
    except Exception as x:
        return {"raises": type(x).__name__, "msg": str(getattr(x, "msg", x))[:120]}


def code_only(src):
    """sha1 of a script's tokens minus comments / blank lines (independent of lmap's own helper, so the
    reference release needs no such function)."""
    import io, tokenize
    toks = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.NL)]
    return hashlib.sha1("\x00".join(toks).encode()).hexdigest()


def snapshot(lpath):
    ld = importlib.machinery.SourceFileLoader("lmap", lpath)
    L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
    a = argparse.Namespace(root=None, catalog=None, json=True, dry_run=True, yes=False, verbose=False, quiet=True,
                           refresh=False, plan=None)
    ctx = L.Ctx(a); ctx.log = lambda *x, **k: None
    snap = {"sources": {}, "embedded": {}, "area_plan": {}, "refresh_plan": None, "cli": None}
    for sid, s in sorted(ctx.sources.items()):
        s = ctx.src(sid) if not s.get("template") else dict(s, id=sid)
        r = {"kind": s.get("kind"), "status": safe(L.status_of, ctx, s), "scratch_need": safe(L.scratch_need, ctx, s)}
        if not s.get("template"):
            r["detail"] = safe(L.detail_of, ctx, s)
        if s.get("kind") in ("osm-clip",) or s.get("kind") not in ("url", "arcgis"):
            r["outdated"] = safe(L.outdated_of, ctx, s)          # offline answers only (url/arcgis ask a server)
        snap["sources"][sid] = r
    for k in sorted(vars(L)):
        v = getattr(L, k)
        if k.isupper() and isinstance(v, str) and len(v) > 2000 and ("import " in v or "def " in v):
            snap["embedded"][k] = code_only(v)                   # comments may change, code may not
    snap["item_code"] = {k: safe(L._item_code, k) for k in ("peaks", "hls", "field", "contours-lidar", "contours-best", "contours", "dem3d")}
    box = (146.70, -19.35, 146.85, -19.20)                         # coastal city test box: covered by every level's inputs
    for lvl in sorted(L.AREA_LEVELS):
        snap["area_plan"][lvl] = safe(lambda: [(k, kd, (sr or {}).get("id") if isinstance(sr, dict) else sr, sorted((ex or {}).keys()) if isinstance(ex, dict) else ex)
                                               for k, kd, sr, ex in L.area_plan(ctx, box, lvl)])
    snap["refresh_plan"] = safe(lambda: [(i, sorted(d), lab) for i, _, d, lab in L.refresh_plan(ctx)])


    def tree(p):
        out = {}
        for ac in p._actions:
            if isinstance(ac, argparse._SubParsersAction):
                out["_sub"] = {n: tree(sp) for n, sp in sorted(ac.choices.items())}
            else:
                out[",".join(ac.option_strings) or ac.dest] = [ac.dest, str(ac.default) if not callable(ac.default) else "fn",
                                                              sorted(map(str, ac.choices)) if ac.choices else None]
        return out


    try:
        snap["cli"] = tree(L.build_parser())
    except AttributeError:
        snap["cli"] = "no build_parser"
    return snap


if old is None:
    home = str(Path(sys.argv[2]).resolve().parent.parent)      # the program's own dir -> <HOME> (ref vs tree)
    Path(sys.argv[3]).write_text(json.dumps(snapshot(sys.argv[2]), default=str, sort_keys=True).replace(home, "<HOME>"))
    sys.exit(0)
print(f"PASS compared {len(cur['sources'])} sources, {len(cur['embedded'])} embedded scripts against {REF}")


def diff(x, y, path=""):
    if type(x) is not type(y):
        return [f"{path}: {str(x)[:100]} -> {str(y)[:100]}"]
    if isinstance(x, dict):
        o = []
        for k in sorted(set(x) | set(y)):
            if k not in x or k not in y:
                o.append(f"{path}/{k}: {'added' if k in y else 'removed'}")
            else:
                o += diff(x[k], y[k], f"{path}/{k}")
        return o
    if isinstance(x, list):
        if len(x) != len(y):
            return [f"{path}: len {len(x)} -> {len(y)}"]
        return [d for i, (p, q) in enumerate(zip(x, y)) for d in diff(p, q, f"{path}[{i}]")]
    return [] if x == y else [f"{path}: {str(x)[:100]} -> {str(y)[:100]}"]


# Deliberate changes since REF, each named with why. Anything else that differs still fails.
INTENDED = {
    "embedded/AREA_QGIS": "contour widths from contour_widths() (0.12 mm, index 2x)",
    "embedded/PRINT_PY": "INCOMPLETE stamp when a layer the style needs is missing (98c)",
    "embedded/TOPO_PY": "contour widths from contour_widths(); photo contours at topo width; water drawn over contours",
    "cli/_sub/map/--contour-mm": "contour width setting (global default, per-map override)",
    "cli/_sub/map/--index-ratio": "index contour = ratio x contour width",
    "cli/_sub/area/_sub/print/--contour-mm": "contour width setting",
    "cli/_sub/area/_sub/print/--index-ratio": "index ratio setting",
    "cli/_sub/refresh/--resume": "refresh --resume: skip steps the last refresh finished",
    "cli/_sub/job/_sub/log": "job log <id|PID> (run registry)",
    "cli/_sub/job/_sub/start/--refresh": "job start --refresh",
    "item_code/contours-best": "one contour provider per area (--contours-from, NSW 5 m model)",
    "cli/_sub/map/--contours-from": "contour provider override",
    "cli/_sub/area/_sub/create/--contours-from": "contour provider override",
    "area_plan/local": "sat-even item (photo sheets: sea evened)",
    "area_plan/regional": "sat-even item",
    "area_plan/street": "sat-even item",
    "cli/_sub/cache": "cache list/rm/prune/pin/unpin",
    "cli/_sub/data/_sub/rm": "data rm (needs --confirm ID)",
    "cli/_sub/export/_sub/bundle": "export bundle",
    "cli/_sub/import": "import add/list/rm",
    "cli/_sub/area/_sub/print/--overlay": "photo overlay (default none = imagery + grid)",
    "cli/_sub/map/--overlay": "photo overlay",
    "embedded/IMAGE_PY": "contours under roads (PICK_PY), photo overlay, thin 1:50k streets",
    "sources/osm-water": "OSM water polygons (sea fill on topo prints)",
    "area_plan/overview": "area sea item (osm-water)",
    "embedded/PHONE_TILES_PY": "area phone-map tile renderer",
    "cli/_sub/area/_sub/phone-map": "area phone-map command",
}
INTENDED.update({f"area_plan/{s}[{i}][3]": "contours-best now depends on contours-nsw5"
                 for s, ii in (("local", (4, 23)), ("regional", (3, 18)), ("street", (4, 25))) for i in ii})
for part in ("sources", "embedded", "item_code", "area_plan", "refresh_plan", "cli"):
    d = diff(old.get(part), cur.get(part), part)
    def why(x):                          # an intended key covers its whole subtree (sources/osm-water/detail/...)
        p = x.split(": ", 1)[0]
        return next((INTENDED[k] for k in INTENDED if p == k or p.startswith((k + "/", k + "["))), None)
    ok_ = [x for x in d if why(x)]
    for x in ok_:
        print(f"NOTE intended: {x.split(': ', 1)[0]} ({why(x)})")
    d = [x for x in d if x not in ok_]
    check(not d, f"{part} unchanged" + ("" if not d else f": {len(d)} diffs\n  " + "\n  ".join(d[:15])))
sys.exit(1 if fails else 0)
