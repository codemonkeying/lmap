#!/usr/bin/env python3
"""share/deps.tsv is the ONE dependency list: every external tool and non-stdlib module the code uses is in it,
install.sh reads it (no package list of its own), and
deps.py reports missing required/optional ones right. Usage: t_deps.py <scratch>"""
import ast, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
fails = 0


def ok(c, m):
    global fails
    print(("PASS " if c else "FAIL ") + m); fails += not c


sys.path.insert(0, str(HERE / "share"))
import deps  # noqa: E402
rows = deps.load()
cmds = {r["name"] for r in rows if r["kind"] == "cmd"}
mods = {r["name"] for r in rows if r["kind"] == "py"}
ok(all(r["group"] in deps.GROUPS for r in rows), "every row has a known group")

files = [HERE / "bin" / "lmap", *sorted((HERE / "lmaplib").glob("*.py")), *sorted((HERE / "share").glob("*.py"))]
src = {f: f.read_text() for f in files}

# tools: the program of a subprocess call / exec, a which() or need_tool() probe; GRASS's "--exec bash"
POSIX = {"sh", "stat"}                               # always there on any Linux
CALL = r"(?:run|Popen|check_output|check_call|call|run_q|_run|_check_cmd|execvp|which)"
used = set()
for t in src.values():
    used |= set(re.findall(CALL + r'\(\s*\[?\s*"([A-Za-z0-9_.+-]+)"', t))
    used |= set(re.findall(r'(?:^|[=(]\s*)\[\s*"([A-Za-z0-9_][A-Za-z0-9_.+-]*)"\s*,\s*"-', t, re.M))  # ["tool", "-x"
    for args in re.findall(r"need_tool\(([^)]*)\)", t):   # tools are the args; a conditional's test isn't
        used |= set(re.findall(r'(?:^|,|\belse)\s*"([^"]+)"', args)) | set(re.findall(r'"([^"]+)"\s+if\b', args))
    used |= set(re.findall(r'"--exec",\s*"([A-Za-z0-9_.+-]+)"', t))
used -= POSIX
ok(len(used) > 15, f"{len(used)} external tools found in the code")
ok(used <= cmds, "every tool the code runs is in deps.tsv" + (f": missing {sorted(used - cmds)}" if used - cmds else ""))
ok(not (cmds - used - {"python3"}), "no stale tool rows" + (f": {sorted(cmds - used - {'python3'})}" if cmds - used - {"python3"} else ""))

# modules: imports in the code AND in the Python scripts it carries as strings (run via sys.executable -c)
imp = set()
def scan(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imp.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            imp.add(n.module.split(".")[0])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and "import " in n.value and "\n" in n.value:   # embedded scripts, not one-line help/CLI words
            imp.update(m.split(".")[0] for m in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", n.value, re.M))
for f, t in src.items():
    try:
        scan(ast.parse(t))
    except SyntaxError as e:
        ok(False, f"{f.name} parses: {e}")
third = {m for m in imp if m not in sys.stdlib_module_names} - {"lmaplib", "lmap", "deps", "gpuwarp"}
ok("qgis" in third, "embedded QGIS scripts' imports are seen")
ok(third <= mods, f"every non-stdlib import is in deps.tsv ({sorted(third)})" +
   (f": missing {sorted(third - mods)}" if third - mods else ""))
ok(not (mods - third), f"no stale py rows ({sorted(mods - third)})")

# install.sh has no package list of its own
ins = (HERE / "install.sh").read_text()
own = [r["apt"] for r in rows if r["apt"] not in ("-", "python3", "bash") and re.search(r"(?<![\w-])" + re.escape(r["apt"]) + r"(?![\w-])", ins)]
ok("share/deps.py" in ins and not own, "install.sh reads deps.tsv only" + (f": names {own}" if own else ""))
h = T / "home"; h.mkdir()
r = subprocess.run(["sh", str(HERE / "install.sh"), "--bogus"], capture_output=True, text=True, env={"HOME": str(h), "PATH": "/usr/bin:/bin"})
ok(r.returncode == 2 and not (h / ".local").exists(), "install.sh: unknown option = 2, nothing done")

# the normal install path, on a copy whose list has a missing required package: links made, one apt line, exit 0
cp = T / "repo"; shutil.copytree(HERE, cp, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__"))
with open(cp / "share" / "deps.tsv", "a") as f:
    f.write("cmd\tnosuch-tool-zz\tnosuch-pkg\tcore\tx\n")
h2 = T / "home2"; h2.mkdir()
r = subprocess.run(["sh", str(cp / "install.sh")], capture_output=True, text=True, cwd="/",
                   env={"HOME": str(h2), "PATH": os.environ["PATH"]})
ok(r.returncode == 0 and (h2 / ".local/bin/lmap").is_symlink() and "nosuch-pkg" in r.stderr and "installed:" in r.stdout,
   f"install.sh (from /): linked, names the missing package, exit 0 (rc {r.returncode}) {r.stderr[-200:]}")
(cp / "share" / "deps.tsv").write_text((cp / "share" / "deps.tsv").read_text() + "bad row\n")
h3 = T / "home3"; h3.mkdir()
r = subprocess.run(["sh", str(cp / "install.sh")], capture_output=True, text=True, env={"HOME": str(h3), "PATH": os.environ["PATH"]})
ok(r.returncode != 0 and "deps.tsv:" in r.stderr and not (h3 / ".local").exists(), "broken deps.tsv: stops first, says where")

# missing detection on a fake list
fk = T / "share"; fk.mkdir()
shutil.copy(HERE / "share" / "deps.py", fk)
def fake(lines):
    (fk / "deps.tsv").write_text("\n".join("\t".join(x) for x in lines) + "\n")
    c = subprocess.run([sys.executable, str(fk / "deps.py"), "check"], capture_output=True, text=True)
    a = subprocess.run([sys.executable, str(fk / "deps.py"), "apt"], capture_output=True, text=True).stdout.split()
    o = subprocess.run([sys.executable, str(fk / "deps.py"), "apt", "--optional"], capture_output=True, text=True).stdout.split()
    return c.returncode, c.stdout, a, o
have = ("cmd", "sh", "dash", "core", "x")
c, out, a, o = fake([have, ("cmd", "nosuch-tool-zz", "nosuch-pkg", "core", "x"), ("py", "nosuch_mod_zz", "py-nosuch", "maps", "y"),
                     ("cmd", "nosuch-opt-zz", "opt-pkg", "optional", "z")])
ok(c == 1 and "nosuch-tool-zz" in out and "nosuch_mod_zz" in out, "check: missing required -> 1, named")
ok(a == ["nosuch-pkg", "py-nosuch"] and o == ["opt-pkg"], f"apt lists required / --optional apart ({a} {o})")
c, out, a, o = fake([have, ("cmd", "nosuch-opt-zz", "opt-pkg", "optional", "z")])
ok(c == 0 and a == [] and o == ["opt-pkg"], "only optional missing -> check 0")

shutil.rmtree(T, True)
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
