#!/bin/bash
# lmap test runner: tests/run.sh [--full] [--offline] [-j N] [t_name ...]
#   (default)  quick: every test except the slow ones below (a few minutes)
#   --full     every test, incl. the slow real-data builds (~15 min); run it before a release/commit
#   t_name ... just those tests (quick/full ignored)
#   -j N       parallel jobs (default 80% of cores, min 1); each test gets its own temp dir + XDG dirs
#   --offline  run every test with no network but loopback (unshare -rn, lo up): proves nothing needs the internet
#   --status [--json]  progress of a running suite (done/total, failed, running, elapsed, ETA from past test times), else the last result;
#              exit 0 = no failures, 1 = failures, 4 = no run found
# Output: one line per test "name rc=.. pass=.. fails=.. Ns" sorted by name, then a summary line.
# Env: LMAP_ROOT = a built data root (tests that need real data read it; nothing is written there).
#      LMAP_TEST_TMP = where per-test temp dirs go (default ~/.cache/lmap-tests; removed after each test).
SELF=$(realpath "$0")
cd "$(dirname "$SELF")/.." || exit 2
SCR=${LMAP_TEST_TMP:-${XDG_CACHE_HOME:-$HOME/.cache}/lmap-tests}
mkdir -p "$SCR" || exit 2
J=$(( $(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2) * 8 / 10 )); [ "$J" -lt 1 ] && J=1
SLOW="t_map t_osmorder t_image t_phonemap t_aucont t_golden"   # real-data map/area builds: minutes each
OFF=0; FULL=0; T=(); STATUS=0; JSON=0
while [ $# -gt 0 ]; do
  case $1 in
    --offline) OFF=1 ;;
    --full) FULL=1 ;;
    -h|--help) sed -n '2,12{s/^# \{0,1\}//;p}' "$SELF"; exit 0 ;;
    --status) STATUS=1 ;;
    --json) JSON=1 ;;
    -j) J=${2:?-j needs a number}; shift ;;
    -*) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    */*) T+=("$1") ;;
    *) T+=("tests/${1%.py}.py") ;;
  esac; shift
done
status() {   # a live suite = a tsuite dir whose runner pid is alive; none -> the last finished run
  local d live="" js
  for d in "$SCR"/tsuite.*; do
    [ -f "$d/meta" ] && kill -0 "$(sed -n 's/^pid=//p' "$d/meta")" 2>/dev/null && live=$d
  done
  if [ -z "$live" ]; then
    if [ ! -f "$SCR/last-run" ]; then
      [ $JSON = 1 ] && echo '{"ok": false, "running": false, "error": "no test run found"}' || echo "no test run found"
      return 4
    fi
    local sum; sum=$(grep '^SUMMARY' "$SCR/last-run"); local f; f=$(echo "$sum" | sed -n 's/.*failed=\([0-9]*\).*/\1/p')
    if [ $JSON = 1 ]; then
      python3 -c 'import json,sys; L=open(sys.argv[1]).read().splitlines(); s=[l for l in L if l.startswith("SUMMARY")]
