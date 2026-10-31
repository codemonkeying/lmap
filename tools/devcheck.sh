#!/usr/bin/env bash
# Quick dev check (~1-2 min): one small sheet of a dev box, then a PNG to look at.
# Only layers whose inputs or build code changed are rebuilt (each layer's .lmap-sig holds a hash of its code),
# so after editing one layer's code just that layer is redone.
#   tools/devcheck.sh [bm|tsv] [topo|photo|both] [scale]      e.g. tools/devcheck.sh bm photo 25k
# Needs the dev env: . ./.devenv && export LMAP_CATALOG=$PWD/dev/sources-dev.toml (area built once first).
# PNGs go to $LMAP_DEVCHECK_OUT (default ~/Downloads): devcheck-<box>-<style>.png
set -euo pipefail
cd "$(dirname "$0")/.."
box=${1:-bm}; style=${2:-topo}; scale=${3:-25k}
case $box in
  bm)  c=-33.69,150.335 ;;
  tsv) c=-19.27,146.81 ;;
  *) echo "box: bm or tsv" >&2; exit 2 ;;
esac
out=${LMAP_DEVCHECK_OUT:-$HOME/Downloads}; mkdir -p "$out"
s=$(date +%s)
bin/lmap --json map --name "devcheck-$box" --center="$c" --scale "$scale" --paper A6 --dpi 100 \
  --style "$style" --yes > /tmp/devcheck-$$.json
dir=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('dir',''))" /tmp/devcheck-$$.json)
rm -f /tmp/devcheck-$$.json
[ -d "$dir" ] || { echo "no map folder in lmap output" >&2; exit 1; }
for st in topo photo; do
  [ "$style" = both ] || [ "$style" = $st ] || continue
  pdftoppm -r 150 -png -singlefile "$dir/print-$st-$scale.pdf" "$out/devcheck-$box-$st"
  echo "$out/devcheck-$box-$st.png"
done
echo "devcheck $box $style $scale: $(( $(date +%s) - s ))s"
