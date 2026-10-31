#!/usr/bin/env python3
"""devcat: write a small DEV catalog from sources.toml, every source clipped to two small test boxes, so the
whole tool (every layer, contour type, download kind, areas, maps) can be exercised in minutes, not days.

  python3 tools/devcat.py [OUT] [--catalog sources.toml] [--keep-huge]
  LMAP_CATALOG=OUT LMAP_ROOT=/some/dev/root bin/lmap data fetch <id> --dry-run

Boxes: TSV (a coastal city test box, QLD) and BM (a Blue Mountains test box, NSW). A source is kept for
the boxes it overlaps (QLD -> TSV, NSW -> BM, Australia-wide -> both as bboxes) and dropped if it overlaps
neither (the Moon, other states). Copernicus -> just the 1-degree tiles needed (tiles = [[lat, lon], ...]);
url file lists named by tile (esa-worldcover S21E144_...) or by state (OsmAnd) -> only the files covering the
boxes. Whole-file downloads that can't be clipped are kept as-is and listed. Stdlib only."""
import argparse
import json
import math
import re
import sys
import tomllib
from pathlib import Path

BOXES = {"TSV": [146.74, -19.32, 146.88, -19.22],      # coastal city test box, QLD
         "BM": [150.25, -33.76, 150.42, -33.62]}       # Blue Mountains test box, NSW
BOX_STATE = {"TSV": "queensland", "BM": "new-south-wales"}     # OsmAnd-style per-state file names
OFF_EARTH = re.compile(r"moon|lunar|lola|lroc|mars", re.I)
UNCLIPPABLE_SRC_GB = {"tcc-aerial": 394}               # downloads whole-city source tiles (catalog: ~394 GB)
TILE_NAME = re.compile(r"(?<![A-Za-z])([NS])(\d\d)([EW])(\d{3})")


def inter(a, b):
    w, s, e, n = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return [w, s, e, n] if w < e and s < n else None


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def boxes_of(s):
    """The source's lon/lat footprint as a list of boxes ([] = unknown / not a place)."""
    if s.get("bbox"):
        return [list(s["bbox"])]
    if s.get("bboxes"):
        return [list(b) for b in s["bboxes"]]
    if s.get("kind") == "copernicus":
        if s.get("tiles"):
            return [[lo, -la, lo + 1, -la + 1] for la, lo in s["tiles"]]
        if s.get("lat") and s.get("lon"):
            return [[s["lon"][0], -s["lat"][1], s["lon"][1] + 1, -s["lat"][0] + 1]]
    return []


def cop_cells(boxes):
    out = []
    for w, so, e, n in boxes:
        for la in range(math.floor(so), math.ceil(n)):
            for lo in range(math.floor(w), math.ceil(e)):
                out.append([-la, lo])
    return [list(x) for x in dict.fromkeys(tuple(c) for c in out)]


def file_tile(name, deg):
    """Box of a tile-named file (S21E144 = lower-left corner, deg x deg), or None."""
    m = TILE_NAME.search(name)
    if not m:
        return None
    la = int(m.group(2)) * (-1 if m.group(1) == "S" else 1)
    lo = int(m.group(4)) * (-1 if m.group(3) == "W" else 1)
    return [lo, la, lo + deg, la + deg]


def wmst_units(b, step):
    eps = 1e-9
    return {(j, i) for j in range(math.floor(b[1] / step + eps), math.ceil(b[3] / step - eps))
            for i in range(math.floor(b[0] / step + eps), math.ceil(b[2] / step - eps))}