f=[l.split()[0] for l in L if " rc=" in l and " rc=0 " not in l]
print(json.dumps({"ok": bool(s) and not f, "running": False, "failed_tests": f, "summary": s[-1] if s else None}))' "$SCR/last-run"
    else
      echo "not running; last run ($(date -r "$SCR/last-run" '+%F %H:%M')):"; grep ' rc=[1-9]' "$SCR/last-run"; echo "$sum"
    fi
    [ "${f:-1}" = 0 ]; return
  fi
  . "$live/meta"
  local done_ fails running el
  done_=$(ls "$live"/*.res 2>/dev/null | wc -l)
  fails=$(cat "$live"/*.res 2>/dev/null | grep ' rc=[1-9]' | cut -d' ' -f1 | paste -sd' ' -)
  running=$(for o in "$live"/*.out; do [ -e "$o" ] && [ ! -e "${o%.out}.res" ] && basename "$o" .out; done | paste -sd' ' -)
  el=$(( $(date +%s) - start ))
  local eta; eta=$(python3 - "$live" "$SCR/test-times.json" "${jobs:-$J}" <<'PY'
import json, pathlib, sys, time
d = pathlib.Path(sys.argv[1]); hp = pathlib.Path(sys.argv[2]); j = max(int(sys.argv[3] or 6), 1)
h = json.loads(hp.read_text()) if hp.is_file() else {}
meta = dict(l.split("=", 1) for l in (d / "meta").read_text().split() if "=" in l)
now = time.time()
started = {s.stem: int(s.read_text() or now) for s in d.glob("*.start")}
done = {r.stem for r in d.glob("*.res")}
run = [now - t0 for n, t0 in started.items() if n not in done]
runleft = [max(h.get(n, 60) - (now - started[n]), 0) for n in started if n not in done]
known = [h[n] for n in h]
tot = int(meta.get("total", 0)); waiting = max(tot - len(started), 0)
avg = sum(known) / len(known) if known else 60
if not h:
    print("estimating...")
else:
    left = max(runleft + [0]) if not waiting else (sum(runleft) + waiting * avg) / j
    left = max(left, max(runleft + [0]))
    print(f"eta {int(left // 60)}m{int(left % 60):02d}s" if left >= 5 else "finishing")
PY
)
  if [ $JSON = 1 ]; then
    python3 -c 'import json,sys; a=sys.argv; f=a[3].split(); print(json.dumps({"ok": not f, "running": True, "done": int(a[1]),
      "total": int(a[2]), "failed_tests": f, "running_tests": a[4].split(), "elapsed_s": int(a[5]), "full": a[6] == "1", "eta": a[7]}))' \
      "$done_" "$total" "$fails" "$running" "$el" "$full" "$eta"
  else
    echo "running: $done_/$total done, failed: ${fails:-none}, elapsed ${el}s, $eta, now: ${running:-starting}"
  fi
  [ -z "$fails" ]
}
if [ $STATUS = 1 ]; then status; exit; fi
if [ ${#T[@]} -eq 0 ]; then
  T=(tests/t_*.py)
  if [ $FULL = 0 ]; then
    q=(); for t in "${T[@]}"; do case " $SLOW " in *" $(basename "$t" .py) "*) ;; *) q+=("$t");; esac; done
    T=("${q[@]}"); echo "quick run: skipping $SLOW (--full runs them)" >&2
  fi
fi
if [ $OFF = 1 ] && ! unshare -rn true 2>/dev/null; then echo "--offline: unshare -rn not allowed here"; exit 2; fi
[ -n "$LMAP_ROOT" ] || echo "note: LMAP_ROOT unset - tests that need a built data root are skipped" >&2
OUT=$(mktemp -d $SCR/tsuite.XXXX); trap 'rm -rf "$OUT"' EXIT
printf 'pid=%s\ntotal=%s\nstart=%s\nfull=%s\njobs=%s\n' $$ ${#T[@]} "$(date +%s)" $FULL $J > "$OUT/meta"   # for --status
rm -f "$SCR"/failed-t_*.out                                   # only this run's failures

one() {   # $1 = test file
  local t=$1 n; n=$(basename "$t" .py)
  local D; D=$(mktemp -d $SCR/tt.XXXX)
  mkdir -p "$D/xs" "$D/xc"
  local s; s=$(date +%s)
  local cmd="timeout 1200 python3 $t $D"
  # t_map/t_osmorder saturate a network-share data root (reads stall 10-20 s); t_image's dry runs on the same share then hit
  # its 280 s call timeout. t_image waits until they are done (exclusive vs their shared lock).
  case $n in t_map|t_osmorder) cmd="flock -s $OUT/nas.lock $cmd";; t_image|t_phonemap) cmd="flock -x $OUT/nas.lock $cmd";; esac
  local sc=""
  # in the namespace we're uid 0 with no user bus: systemd-run --user can't work, run unscoped
  if [ $OFF = 1 ]; then cmd="unshare -rn sh -c 'ip link set lo up && exec $cmd'"; sc=1; fi
  date +%s >"$OUT/$n.start"
  # the env MUST prefix the test command itself: temp files, state and cache all land in $D (removed below)
  LMAP_SCOPE=${sc:-$LMAP_SCOPE} XDG_STATE_HOME=$D/xs XDG_CACHE_HOME=$D/xc TMPDIR=$D \
    bash -c "$cmd" >"$OUT/$n.out" 2>&1
  local rc=$?
  # whole-test skip = printed SKIP, rc 0, no PASS/FAIL (a SKIP of one sub-check inside a running test isn't one)
  local sk=""
  [ $rc -eq 0 ] && ! grep -q '^PASS\|^FAIL' "$OUT/$n.out" && grep -q '^SKIP' "$OUT/$n.out" &&
    sk=" SKIPPED ($(grep -m1 '^SKIP' "$OUT/$n.out" | sed 's/^SKIP:\? *//'))"
  echo "$n rc=$rc pass=$(grep -c '^PASS' "$OUT/$n.out") fails=$(grep -c '^FAIL' "$OUT/$n.out") $(( $(date +%s) - s ))s$sk" >"$OUT/$n.res"
  [ $rc -ne 0 ] && grep '^FAIL\|Error\|Traceback' "$OUT/$n.out" | head -5 >>"$OUT/$n.res"
  [ $rc -ne 0 ] && cp "$OUT/$n.out" "$SCR/failed-$n.out"     # full output of a failure survives the suite cleanup
  rm -rf "$D"
}
export -f one; export OUT OFF SCR

