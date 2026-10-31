#!/usr/bin/env python3
"""lmap's dependency list (share/deps.tsv) as a tool. Usage:
  deps.py check [--all]     print what's missing; exit 1 if anything required is (--all: optional too)
  deps.py apt [--all|--optional]  the Debian/Ubuntu packages still missing, one line (empty = none)
  deps.py readme [--write]  the README "Deps" section; --write puts it between README's deps markers
Standard library only, so it runs before anything else is installed."""
import importlib.util, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = {"core": "core", "maps": "maps, prints, projects", "terrain": "terrain extras",
          "export": "export", "optional": "optional"}
BEGIN, END = "<!-- deps:begin (generated: python3 share/deps.py readme --write) -->", "<!-- deps:end -->"


def load():
    rows = []
    for i, line in enumerate((HERE / "deps.tsv").read_text().splitlines(), 1):
        if line.strip() and not line.startswith("#"):
            f = line.split("\t")
            if len(f) != 5 or f[0] not in ("cmd", "py") or f[3] not in GROUPS:
                sys.exit(f"share/deps.tsv:{i}: want 5 tab-separated fields kind(cmd|py) name apt group what: {line!r}")
            kind, name, apt, group, what = f
            rows.append({"kind": kind, "name": name, "apt": apt, "group": group, "what": what})
    return rows


def present(r):
    if r["kind"] == "cmd":
        return shutil.which(r["name"]) is not None
    try:
        return importlib.util.find_spec(r["name"]) is not None
    except (ImportError, ValueError):
        return False


def missing(all_=False, only_optional=False):
    return [r for r in load() if (all_ or only_optional or r["group"] != "optional")
            and (not only_optional or r["group"] == "optional") and not present(r)]


def apt_line(rows):
    return " ".join(sorted({r["apt"] for r in rows if r["apt"] != "-"}))


def readme():
    """one bullet per package; tools with no package (-) group under the row that carries the note"""
    rows, out = load(), []
    for g, title in GROUPS.items():
        items = []                                    # [head, [what...]]
        for r in (r for r in rows if r["group"] == g):
            what = [] if r["what"] == "-" else [r["what"]]
            if r["apt"] == "-":
                if what or not items or not items[-1][0].startswith("`") or items[-1][2] != "tools":
                    items.append([f"`{r['name']}`", what, "tools"])
                else:
                    items[-1][0] += f", `{r['name']}`"
                continue
            k = f"`{r['apt']}`"
            hit = next((x for x in items if x[0] == k), None)
            if hit is None:
                items.append([k, what, "pkg"])
            elif what and what[0] not in hit[1]:
                hit[1] += what
        out.append(f"- {title}:")
        out += [f"  - {h}" + (f": {'; '.join(w)}" if w else "") for h, w, _ in items]
    return "\n".join([BEGIN, *out, END])


def main(a):
    cmd, all_ = (a[0] if a else "check"), "--all" in a
    if sys.version_info < (3, 11):
        print("lmap needs python3 3.11+ (tomllib)", file=sys.stderr)
        return 1
    if cmd == "apt":
        print(apt_line(missing(all_, "--optional" in a)))
        return 0
    if cmd == "check":
        m = missing(all_)
        for r in m:
            print(f"missing [{r['group']}] {r['name']} ({r['apt']}): {r['what']}")
        return 1 if any(r["group"] != "optional" for r in m) else 0
    if cmd == "readme":
        text = readme()
        if "--write" not in a:
            print(text)
            return 0
        p = HERE.parent / "README.md"
        s = p.read_text()
        i, j = s.find(BEGIN), s.find(END)
        if i < 0 or j < 0:
            print("README.md has no deps markers", file=sys.stderr)
            return 1
        p.write_text(s[:i] + text + s[j + len(END):])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