def clip(sid, s, keep_huge):
    """-> (new source or None, est_gb, note)."""
    kind = s.get("kind"); est = float(s.get("est_gb") or 0)
    text = " ".join(str(v) for v in [sid, s.get("title", "")] + list(s.get("urls") or []))
    if OFF_EARTH.search(text):
        return None, 0, "dropped: not on Earth"
    if kind in UNCLIPPABLE_SRC_GB:
        if not keep_huge:
            return None, 0, f"dropped: can't clip, downloads ~{UNCLIPPABLE_SRC_GB[kind]} GB of source (--keep-huge keeps it)"
        return dict(s), UNCLIPPABLE_SRC_GB[kind], f"WHOLE: can't clip, ~{UNCLIPPABLE_SRC_GB[kind]} GB download"
    if kind == "url" and s.get("files"):
        fs = s["files"]; deg = int(s.get("tile_deg", 3))
        tiles = [file_tile(f, deg) for f in fs]
        if all(tiles):                                          # tile-named files (ESA WorldCover 3x3 degree)
            keep = [f for f, t in zip(fs, tiles) if any(inter(t, b) for b in BOXES.values())]
            note = f"files {len(keep)}/{len(fs)} (tiles over the boxes)"
        else:                                                   # per-state files (OsmAnd)
            slugs = set(BOX_STATE.values())
            keep = [f for f in fs if any(re.search(rf"(?<![a-z-]){re.escape(g)}(?![a-z])", f.lower()) for g in slugs)]
            note = f"files {len(keep)}/{len(fs)} (state files for the boxes; whole-state download)"
        if not keep:
            return None, 0, "dropped: no file covers a box"
        return {**s, "files": keep}, est * len(keep) / len(fs), note
    src = boxes_of(s)
    if not src:
        if kind in ("url", "osm-clip", "file-clip"):
            if kind == "osm-clip":                              # reads the whole pbf, keeps only the boxes
                return {**s, "bboxes": [list(b) for b in BOXES.values()]}, 0.05, "clipped from the whole pbf"
            return dict(s), est, "WHOLE: whole-file download, can't clip"
        return dict(s), est if kind not in ("manual", "build-input") else 0, f"kept as-is ({kind}, no footprint)"
    hits = []                                                   # (box name, dev box inside the source)
    for name, b in BOXES.items():
        cut = next((c for c in (inter(b, x) for x in src) if c), None)
        if cut:
            hits.append((name, cut))
    if not hits:
        return None, 0, "dropped: overlaps neither box"
    new = {k: v for k, v in s.items() if k not in ("bbox", "bboxes", "statewide", "lat", "lon", "tiles")}
    boxes = [c for _, c in hits]
    names = "+".join(n for n, _ in hits)
    if kind == "copernicus":
        new["tiles"] = cop_cells(boxes)
        tot = sum(1 for la in range(s["lat"][0], s["lat"][1] + 1) for lo in range(s["lon"][0], s["lon"][1] + 1)) \
            if s.get("lat") else len(cop_cells(src))
        return new, est * len(new["tiles"]) / max(1, tot), f"{names}: tiles {new['tiles']}"
    if len(boxes) == 1:
        new["bbox"] = boxes[0]
    else:
        new["bboxes"] = boxes
    if kind == "wms-tiles":
        st = float(s.get("step", 1))
        mine = set().union(*(wmst_units(b, st) for b in boxes))
        tot = sum(len(wmst_units(b, st)) for b in src)
        return new, est * len(mine) / max(1, tot), f"{names}: {len(mine)} unit(s) of {st:g} deg"
    frac = sum(area(b) for b in boxes) / max(1e-12, sum(area(b) for b in src))
    return new, est * frac, names


def toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{toml_key(k)} = {toml_val(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def toml_table(path, d, out):
    out.append(f"[{'.'.join(toml_key(p) for p in path)}]")
    subs = []
    for k, v in d.items():
        if isinstance(v, dict):
            subs.append((k, v))
        else:
            out.append(f"{toml_key(k)} = {toml_val(v)}")
    out.append("")
    for k, v in subs:
        toml_table(path + [k], v, out)


def build(cat, keep_huge=False):
    """-> (dev sources {id: source}, rows [(id, kind, est_gb, note, kept)])."""
    srcs = cat.get("source", {}); dev = {}; rows = []
    for sid, s in srcs.items():
        new, gb, note = clip(sid, s, keep_huge)
        if new is not None:
            if "est_gb" in new:                                 # the dev size: scratch checks, ETAs, approx_gb
                new["est_gb"] = max(0.001, round(gb, 3))
            dev[sid] = new
        rows.append((sid, s.get("kind", "?"), gb, note, new is not None))
    return dev, rows


def main(argv=None):
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default="dev/sources-dev.toml", help="dev catalog to write (default dev/sources-dev.toml)")
    ap.add_argument("--catalog", default=str(here / "sources.toml"), help="input catalog (default: the repo's sources.toml)")
    ap.add_argument("--keep-huge", action="store_true", help="keep unclippable sources with a huge source download")
    a = ap.parse_args(argv)
    cat = tomllib.loads(Path(a.catalog).read_text())
    dev, rows = build(cat, a.keep_huge)
    lines = ["# DEV catalog written by tools/devcat.py from " + Path(a.catalog).name + " - do not edit, re-run it.",
             "# Boxes: " + ", ".join(f"{k} = {v}" for k, v in BOXES.items()), ""]
    for sid, s in dev.items():
        toml_table(["source", sid], s, lines)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True); out.write_text("\n".join(lines))
    tomllib.loads(out.read_text())                              # must read back
    print(f"{'id':32} {'kind':14} {'est GB':>8}  note")
    for sid, kind, gb, note, kept in rows:
        print(f"{sid:32} {kind:14} {gb:8.3f}  {note}" if kept else f"{sid:32} {kind:14} {'-':>8}  {note}")
    whole = [(sid, gb) for sid, _, gb, note, kept in rows if kept and note.startswith("WHOLE")]
    total = sum(gb for _, _, gb, _, kept in rows if kept)
    print(f"\nkept {len(dev)}/{len(rows)} sources -> {out}; est total {total:.2f} GB")
    if whole:
        print("whole-file downloads (not clipped): " + ", ".join(f"{i} ~{g:g} GB" for i, g in whole))
    return 0


if __name__ == "__main__":
    sys.exit(main())