# slowest first so the long ones don't finish last
order=$(for t in "${T[@]}"; do case $(basename "$t") in
  t_review.py|t_osmorder.py|t_map.py) echo "0 $t";; t_image.py) echo "1 $t";; t_sheets.py|t_water.py|t_lidar.py|t_export.py|t_queue.py) echo "1 $t";; *) echo "2 $t";; esac; done | sort | cut -d' ' -f2)
s0=$(date +%s)
printf '%s\n' $order | xargs -P "$J" -I{} bash -c 'one {}'
cat "$OUT"/*.res > "$OUT/all"; cat "$OUT/all"
bad=$(cat "$OUT"/*.res | grep -c ' rc=[1-9]')
if [ "$bad" != 0 ]; then               # full output of each failed test survives the clean-up
  keep=$(mktemp -d $SCR/tfail.XXXX)
  for r in "$OUT"/*.res; do grep -q ' rc=[1-9]' "$r" && cp "${r%.res}.out" "$keep/"; done
  echo "failed tests' full output: $keep"
fi
nsk=$(grep -l ' SKIPPED' "$OUT"/*.res 2>/dev/null | wc -l)
echo "SUMMARY tests=${#T[@]} failed=$bad skipped=$nsk jobs=$J offline=$OFF full=$FULL $(( $(date +%s) - s0 ))s" | tee -a "$OUT/all"
cp "$OUT/all" "$SCR/last-run.tmp" && mv "$SCR/last-run.tmp" "$SCR/last-run"
python3 - "$SCR/test-times.json" "$OUT" <<'PY' || true     # per-test seconds (smoothed): --status ETA
import json, pathlib, re, sys
p = pathlib.Path(sys.argv[1]); h = json.loads(p.read_text()) if p.is_file() else {}
for r in pathlib.Path(sys.argv[2]).glob("*.res"):
    m = re.match(r"(\S+) rc=0 .* (\d+)s", r.read_text().splitlines()[0])
    if m:
        s = int(m.group(2)); h[m.group(1)] = round(s if m.group(1) not in h else 0.5 * h[m.group(1)] + 0.5 * s)
p.write_text(json.dumps(h))
PY
[ "$bad" = 0 ]
