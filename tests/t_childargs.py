#!/usr/bin/env python3
"""Every a['k'] a QGIS child script reads is supplied by its parent (cf11b90: phone-map lacked 'mstyle' -> KeyError
on every real run, and no quick test saw it). Phone-map: the real args builder is called. Print and image: the
keys of the args dict literal in the parent function. Also: map_style() has every _STY['k'] the styling reads, and
a failed child is reported with its exit code + error line and its whole stderr kept in a log file."""
import ast, importlib.machinery, importlib.util, inspect, os, re, sys, tempfile, textwrap, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

HERE = Path(__file__).resolve().parent.parent
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="lmap-t-childargs-")     # child_fail logs go here, not ~
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)


def reads(script):
    """Keys read with a['k'] / a["k"], minus keys the script assigns itself."""
    got = set(re.findall(r"""\ba\[['"](\w+)['"]\]""", script))
    own = set(re.findall(r"""\ba\[['"](\w+)['"]\]\s*=[^=]""", script))
    return got - own


def literal_keys(fn, var="args"):
    """Keys the parent gives `var` in function fn: its dict literal plus later args["k"] = ... assignments."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    keys = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var for t in n.targets) \
                and isinstance(n.value, ast.Dict):
            keys = (keys or set()) | {k.value for k in n.value.keys if isinstance(k, ast.Constant)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == var \
                        and isinstance(t.slice, ast.Constant) and keys is not None:
                    keys.add(t.slice.value)
    return keys


PARENTS = {"PRINT_PY": "print_sheet", "IMAGE_PY": "cmd_area_image"}     # child script -> the function building its args


# phone-map: the real builder, both styles
a = types.SimpleNamespace(dpi=160)
for s in ("topo", "photo"):
    q = {"z": 15, "tiles": [[0, 0]], "styled_as": 25000}
    got = set(L.phone_tile_args(a, "p.qgz", s, q, "best", 10, "c.gpkg", "png", "o.mbtiles"))
    miss = reads(L.PHONE_TILES_PY) - got
    check(not miss, f"phone-map {s}: args supply every a['k'] PHONE_TILES_PY reads (missing {sorted(miss)})")

# print + image: the parent's args literal
for script in ("PRINT_PY", "IMAGE_PY"):
    f = getattr(L, PARENTS[script], None)
    check(f is not None and re.search(rf"\b{script}\b", inspect.getsource(f)) is not None, f"{script} is run by {PARENTS[script]}")
    keys = literal_keys(f) if f else None
    check(keys is not None, f"{script}: parent args dict found ({f.__name__ if f else None})")
    if keys is not None:
        miss = reads(getattr(L, script)) - keys
        check(not miss, f"{script}: {f.__name__} args supply every a['k'] it reads (missing {sorted(miss)})")

# the styling's _STY['k'] reads are all in map_style()
sty = set(re.findall(r"""_STY\[['"](\w+)['"]\]""", L.PRINT_PY + L.PHONE_TILES_PY))
miss = sty - set(L.map_style())
check(bool(sty) and not miss, f"map_style() has every _STY key the styling reads ({len(sty)}; missing {sorted(miss)})")


# a failed child: exit code + the real error line in the message, whole stderr in a log file
class Ctx:
    def __init__(self): self.v = []
    def vlog(self, m): self.v.append(m)
    def log(self, *x): pass


c = Ctx()
err = ["Traceback (most recent call last):\n", '  File "<string>", line 9, in <module>\n',
       "AttributeError: 'QgsVectorLayerSimpleLabeling' object has no attribute 'rootRule'\n",
       "QGIS: some unrelated warning\n"]
try:
    L.child_fail(c, "phone map topo z15", 1, err); e = None
except L.CliError as x:
    e = x
check(e is not None and "exit code 1" in e.msg and "AttributeError: 'QgsVectorLayerSimpleLabeling'" in e.msg,
      f"message names exit code + the exception line, not the trailing warning: {getattr(e, 'msg', None)!r}")
lp = Path(re.sub(r"^full error log: ", "", (e.hint or ""))) if e else None
check(lp is not None and lp.is_file() and 'line 9, in <module>' in lp.read_text(), f"whole stderr kept in {lp}")
check(any("exit code 1" in m and "Traceback" in m for m in c.v), "-v shows a headed stderr dump")
try:
    L.child_fail(c, "image", -11, []); e = None
except L.CliError as x:
    e = x
check(e is not None and "exit code -11" in e.msg, f"no stderr (signal): exit code still named: {getattr(e, 'msg', None)!r}")
try:
    L.child_fail(c, "print", 0, ["QGIS: warning\n", "qgis._core.QgsProcessingException: bad layer\n", "QGIS: later\n"], "result=2")
    e = None
except L.CliError as x:
    e = x
check(e is not None and "exit code 0, result=2" in e.msg and "QgsProcessingException: bad layer" in e.msg,
      f"dotted exception found + reason named: {getattr(e, 'msg', None)!r}")
try:
    L.child_fail(c, "phone map topo z15", 0, ["QGIS: unrelated warning\n"], "no result line"); e = None
except L.CliError as x:
    e = x
check(e is not None and "exit code 0, no result line" in e.msg, f"rc 0, no output: reason named: {getattr(e, 'msg', None)!r}")
src = inspect.getsource(L.cmd_area_image) + inspect.getsource(L.cmd_area_phone_map)
check(len(re.findall(r"child_fail\([^\n]*None if rc else", src)) == 2, "image + phone-map pass a reason when the child exits 0")
for i in range(L.CHILD_LOGS_KEEP + 5):
    try:
        L.child_fail(c, f"x{i}", 1, ["E\n"])
    except L.CliError:
        pass
check(len(list(lp.parent.glob("*.log"))) <= L.CHILD_LOGS_KEEP, "old child logs pruned")

print("FAIL" if fails else "OK"); sys.exit(1 if fails else 0)
