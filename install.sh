#!/bin/sh
# Symlink lmap into ~/.local/bin and install bash completion. Idempotent.
# Dependencies come from ONE list, share/deps.tsv:
#   ./install.sh               link + check; prints one `apt install` line for anything missing
#   ./install.sh --deps        also install the missing packages (apt, asks for sudo)
#   ./install.sh --deps --all  ... including the optional ones (osmium, ...)
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
deps=0; all=""
for a in "$@"; do
  case $a in
    --deps) deps=1 ;;
    --all) all=--all ;;
    -h|--help) sed -n '2,6p' "$0" | cut -c3-; exit 0 ;;
    *) echo "install.sh: unknown option $a (see --help)" >&2; exit 2 ;;
  esac
done
# python3 3.11+ first: everything below (deps.py, lmap itself) needs it; nothing is touched until it's there
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  echo "lmap needs python3 3.11+ (have: $(python3 -V 2>&1 || echo none)). e.g. sudo apt install python3" >&2
  exit 1
fi
python3 "$here/share/deps.py" apt >/dev/null       # a broken deps.tsv stops here, with file:line
mkdir -p "$HOME/.local/bin"
ln -sf "$here/bin/lmap" "$HOME/.local/bin/lmap"
comp="${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions"
mkdir -p "$comp"
ln -sf "$here/completions/lmap.bash" "$comp/lmap"
hasapt=0; command -v apt-get >/dev/null && hasapt=1
pk=$(python3 "$here/share/deps.py" apt $all)
if [ -n "$pk" ] && [ $deps = 1 ]; then
  if [ $hasapt = 1 ]; then
    s=sudo; [ "$(id -u)" = 0 ] && s=
    echo "installing: $pk"
    # shellcheck disable=SC2086
    if ! { $s apt-get update && $s apt-get install -y $pk; }; then
      echo "apt-get failed as a batch; trying one package at a time" >&2
      for p in $pk; do $s apt-get install -y "$p" || echo "  could not install $p" >&2; done
    fi
    pk=$(python3 "$here/share/deps.py" apt $all)
  fi
fi
python3 "$here/share/deps.py" check $all || true
if [ -n "$pk" ]; then
  if [ $hasapt = 1 ]; then
    echo "missing: sudo apt install $pk   (or: ./install.sh --deps)" >&2
  else
    echo "missing (Debian package names; install your distro's equivalents): $pk" >&2
  fi
fi
opt=$(python3 "$here/share/deps.py" apt --optional)
[ -n "$all" ] || [ -z "$opt" ] || echo "optional: $opt   (./install.sh --deps --all)" >&2
echo "installed: $HOME/.local/bin/lmap"
echo "completion: $comp/lmap  (new shells; or now: . $comp/lmap)"
