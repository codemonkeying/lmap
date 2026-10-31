"""All of lmap's source text, for tests that check how something is written (grep-style). bin/lmap plus every
module of the package beside it, in a fixed order, so these tests survive code moving between files."""
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def lmap_source():
    parts = [(HERE / "bin" / "lmap").read_text()]
    pkg = HERE / "lmaplib"
    if pkg.is_dir():
        parts += [p.read_text() for p in sorted(pkg.rglob("*.py"))]
    return "\n".join(parts)
