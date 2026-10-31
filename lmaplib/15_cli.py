# lmap: cli. Loaded by bin/lmap into one shared namespace (see lmaplib/__init__.py).
# ---------------------------------------------------------------- output
def human(obj):
    if isinstance(obj, dict) and set(obj) == {"log", "lines"}:      # job log
        print("\n".join(obj["lines"])); print(f"({obj['log']})", file=sys.stderr); return
    if isinstance(obj, dict) and obj.get("action") == "config.show":
        print(f"config file  {obj['file']}" + ("" if obj["exists"] else "  (none: defaults)"))
        for r in obj["settings"]:
            print(f"  {r['key']:<16} {str(r['value']):<52} {r['from']}")
        if obj.get("error"):
            print(f"ERROR    lmap rejects this config: {obj['error']}")
        if obj.get("hint"):
            print(f"WARNING  {obj['hint']}")
        return
    if isinstance(obj, dict) and "detail" in obj:
        d = obj["detail"]
        print(f"{d['id']}  -  {d['title']}")
        prog = f"{d['have']}/{d['total']}" if d.get("total") is not None else "-"
        if d.get("no_data"):
            prog += f"  ({d['no_data']} no-data/ocean)"
        print(f"  state     {d['state']}   {prog}" + (f"   {d['bytes'] / 1e9:.2f} GB" if d.get("bytes") else ""))
        j = d.get("job")
        if j and j["running"]:
            rate = f"{j['rate_per_h']}/h" if j.get("rate_per_h") else "measuring..."
            print(f"  job       RUNNING pid {j['pid']}   {rate}   ETA {fmt_dur(j.get('eta_s'))}"
                  + (f" (~{j['eta_at']})" if j.get("eta_at") else ""))
            if "dl_slots" in j:
                print(f"  tuning    downloads {j['dl_active']}/{j['dl_slots']} (max {j['jobs_cap']}), "
                      f"shrinking {j['shrink_active']}/{j['shrink_cap']}, waiting {j['waiting']}, "
                      f"link {j.get('dl_mbps') or '?'} Mbit/s")
            print(f"  this run  {j['this_run_done']} done, {j['this_run_failed']} failed, {fmt_dur(j.get('elapsed_s'))} elapsed")
        elif j:
            print(f"  last job  {j.get('final') or 'stopped'}, {j.get('this_run_done', 0)} done, {j.get('this_run_failed', 0)} failed")
        print(f"  path      {d['dest']}" + ("   (vrt built)" if d.get("vrt") else ""))
        print(f"  commands  {d['use_with']}")
        if d.get("licence"):
            print(f"  licence   {d['licence']}")
        if j:
            print(f"  log       {j['log']}")
        return
    if isinstance(obj, dict) and "list" in obj:
        print(f"{'ID':<22} {'KIND':<11} {'STATE':<8} USE WITH")
        for r in obj["list"]:
            print(f"{r['id']:<22} {r['kind']:<11} {r['state']:<8} {r['use_with']}")
        print("\nexample: lmap job start <ID> --yes    details: lmap source get <ID>")
        return
    if isinstance(obj, dict) and "status" in obj:
        st = obj["status"]
        if st.get("share_reconnects"):
            print(f"Network share reconnects since boot: {st['share_reconnects']} (rising = the link is dropping; steps retry ~5 min)")
        if st.get("overall"):
            print(f"OVERALL  {st['overall']['text']}")
        print("RUNS (builds, fetches, refreshes; lmap job log|stop <PID>)")
        if not st.get("runs"):
            print("  (none in the last day)")
        for r in st.get("runs", []):
            print("  " + run_brief(r).strip())
        print("JOBS")
        if not st["jobs"]:
            print("  (none running)")
        for j in st["jobs"]:
            pct = 100 * j["have"] / j["total"] if j.get("total") else 0
            rate = f"{j['rate_per_h']}/h" if j.get("rate_per_h") else "measuring"
            eta = f"ETA {j['eta_at']}" if j.get("eta_at") else "no ETA yet"
            if j.get("stage"):
                eta = f"{j['stage'][:60]}" + (f" (since {j['stage_since']})" if j.get("stage_since") else "")
            fails = f"  {j['this_run_failed']} failed" if j.get("this_run_failed") else ""
            print(f"  {j['id']:<22} RUNNING  {j.get('have', '?')}/{j.get('total', '?')} ({pct:.0f}%)  {rate}  {eta}{fails}")
        if st.get("areas_not_current"):
            print("AREAS NOT CURRENT (re-run lmap area create <name> --yes)")
            for x in st["areas_not_current"]:
                print(f"  {x['area']:<16} {x['layer']:<12} {x['why'][:110]}")
        print("DATA")
        for d in st["data"]:
            prog = f"{d['have']}/{d['total']}" if d.get("total") is not None and d["state"] != "manual" else ""
            if d["state"] == "done" and d.get("no_data"):
                prog = f"{d['have']} tiles"
            gb = f"{d['bytes'] / 1e9:.2f} GB" if d.get("bytes") else ""
            print(f"  {d['id']:<22} {d['state']:<8} {prog:>11}  {gb:>9}")
        over = st.get("root_used_pct", 0) > st.get("disk_limit_pct", 100)
        print(f"\n{st['root']}  ({st['root_free_gb']} GB free, {st.get('root_used_pct')}% used"
              + (f": past the {st['disk_limit_pct']}% disk limit; lmap still writes here until 5 GB are left)" if over else ")"))
        return
    if isinstance(obj, dict) and isinstance(obj.get("jobs"), list):
        for r in obj.get("runs", []):
            print(run_brief(r).strip() + (f"\n  log: {r['log']}" if r.get("log") else ""))
        if not obj["jobs"]:
            print("(no download jobs)" if obj.get("runs") else "(no jobs)"); return
        for j in obj["jobs"]:
            state = f"RUNNING pid {j['pid']}" if j["running"] else (j["final"] if j.get("final") in ("done", "failed") else ("stopped" if j.get("final") == "stopped" else f"stopped ({j.get('final') or 'killed'})"))
            print(f"{j['id']}: {state}")
            if "total" in j:
                pct = 100 * j["have"] / j["total"] if j["total"] else 0
                print(f"  progress  {j['have']}/{j['total']} ({pct:.1f}%), {j['left']} left")
                if j["running"]:
                    rate = f"{j['rate_per_h']}/h" if j.get("rate_per_h") else "measuring..."
                    if j.get("stage"):
                        print(f"  stage     {j['stage']}" + (f" (since {j['stage_since']})" if j.get("stage_since") else "")
                              + "   ETA: when this step ends (no per-step estimate)")
                    else:
                        print(f"  rate      {rate}   ETA {fmt_dur(j.get('eta_s'))}" + (f" (~{j['eta_at']})" if j.get("eta_at") else ""))
                    if "dl_slots" in j:
                        print(f"  tuning    downloads {j['dl_active']}/{j['dl_slots']} (max {j['jobs_cap']}), "
                              f"shrinking {j['shrink_active']}/{j['shrink_cap']}, waiting {j['waiting']}, "
                              f"link {j.get('dl_mbps') or '?'} Mbit/s")
                print(f"  this run  {j['this_run_done']} done, {j['this_run_failed']} failed, {fmt_dur(j.get('elapsed_s'))} elapsed")
            print(f"  log       {j['log']}")
        return
    if isinstance(obj, dict) and "qld" in obj:
        print(f"{'ID':<30} {'STATE':<8} {'GB':>6}  PROGRESS")
        for r in obj["qld"]:
            prog = ", ".join(f"{k} {v:,}" for k, v in (r["layers"] or {}).items())
            print(f"{r['id']:<30} {r['state']:<8} {r['gb']:>6}  {prog}")
        return
    if isinstance(obj, list) and not obj:
        print("(none)"); return
    if isinstance(obj, list) and isinstance(obj[0], dict) and "pid" in obj[0]:
        for r in obj:
            print(f"{r['id']:<22} {'RUNNING pid ' + str(r['pid']) if r.get('running') else r.get('state') or 'stopped':<18} "
                  f"{r.get('last') or ''}")
            print(f"{'':<18} log: {r.get('log') or ''}")
        return
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        if "level" in obj[0]:                                  # area list
            print(f"{'NAME':<24} {'LEVEL':<9} {'KM2':>7} {'GB':>6}  MADE")
            for r in obj:
                print(f"{r['name']:<24} {r['level']:<9} {r['km2']:>7} {r['gb']:>6}  {r.get('made') or ''}")
                print(f"{'':<24} qgis {r['dir']}/{r['name']}.qgz")
            return
        if "title" in obj[0]:
            for r in obj:
                print(f"{r['id']:<22} {r['kind']:<11} ~{r.get('est_gb') or '?':>5} GB  {r['title']}")
            return
        for r in obj:
            have, tot = r.get("items_have"), r.get("items_total")
            prog = f"{have}/{tot}" if tot is not None else "-"
            gb = f"{r['bytes'] / 1e9:.2f} GB" if r.get("bytes") is not None else "-"
            flag = "RUNNING" if r["running"] else ("done" if r["complete"] else ("partial" if have else "missing"))
            if r["kind"] in ("manual", "build-input"):
                flag = "manual" if r["kind"] == "manual" else "build"
            print(f"{r['id']:<22} {flag:<8} {prog:>11}  {gb:>9}  {r['dest']}")
        return
    if isinstance(obj, dict) and obj.get("action") == "terrain.los" and not obj.get("dry_run"):
        human_los(obj); return
    if isinstance(obj, dict) and obj.get("action") in ("terrain.route", "terrain.bestroute") and not obj.get("dry_run"):
        human_route(obj); return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if v is not None:
                print(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}")
        return
    print(obj)


def human_route(o):
    km = lambda m: f"{m / 1000:.2f} km"
    print(f"{o['start']}  ->  {o['end']}" + ("   (best route: r.walk)" if "r_walk_h" in o else ""))
    if "r_walk_h" in o:
        print(f"  straight line {km(o['straight_m'])};  r.walk estimate {_hm(o['r_walk_h'])};  {o['res_m']:g} m grid, "
              f"roads {o['roads']}")
    print(f"  {km(o['distance_m'])} on the map, {km(o['ground_m'])} over the ground;  climb {o['climb_m']} m, "
          f"descent {o['descent_m']} m;  heights {o['low_m']:.0f} to {o['high_m']:.0f} m")
    print(f"  steepest {o['max_slope_deg']} deg (over ~20 m)" + (f";  {o['over_30deg_m']} m steeper than 30 deg: scramble"
                                                                if o["over_30deg_m"] else ""))
    print(f"  time: Naismith {_hm(o['naismith_h'])}   Tobler {_hm(o['tobler_h'])}"
          + ("  (off track)" if o["off_track"] else "  (on a track)"))
    if len(o["legs"]) == 1 and o["legs"][0].get("bearing"):
        b = o["legs"][0]["bearing"]
        print(f"  bearing {b['grid_mils']} mils grid" + (f" / {b['mag_mils']} mils mag ({b['mag_deg']} deg)"
                                                         if "mag_mils" in b else ""))
    if len(o["legs"]) > 1:
        w = max(len(l["from"]) + len(l["to"]) for l in o["legs"]) + 4
        print(f"  {'leg':<{w}} {'km':>6} {'mils':>5} {'mag':>5} {'up':>5} {'down':>5} {'Nais':>5} {'Tobl':>5}")
        for l in o["legs"]:
            b = l.get("bearing") or {}
            print(f"  {l['from'] + ' -> ' + l['to']:<{w}} {l['distance_m'] / 1000:>6.2f} {b.get('grid_mils', ''):>5} "
                  f"{b.get('mag_mils', ''):>5} {l['climb_m']:>5} {l['descent_m']:>5} {_hm(l['naismith_h']):>5} "
                  f"{_hm(l['tobler_h']):>5}")
    print(f"  terrain: {', '.join(f'{k} {v}%' for k, v in o['terrain'].items())}, sampled every {o['step_m']} m")
    for k in ("png", "gpkg"):
        if o.get(k):
            print(f"  {k}: {o[k]}")
    print(f"  NOTE {o['note']}")


def human_los(o):
    km = lambda m: f"{m / 1000:.2f} km" if m >= 1000 else f"{m} m"
    if o.get("mode") == "network":
        print(f"{o['visible_pairs']}/{o['pairs']} pairs can see each other  (k={o['refraction_k']}"
              + (f", radio {o['radio_mhz']:g} MHz" if o.get("radio_mhz") else "") + ")")
        w = max(len(p["name"]) for p in o["points"])
        for r in o["lines"]:
            if r.get("visible") is None:
                print(f"  {r['from']:<{w}} -> {r['to']:<{w}}  ERROR {r['error']}"); continue
            v = "SEES " if r["visible"] else "no   "
            extra = (f"blocked {km(r['blocked_by']['distance_m'])} along at {r['blocked_by']['mgrs']}" if r.get("blocked_by")
                     else f"clears ground by {r['clearance_m']} m" if r.get("clearance_m") is not None else "")
            rad = f"  radio {r['radio']['verdict']}" if r.get("radio") else ""
            print(f"  {r['from']:<{w}} -> {r['to']:<{w}}  {v} {km(r['distance_m']):>9}  {extra}{rad}")
        if o.get("gpkg"):
            print(f"layer: {o['gpkg']}")
    else:
        v = "VISIBLE" if o["visible"] else "NOT VISIBLE"
        print(f"{o['from']} {o['from_mgrs']}  ->  {o['to']} {o['to_mgrs']}")
        print(f"  {v}   {km(o['distance_m'])}   bearing {o['bearing']['grid_mils']} mils grid"
              + (f" / {o['bearing']['mag_mils']} mils mag ({o['bearing']['mag_deg']} deg)" if "mag_mils" in o["bearing"] else ""))
        print(f"  eye {o['eye_height_m']:g} m, target {o['target_height_m']:g} m;  ground {o['ends']['from_ground_m']} m"
              f" -> {o['ends']['to_ground_m']} m")
        if o.get("blocked_by"):
            b = o["blocked_by"]
            print(f"  blocked at {b['mgrs']}  ({km(b['distance_m'])} along, ground {b['ground_m']} m, "
                  f"{b['above_sightline_m']} m above the sight line)")
        elif o.get("clearance_m") is not None:
            print(f"  sight line clears the ground by {o['clearance_m']} m at the closest point")
        if o["visible"] and o["target_seen_m"] < o["target_height_m"]:
            print(f"  only the top {o['target_seen_m']} m of the target shows")
        if o.get("radio"):
            r = o["radio"]
            print(f"  radio {r['mhz']:g} MHz: {r['verdict'].upper()}  worst Fresnel clearance {r['fresnel_clear_pct_worst']}% "
                  f"(need 60%) at {r['worst_at']['mgrs']}" + (f", short by {r['short_of_60pct_m']} m" if r['short_of_60pct_m'] else ""))
        dg = o.get("dead_ground") or []
        print(f"  dead ground: {o['dead_ground_pct']}% of the line" + (":" if dg else ""))
        for g in dg[:8]:
            print(f"    {km(g['from_m'])} - {km(g['to_m'])}   {g['from_mgrs']} .. {g['to_mgrs']}")
        if len(dg) > 8:
            print(f"    ... {len(dg) - 8} more (--json)")
        print(f"  terrain: {', '.join(f'{k} {v}%' for k, v in o['terrain'].items())}, sampled every {o['step_m']} m")
        if o.get("png"):
            print(f"  profile: {o['png']}")
    print(f"  NOTE {o['note']}")


EXAMPLES = {
    "source list": "lmap source list --json",
    "source get": "lmap source get dem-au-90m",
    "data status": "lmap data status            # or: lmap data status aerial-tsv-2019 --json",
    "qld project": "lmap qld project --dry-run && lmap qld project --yes",
    "nsw project": "lmap nsw project --dry-run && lmap nsw project --yes",
    "au project": "lmap au project --dry-run && lmap au project --yes",
    "au grid": "lmap au grid --dry-run && lmap au grid --yes && lmap au project --yes",
    "au contours": "lmap au contours --dry-run && lmap au contours --yes && lmap au project --yes",
    "sheets index": "lmap sheets index --yes",
    "sheets get": "lmap sheets get --at '55K DU 79234 70557' --radius-km 100 --scale 250k --dry-run",
    "config show": "lmap config show",
    "moon project": "lmap moon project --dry-run && lmap moon project --yes",
    "data fetch": "lmap data fetch dem-au-90m --dry-run && lmap data fetch dem-au-90m --yes",
    "data outdated": "lmap data outdated   # then: lmap refresh --outdated --dry-run",
    "data hash": "lmap data hash --yes      # then: lmap data hash --check",
    "clean": "lmap clean --dry-run   # then: lmap clean --yes (add --resume to drop stopped jobs' progress too)",
    "data verify": "lmap data verify dem-au-90m   # add --yes to delete corrupt tiles",
    "job start": "lmap job start aerial-tsv-2019 --yes --jobs 10",
    "job stop": "lmap job stop aerial-tsv-2019 --yes        # pause; add --now to abort. A run: lmap job stop 12345 --yes",
    "job log": "lmap job log 12345          # a run's log (PID from lmap status); or a source id",
    "job list": "lmap job list --json",
    "job status": "lmap job status          # or: lmap job status aerial-tsv-2019 --json",
    "export create": "lmap export create /media/usb --format all --yes   # or --format copy|phone|atak, --bbox w,s,e,n",
    "export verify": "lmap export verify /media/usb",
    "status": "lmap status            # or: lmap status aerial-tsv-2019 --json",
    "list": "lmap list              # ids to use with fetch/job/verify/vrt",
    "vrt create": "lmap vrt create dem-au-90m --yes",
    "pyramid create": "lmap pyramid create aerial-tsv-2019 --mode cog --yes   # or --mode ovr",
    "pyramid fix": "lmap pyramid fix --dry-run   # list short pyramids; then: lmap pyramid fix --yes (or one: fix dem-se-aus-30m --yes)",
    "area create": "lmap area create magnetic-island --center=-19.14,146.84 --radius-km 6 --level street --yes  (add --3d / --aerial / --full for 3D terrain + aerial)",
    "area list": "lmap area list",
    "cache list": "lmap cache list --kind maps",
    "cache rm": "lmap cache rm old-sheet --yes   # --dry-run first; areas/NAME or maps/NAME if both exist",
    "cache prune": "lmap cache prune --older 30 --yes   # --dry-run first; pinned ones are kept",
    "cache pin": "lmap cache pin townsville --yes",
    "cache unpin": "lmap cache unpin townsville --yes",
    "data rm": "lmap data rm qld-fire-state --yes --confirm qld-fire-state   # --dry-run first",
    "import add": "lmap import add walk-route.gpx --as walk-route --yes   # or a .lmapbundle / ATAK .zip",
    "import list": "lmap import list",
    "import rm": "lmap import rm walk-route --yes --confirm walk-route --force   # --dry-run first",
    "export bundle": "lmap export bundle townsville-50k --to /media/usb --yes",
    "area print": "lmap area print test-paluma --scale 25000 --paper A3 --yes",
    "map": "lmap map --place Townsville --scale 50k --style both --yes",
    "verify": "lmap verify cmp-ours            # measure the newest print; --yes saves it for footers",
    "area image": "lmap area image townsville --res 2 --bbox=146.75,-19.3,146.85,-19.22 --jpeg --yes",
    "area phone-map": "lmap area phone-map townsville --style both --yes",
    "area export": "lmap area export test-paluma /media/usb --yes        # add --encrypt for an AES-256 zip",
    "area viewshed": "lmap area viewshed test-paluma --point=-19.0,146.2 --height 2 --label lookout",
    "terrain route": "lmap terrain route --points walk.txt --off-track --png walk.png",
    "terrain water": "lmap terrain water --from '55K DU 76962 61149' --radius-km 5 --yes",
    "terrain bestroute": "lmap terrain bestroute --from '55K DU 79234 70557' --to '55K DU 76962 61149' --gpkg best.gpkg",
    "terrain viewshed": "lmap terrain viewshed --from '55K DU 79234 70557' --name castle-hill --height 3 --yes",
    "terrain slope": "lmap terrain slope --dry-run && lmap terrain slope --yes && lmap au project --yes",
    "terrain los": "lmap terrain los --from '55K DU 81084 70615' --to '55K DU 83500 72100' --png los.png   "
                   "# or --points ops.txt --gpkg net.gpkg --yes; --radio 477",
    "qld fetch": "lmap qld fetch --tier essentials --dry-run && lmap qld fetch --tier essentials --yes",
    "qld status": "lmap qld status --tier everything",
    "queue add": "lmap queue add sat-qld-s2 qld-cadastre-state",
    "queue remove": "lmap queue remove qld-cadastre-state",
    "queue run": "lmap queue run --bandwidth 20MiB --detach --yes",
    "queue status": "lmap queue status",
    "queue stop": "lmap queue stop --yes",
}


class Parser(argparse.ArgumentParser):
    """Usage errors follow the contract: one JSON line on stderr with a runnable hint, exit 2.
    Subparsers inherit this class, so `lmap data list` says where `list` really lives."""
    def error(self, message):
        hint = f"run `{self.prog} --help`"
        m = re.search(r"invalid choice: '([^']+)'", message)
        if m:
            bad = m.group(1)
            top = ("status", "list", "source", "data", "job", "queue", "refresh", "clean", "export", "vrt", "pyramid", "area", "terrain", "qld", "au", "tides")
            if bad in top and self.prog != "lmap":
                hint = f"`{bad}` is a top-level command: run `lmap {bad} --help`"
            else:
                tail = message.split("choose from", 1)[-1].strip(" )")
                choices = [c.strip(" '") for c in tail.split(",") if c.strip(" '")]
                near = [c for c in choices if c.startswith(bad[:2]) or bad in c or c in bad
                        or sorted(c) == sorted(bad)]
                hint = (f"did you mean `{self.prog} {near[0]}`? " if near else "") + f"valid: {', '.join(choices)}"
        print(json.dumps({"error": f"{self.prog}: {message}", "hint": hint}), file=sys.stderr)
        sys.exit(E_USAGE)


def build_parser():
    def globals_(sub):
        # sub-level copies use SUPPRESS so flags given before the subcommand survive
        d = {"default": argparse.SUPPRESS} if sub else {}
        g = argparse.ArgumentParser(add_help=False)
        g.add_argument("--json", action="store_true", help="single JSON value on stdout", **d)
        g.add_argument("--dry-run", action="store_true", help="preview mutation as JSON, change nothing", **d)
        g.add_argument("-y", "--yes", action="store_true", help="confirm a mutating action", **d)
        g.add_argument("-v", "--verbose", action="store_true", help="diagnostics to stderr", **d)
        g.add_argument("-q", "--quiet", action="store_true", help="no progress chatter", **d)
        g.add_argument("--root", help=f"data root (else env LMAP_ROOT, else [paths] root = in {CONFIG}, else {DEFAULT_ROOT})", **d)
        g.add_argument("--catalog", help="sources.toml path (env LMAP_CATALOG; default: repo copy)", **d)
        return g
    g, gs = globals_(False), globals_(True)

    p = Parser(
        prog="lmap", parents=[g],
        description="Offline map data library manager. Downloads, resumes, resamples and indexes "
                    "the datasets listed in sources.toml into the data root.",
        epilog="exit codes: 0 ok, 1 failure, 2 bad usage, 4 not found, 5 refused (needs --yes), "
               "6 upstream/network error.\nfirst moves: lmap status ; lmap list (on disk) ; lmap source list (can fetch)\n"
               "downloading - pick one:\n"
               "  data fetch <id> --yes   one source, in the foreground (waits; can take hours)\n"
               "  job start <id> --yes    one source in the background; poll `lmap --json job status <id>`\n"
               "  queue add/run --yes     several sources at once, sharing bandwidth and per-server limits\n"
               "data root: --root, else $LMAP_ROOT, else [paths] root = in ~/.config/lmap/config.toml, else the default.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    nouns = p.add_subparsers(dest="noun", metavar="{status,list,source,data,job,queue,refresh,clean,export,vrt,pyramid,map,area,terrain,qld,au,tides}")
    for name, fn, h in (("status", cmd_status, "one screen: running jobs + every dataset (read-only)"),
                        ("list", cmd_list, "dataset ids you can pass to other commands, with state (read-only)")):
        sp = nouns.add_parser(name, parents=[gs], help=h, description=h, epilog="example: " + EXAMPLES[name])
        if name == "status":
            sp.add_argument("id", nargs="?", help="one source id for detail (see `lmap list`)")
        sp.set_defaults(fn=fn)

    def verb(noun_sp, name, fn, helptext, id_req=True, extra=None):
        sp = noun_sp.add_parser(name, parents=[gs], help=helptext, description=helptext,
                                epilog="example: " + EXAMPLES[f"{noun_sp._noun} {name}"])
        if id_req is not None:
            sp.add_argument("id", nargs=None if id_req else "?", help="source id (see `lmap source list`)")
        for a, kw in (extra or []):
            sp.add_argument(a, **kw)
        sp.set_defaults(fn=fn, **({} if any(a == "--refresh" for a, _ in extra or []) else {"refresh": False}))

    src = nouns.add_parser("source", help="the catalog of known datasets").add_subparsers(dest="verb", required=True)
    src._noun = "source"
    verb(src, "list", cmd_source_list, "list catalog sources", id_req=None)
    verb(src, "get", cmd_source_get, "show one source's catalog entry (urls, licence, bbox)")

    dat = nouns.add_parser("data", help="the downloaded files on disk").add_subparsers(dest="verb", required=True)
    dat._noun = "data"
    verb(dat, "status", cmd_data_status, "progress of every source (or one): have/total, size, running", id_req=False)
    verb(dat, "fetch", cmd_data_fetch,
         "download/resume a source; skips what's already there. Needs --yes. Safe to re-run after a crash",
         extra=[("--refresh", {"action": "store_true", "help": "re-list upstream / re-download single files"}),
                ("--tmp", {"help": f"scratch dir (env LMAP_TMP, config [paths] tmp; default {DEFAULT_TMP}). A local disk: big builds need tens of GB"}),
                ("--jobs", {"help": "MAX parallel downloads, 1-32 (env LMAP_JOBS, config [net] jobs; default 3). Auto-tuned below this"}),
                ("--shrink", {"help": "parallel resample processes, 1-32 (env LMAP_SHRINK, config [build] shrink; default cores/2, max 4)"}),
                ("--allow-missing", {"type": int, "default": 0, "metavar": "N",
                                     "help": "build the finished file even if up to N tiles keep failing (holes)"})])
    verb(dat, "outdated", cmd_data_outdated, "which stored sources have newer data upstream (cheap checks: HTTP "
         "date/size, ArcGIS counts + edit dates; nothing downloaded). Like apt list --upgradable", id_req=False)
    verb(dat, "hash", cmd_data_hash, "SHA256SUMS over the data root (or a folder in it); incremental. --check verifies",
         id_req=None, extra=[("sub", {"nargs": "?", "help": "folder under the root, e.g. qld or areas/test-paluma"}),
                             ("--check", {"action": "store_true", "help": "verify against the existing SHA256SUMS"})])
    verb(dat, "verify", cmd_data_verify, "decode-check every raster tile (catches truncated ones); --yes deletes corrupt ones",
         extra=[("--quick", {"action": "store_true", "help": "header-only check (old behaviour; misses truncation)"})])

    job = nouns.add_parser("job", help="background fetches: start, stop (pause), list").add_subparsers(dest="verb", required=True)
    job._noun = "job"
    verb(job, "start", cmd_job_start, "run `data fetch <id>` detached; logs to ~/.local/state/lmap/<id>.log. Idempotent",
         extra=[("--tmp", {"help": "scratch dir (see data fetch)"}),
                ("--refresh", {"action": "store_true", "help": "re-download even if complete (data fetch --refresh)"}),
                ("--jobs", {"help": "MAX parallel downloads, 1-32 (default 3); auto-tuned below this"}),
                ("--shrink", {"help": "parallel resample processes (default cores/2, max 4)"})])
    verb(job, "stop", cmd_job_stop, "pause: finish in-flight work then exit (resume = job start / re-run). --now aborts (SIGUSR1). "
         "ID = a source or a run PID (lmap status)",
         extra=[("--now", {"action": "store_true", "help": "abandon in-flight tiles immediately (cleaned up; re-fetched on resume)"}),
                ("--wait", {"type": int, "default": 0, "metavar": "SECS", "help": "block up to SECS for it to exit"})])
    verb(job, "status", cmd_job_status, "progress, rate, ETA of running (or last) fetches and runs; ID = a source or a run PID",
         id_req=False)
    verb(job, "log", cmd_job_log, "last lines of a fetch's or run's log; ID = a source or a run PID",
         extra=[("--lines", {"type": int, "default": 40, "help": "how many (default 40)"})])
    verb(job, "list", cmd_job_list, "running fetches and runs (pid, log, last log line)", id_req=None,
         extra=[("--all", {"action": "store_true", "help": "include stopped jobs that have a log"})])

    exp = nouns.add_parser("export", help="offline copies: USB mirror, phone MBTiles, ATAK").add_subparsers(dest="verb", required=True)
    exp._noun = "export"
    verb(exp, "create", cmd_export_create,
         "write <target>/lmap-export/: copy (mirror + tool), phone (MBTiles), atak (DTED/imagery/KML + data package). "
         "Re-runs are incremental", id_req=None,
         extra=[("target", {"help": "directory to export into, e.g. the mounted USB"}),
                ("--format", {"default": "copy", "help": "comma list: copy, phone, atak, all (default copy)"}),
                ("--bbox", {"help": "w,s,e,n degrees: the area for phone/atak (required for those)"}),
                ("--jpeg", {"action": "store_true",
                            "help": "copy: re-encode WEBP photo images as JPEG q85 (for readers without WEBP; "
                                    "slight extra loss, slower)"}),
                ("--encrypt", {"action": "store_true",
                               "help": "pack each part into an AES-256 zip (passphrase asked twice, or "
                                       "LMAP_PASSPHRASE_FILE). Opens with 7-Zip/Windows 11/ZArchiver (Android)"})])
    verb(exp, "verify", cmd_export_verify, "re-hash an export against its SHA256SUMS", id_req=None,
         extra=[("target", {"help": "export dir (or its parent)"})])

    vrt = nouns.add_parser("vrt", help="mosaic index files (.vrt) over tile folders").add_subparsers(dest="verb", required=True)
    vrt._noun = "vrt"
    verb(vrt, "create", cmd_vrt_create, "build/rebuild the source's .vrt from its tiles (--yes to overwrite)")

    pyr = nouns.add_parser("pyramid", help="zoomed-out copies (overviews) so big mosaics open fast").add_subparsers(dest="verb", required=True)
    pyr._noun = "pyramid"
    verb(pyr, "create", cmd_pyramid_create,
         "build overviews over the source's .vrt. ovr = <vrt>.ovr sidecar (quick, small); "
         "cog = one Cloud-Optimised GeoTIFF <name>.cog.tif (fastest to browse, ~size of the data again). Long: run under nohup",
         extra=[("--mode", {"choices": ["ovr", "cog"], "default": "ovr", "help": "ovr (default) or cog"}),
                ("--refresh", {"action": "store_true", "help": "rebuild if it already exists"})])
    verb(pyr, "fix", cmd_pyramid_fix,
         "finished .cog.tif files whose pyramid stops short (slow zoomed out): rebuild with full levels in scratch, "
         "verify, swap in. No id = check every file. Lossless (elevation) only: photos are listed, not rebuilt", id_req=False)

    mh = ("printed sheet (PDF) of anywhere in Australia, built just for that sheet: --place/--center/--bbox, "
          "--scale 50k (default), --style topo|photo|both. Reuses built areas; offline once data is stored")
    vh = "measure a printed topo sheet against its source data (georef vs grid, roads, creeks, contours)"
    vp_ = nouns.add_parser("verify", parents=[gs], help=vh, description=vh, epilog="example: " + EXAMPLES["verify"])
    vp_.set_defaults(fn=cmd_verify)
    vp_.add_argument("name", help="map (maps/) or area (areas/) name")
    vp_.add_argument("--pdf", help="which print in the folder (default: newest print-*.pdf)")
    vp_.add_argument("--scale", type=int, help="scale denominator if not in the file name")
    mp_ = nouns.add_parser("map", parents=[gs], help=mh, description=mh, epilog="example: " + EXAMPLES["map"])
    mp_.set_defaults(fn=cmd_map)
    for flag, kw in (("--place", {"help": "place name from OSM (town, suburb, locality), e.g. --place Ravenswood"}),
                     ("--center", {"help": "lat,lon sheet centre; use = for negatives: --center=-20.42,146.66"}),
                     ("--bbox", {"help": "w,s,e,n degrees: the sheet is centred on it and must fit (--bbox=146.6,-20.5,146.75,-20.35)"}),
                     ("--name", {"help": "folder name under maps/ (default from the place or centre)"}),
                     ("--scale", {"default": "50k", "help": "25k, 50k (default), 100k or a number"}),
                     ("--paper", {"choices": list(PAPER), "default": "A3", "help": "paper A1-A6 (default A3); print at 100%%: true scale. A5/A6: compact footer"}),
                     ("--portrait", {"action": "store_true", "help": "portrait (default landscape)"}),
                     ("--style", {"choices": ["topo", "photo", "both"], "default": "topo",
                                  "help": "topo (default): paper map with tree cover; photo: satellite; both: one PDF each"}),
                     ("--add", {"action": "append", "metavar": "WHAT", "help": "extra layers for the project / photo sheet: "
                                + ", ".join(MAP_EXTRA) + " (comma list or repeat)"}),
                     ("--interval", {"type": int, "choices": [10, 20], "default": None,
                                     "help": "contour interval; default 20 at 1:50k and smaller; larger scales auto: 20 where steep (mean slope > 15%%), else 10"}),
                     ("--title", {"help": "sheet title"}),
                     ("--dpi", {"type": int, "default": 300, "help": "raster resolution (default 300)"}),
                     ("--no-legend", {"action": "store_true", "help": "leave out the legend"}),
                     ("--no-grid", {"action": "store_true", "help": "no grid lines over the map (margin numbers kept)"}),
                     ("--contours-from", {"choices": ["auto", "nsw-5m", "nsw-lines", "lidar", "dem30"], "dest": "contours_from", "default": None,
                                     "help": "ONE contour source for the print set (no mixing): auto (NSW 5 m DEM in NSW, else QLD LiDAR), "
                                             "nsw-5m, nsw-lines (NSW mapped lines), lidar, dem30; the 30 m DEM fills only its gaps"}),
                     ("--overlay", {"choices": ["auto", "full", "minimal", "none"], "default": "auto", "help": "--style photo line work: full (all ticked layers), minimal (index contours, roads, places), none (imagery only); auto = none (photo sheets are imagery + grid by default)"}),
                     ("--photo-contours", {"choices": ["orange", "white", "faded", "plain"], "help": "contours on --style photo: orange (+dark halo), white (brown + white halo), faded (photo 65%%, dark lines), plain"}),
                ("--contour-mm", {"type": float, "help": "contour line width in mm (default 0.12; config [build] contour_mm)"}),
                ("--index-ratio", {"type": float, "help": "index contour = this x the contour width (default 2; config [build] index_ratio)"}),
                     ("--plain-pdf", {"action": "store_true", "help": "ordinary PDF, not GeoPDF"}),
                     ("--refresh", {"action": "store_true", "help": "rebuild layers that already exist"})):
        mp_.add_argument(flag, **kw)
    ar = nouns.add_parser("area", help="build a map for any box: clipped data + ready QGIS project").add_subparsers(dest="verb", required=True)
    ar._noun = "area"
    verb(ar, "create", cmd_area_create,
         "clip what we have to a box at a detail level into <root>/areas/<name>/ (+ <name>.qgz). "
         "Levels: overview (90m, major roads), regional (30m, S2 10m, main OSM, hazard), "
         "local (+aerial, all OSM, water/infra), street (+QLD roads, 5m contours, parcels). Re-run resumes",
         id_req=None,
         extra=[("name", {"help": "folder/project name, e.g. magnetic-island"}),
                ("--level", {"choices": list(AREA_LEVELS), "default": "local", "help": "detail (default local)"}),
                ("--center", {"help": "lat,lon degrees; use = for negatives: --center=-19.26,146.82"}),
                ("--radius-km", {"type": float, "default": 10.0, "help": "half-width of the box (default 10)"}),
                ("--bbox", {"help": "w,s,e,n degrees instead of --center (--bbox=146.7,-19.4,146.9,-19.2)"}),
                ("--refresh-live", {"action": "store_true", "help": "re-download the parts made from live servers (tile topo/imagery, ArcGIS, GA sheets, vegetation); never automatic"}),
                ("--rebuild", {"action": "store_true", "help": "rebuild every part even if its sources are unchanged"}),
                ("--contours-from", {"choices": ["auto", "nsw-5m", "nsw-lines", "lidar", "dem30"], "dest": "contours_from", "default": None,
                                     "help": "ONE contour source for the print set (no mixing): auto (NSW 5 m DEM in NSW, else QLD LiDAR), "
                                             "nsw-5m, nsw-lines (NSW mapped lines), lidar, dem30; the 30 m DEM fills only its gaps"}),
                ("--force", {"action": "store_true", "help": "allow a box bigger than the level's limit"}),
                ("--3d", {"action": "store_true", "dest": "with_3d", "help": "also build 3D terrain + landing-site candidates "
                          "(not needed for print maps; skipped by default)"}),
                ("--aerial", {"action": "store_true", "help": "also clip the aerial photo where one covers the box "
                              "(Paluma-Dalbeg/Townsville; skipped by default, prints use satellite)"}),
                ("--full", {"action": "store_true", "help": "everything: --3d and --aerial"}),
                ("--sat-2m4", {"action": "store_true", "help": "sharper satellite: QLD state imagery at 2.4 m, downloaded once "
                               "for this box and saved in the area (works offline after; ~0.1 MB/km2; Planet fill: personal use "
                               "only). Existing area: delete its sat.tif and re-run"}),
                ("--sat-res", {"type": float, "metavar": "M", "help": "like --sat-2m4 at any pixel size in metres, "
                               "e.g. 0.3 or 0.1 (Townsville urban has 10cm). Per km2: 0.3m ~4 MB/11 s, 0.1m ~22 MB/4 min"})])
    verb(ar, "list", cmd_area_list, "areas built so far", id_req=None)
    verb(ar, "print", cmd_area_print,
         "printable sheet (GeoPDF): fixed scale, MGRS 1 km grid with 2-digit labels, declination/GMA in mils, "
         "legend, imagery dates. Uses the layers ticked in the area's project",
         id_req=None,
         extra=[("name", {"help": "area name"}),
                ("--scale", {"type": int, "default": 25000, "help": "map scale denominator (default 25000; 50000 etc.)"}),
                ("--paper", {"choices": list(PAPER), "default": "A3", "help": "paper A1-A6 (default A3); print at 100%%: true scale. A5/A6: compact footer"}),
                ("--portrait", {"action": "store_true", "help": "portrait (default landscape)"}),
                ("--center", {"help": "lat,lon sheet centre (default: area centre); --center=-19.0,146.2"}),
                ("--title", {"help": "sheet title"}),
                ("--label", {"help": "file name part: print-<label>.pdf (default <paper>-<scale>k)"}),
                ("--dpi", {"type": int, "default": None, "help": "raster resolution (default 300; photo sheets: LMAP_PHOTO_DPI, config [build] photo_dpi, else 1200)"}),
                ("--no-legend", {"action": "store_true", "help": "leave out the legend"}),
                ("--no-grid", {"action": "store_true", "help": "no grid lines over the map (margin numbers kept)"}),
                ("--overlay", {"choices": ["auto", "full", "minimal", "none"], "default": "auto", "help": "--style photo line work: full (all ticked layers), minimal (index contours, roads, places), none (imagery only); auto = none (photo sheets are imagery + grid by default)"}),
                     ("--photo-contours", {"choices": ["orange", "white", "faded", "plain"], "help": "contours on --style photo: orange (+dark halo), white (brown + white halo), faded (photo 65%%, dark lines), plain"}),
                ("--contour-mm", {"type": float, "help": "contour line width in mm (default 0.12; config [build] contour_mm)"}),
                ("--index-ratio", {"type": float, "help": "index contour = this x the contour width (default 2; config [build] index_ratio)"}),
                ("--plain-pdf", {"action": "store_true", "help": "ordinary PDF, not GeoPDF"}),
                ("--style", {"choices": ["topo", "photo"], "default": "topo",
                             "help": "topo (default): paper map - light background, faint hillshade, bold contours, "
                                     "tracks, creeks, water; photo: the project's ticked layers (satellite etc.)"}),
                ("--contours", {"choices": ["best", "lidar", "dem", "none", "ticked"], "default": "best",
                                "help": "best (default): LiDAR where flown, approx DEM contours elsewhere, never both; "
                                        "lidar / dem: only that set; none; ticked: as ticked in the project"}),
                ("--interval", {"type": int, "choices": [10, 20], "default": None,
                                "help": "contour spacing: 10 (index 50) or 20 (index 100); default 20 at 1:50k "
                                        "and smaller; larger: 20 where steep (mean slope > 15%%), else 10"})])
    verb(ar, "export", cmd_area_export,
         "phone/ATAK bundle for an area: tiles (MBTiles + OsmAnd .sqlitedb), points KMZ+GPX (water, shelter, "
         "emergency, tides, landing sites), DTED, TAK data package, print PDFs. Plain files; --encrypt = AES-256 zip",
         id_req=None,
         extra=[("name", {"help": "area name"}),
                ("target", {"nargs": "?", "help": "where to write (e.g. the USB); default areas/<name>/export"}),
                ("--max-zoom", {"type": int, "default": 17, "help": "highest tile zoom (default 17 ~1.2 m/px)"}),
                ("--encrypt", {"action": "store_true", "help": "pack into one AES-256 zip (passphrase asked twice)"}),
                ("--refresh", {"action": "store_true", "help": "rebuild tiles that already exist"})])
    verb(ar, "image", cmd_area_image,
         "the area's map as one big zoomable image: GeoTIFF/COG (LZW, Deflate, JPEG or WEBP inside, overviews), same layers and "
         "contours as print, no margin. Opens in QGIS, ATAK, any GDAL tool; --jpeg / --kmz copies for viewers/phones",
         id_req=None,
         extra=[("name", {"help": "area name"}),
                ("--res", {"type": float, "metavar": "M", "help": "metres per pixel (overrides --scale; symbols and text are still sized for --dpi)"}),
                ("--scale", {"type": int, "default": 25000, "help": "styled as this print scale (default 25000)"}),
                ("--dpi", {"type": int, "default": 150, "help": "with --scale: pixels per inch (default 150 -> 4.2 m/px at 1:25k)"}),
                ("--bbox", {"help": "w,s,e,n degrees, part of the area (default the whole area)"}),
                ("--label", {"help": "file name part: image-<label>.tif"}),
                ("--codec", {"choices": list(IMAGE_CODECS), "default": IMAGE_CODECS[0], "help": "inside the GeoTIFF "
                             "(default lzw: lossless, opens in MapNero; deflate: lossless, usually a bit smaller; "
                             "jpeg ~5x smaller (measured) but MapNero can't open it; webp smaller still, newer readers only)"}),
                ("--jpeg", {"action": "store_true", "help": "also a plain .jpg + .wld (image viewers; up to 500 Mpx)"}),
                ("--kmz", {"action": "store_true", "help": "also a .kmz (Google Earth, Avenza, AlpineQuest; up to 500 Mpx)"}),
                ("--force", {"action": "store_true", "help": f"allow more than {IMAGE_MAX_PX // 10**9} Gpx"}),
                ("--contours", {"choices": ["best", "lidar", "dem", "none", "ticked"], "default": "best",
                                "help": "as area print"}),
                ("--interval", {"type": int, "choices": [10, 20], "default": 10, "help": "contour spacing 10 (default) or 20"})])
    verb(ar, "phone-map", cmd_area_phone_map,
         "the area's own map (topo and/or photo, print styling redrawn for each zoom) as phone tiles: "
         "phone-<style>.mbtiles + .sqlitedb (OsmAnd) in the area folder. Offline: reads only the area's files",
         id_req=None,
         extra=[("name", {"help": "area name"}),
                ("--style", {"choices": ["topo", "photo", "both"], "default": "both", "help": "default both"}),
                ("--zoom", {"default": None,
                            "help": f"MIN-MAX web zoom (default {PHONE_ZOOMS[0]}-{PHONE_ZOOMS[1]}, photo to {PHONE_SAT_Z} when the area has no aerial; 17 ~ 1 m/px, each +1 = 4x tiles)"}),
                ("--dpi", {"type": int, "default": PHONE_DPI,
                           "help": f"label/symbol size (default {PHONE_DPI}; 96 = desktop web map size)"}),
                ("--bbox", {"help": "w,s,e,n degrees, part of the area (default the whole area)"}),
                ("--label", {"help": "file name part: phone-<style>-<label>.mbtiles"}),
                ("--force", {"action": "store_true", "help": "allow more than PHONE_MAX_TILES tiles per style"})])
    verb(ar, "viewshed", cmd_area_viewshed,
         "what can be seen from a point (terrain only, earth curvature on); adds a red overlay to the area's project",
         id_req=None,
         extra=[("name", {"help": "area name"}),
                ("--point", {"required": True, "help": "lat,lon of the observer: --point=-19.0,146.2"}),
                ("--height", {"type": float, "default": 1.7, "help": "observer eye height above ground, m (default 1.7)"}),
                ("--target-height", {"type": float, "default": 1.7,
                                     "help": "height of what is being looked for, m (1.7 person, 3 vehicle; default 1.7)"}),
                ("--radius-km", {"type": float, "default": 10.0, "help": "max distance (default 10)"}),
                ("--label", {"help": "name for the layer/file (default lat,lon)"}),
                ("--no-project", {"action": "store_true", "help": "don't rebuild the .qgz"})])

    q = nouns.add_parser("qld", help="statewide QLD Government layers stored offline (areas clip them locally)").add_subparsers(dest="verb", required=True)
    q._noun = "qld"
    tier = ("--tier", {"choices": ["essentials", "everything"], "default": "essentials",
                       "help": "essentials: water, infrastructure, hazard, roads (~3 h); everything: + parcels, "
                               "LiDAR contours, all watercourses/reservoirs (~12 h, ~20 GB)"})
    verb(q, "fetch", cmd_qld_fetch, "download the tier statewide in the background (resumes; --dry-run shows what's left)",
         id_req=None, extra=[tier, ("--refresh", {"action": "store_true", "help": "re-download sources already done"})])
    verb(q, "project", cmd_qld_project, "write <root>/data/australia/queensland/qld.qgz: one QGIS project over the statewide offline data (--yes)", id_req=None,
         extra=[("--rebuild", {"action": "store_true", "help": "rebuild the derived files (OSM cut, main roads, shading, contour tiles) regardless"})])
    verb(q, "status", cmd_qld_status, "progress of the statewide downloads", id_req=None, extra=[tier])

    nw = nouns.add_parser("nsw", help="statewide NSW Government layers (lmap data fetch nsw-*-state)").add_subparsers(dest="verb", required=True)
    nw._noun = "nsw"
    verb(nw, "project", cmd_nsw_project, "write data/australia/new-south-wales/nsw.qgz: one QGIS project over the statewide "
         "NSW data (--yes)", id_req=None,
         extra=[("--rebuild", {"action": "store_true", "help": "rebuild the derived files (OSM cut, main roads, shading, contour tiles) regardless"})])
    au = nouns.add_parser("au", help="all of Australia").add_subparsers(dest="verb", required=True)
    au._noun = "au"
    verb(au, "grid", cmd_au_grid,
         "MGRS grid for australia.qgz: 1 km lines (zoomed in past 1:100k), 10 km, 100 km, zone edges, 2-digit numbers "
         "and square IDs ('55K DU'), over land + 20 km of sea, all 8 zones. One-off; then `lmap au project --yes`",
         id_req=None, extra=[("--refresh", {"action": "store_true", "help": "rebuild even if up to date"})])
    verb(au, "contours", cmd_au_contours,
         "20 m contours (index 100 m) for all of Australia from the national 30 m elevation -> data/australia/contours-30m.gpkg "
         "(~1 h, resumable). Then `lmap au project --yes`. Detail: `lmap area contours` (10 m / LiDAR)",
         id_req=None, extra=[("--refresh", {"action": "store_true", "help": "rebuild even if up to date"})])
    verb(au, "project", cmd_au_project, "write <root>/data/australia/australia.qgz: national overview map (--yes; "
         "first run builds a 90m hillshade + main-roads file)", id_req=None,
         extra=[("--rebuild", {"action": "store_true", "help": "rebuild main roads, field features, shading regardless"}),
                ("--lidar-res", {"type": float, "default": None, "metavar": "M",
                                 "help": "3D view: real QLD LiDAR at M metres where flown (default 5; 2 = sharper, "
                                         "~1 h first build; 0 = off). Remembered for later runs"})])

    sh = nouns.add_parser("sheets", help="GA topographic map sheets on demand: 1:250k (map layer + PDF), 1:100k / 1:50k "
                          "(print files)").add_subparsers(dest="verb", required=True)
    sh._noun = "sheets"
    verb(sh, "index", cmd_sheets_index,
         "download the GA sheet indexes (1:250k AUSTopo, 1:100k, 1:50k) -> data/australia/sheets/index.gpkg (--yes; ~2 min)",
         id_req=None, extra=[("--refresh", {"action": "store_true", "help": "re-download even if present"})])
    verb(sh, "get", cmd_sheets_get,
         "download the sheets around a point (or one by number) -> data/australia/sheets/<scale>/. 250k: GeoPDF + a map layer "
         "(cut to the frame); 100k/50k: the original print scans (not georeferenced; AGD66 grid). Then `lmap au project --yes`",
         id_req=None,
         extra=[("--at", {"metavar": "POS", "help": "MGRS (quote it) or lat,lon"}),
                ("--sheet", {"metavar": "NUMBER", "help": "one sheet: SE55-14 (250k), 8259 (100k), 8259-2 (50k)"}),
                ("--radius-km", {"type": float, "default": 0.0, "help": "sheets touching this circle (default 0 = the one under --at)"}),
                ("--scale", {"default": "250k", "help": "comma list of 250k,100k,50k (default 250k). 100k/50k = "
                             "HISTORICAL 1961-2008 scans, ~20 m off, printed grid ~200 m off: never navigate by them"})])

    te = nouns.add_parser("terrain", help="ground analysis on the offline DEMs: slope, viewshed, line of sight").add_subparsers(dest="verb", required=True)
    te._noun = "terrain"
    verb(te, "slope", cmd_terrain_slope,
         "slope layers for australia.qgz: whole-degree slope at 30 m (all Australia) + 10 m from the 2 m LiDAR; "
         "styles walk/4WD (default), bushfire AS 3959, plain degrees. Then `lmap au project --yes` adds them",
         id_req=None,
         extra=[("--refresh", {"action": "store_true", "help": "rebuild even if up to date"})])
    verb(te, "viewshed", cmd_terrain_viewshed,
         "what ground can be seen from a point, or from a set of OPs (each cell = how many see it). Writes "
         "data/australia/viewsheds/NAME.cog.tif (--yes); `lmap au project --yes` adds it. Bare earth: trees/buildings not "
         "included; the finest terrain with data all round the circle: 2 m LiDAR, else 10 m LiDAR, else 30 m",
         id_req=None,
         extra=[("--from", {"dest": "from_", "metavar": "POS", "help": "observer: MGRS (quote it) or lat,lon"}),
                ("--points", {"metavar": "FILE", "help": f"up to {VS_MAX_PTS} OPs: 'name, MGRS-or-lat,lon[, 5m]' per "
                              "line, or .gpx/.kml/.gpkg/.geojson points"}),
                ("--name", {"help": "file/layer name (default the points file name, or the 100 m MGRS)"}),
                ("--height", {"type": float, "default": 1.7, "help": "eye height above ground, m (default 1.7; "
                              "0.5 prone, 3 vehicle roof, 10 mast)"}),
                ("--target-height", {"type": float, "default": 1.7,
                                     "help": "height of what is looked for, m (default 1.7 person; 0 = the ground itself)"}),
                ("--radius-km", {"type": float, "default": 10.0, "help": f"how far to look, km (default 10, max {VS_MAX_KM})"}),
                ("--refraction", {"type": float, "metavar": "K", "help": "refraction coefficient (default 0.143)"}),
                ("--angles", {"action": "store_true", "help": "also NAME-angle.cog.tif: vertical angle in mils from "
                              "the eye to each seen cell (+ up, - down); one observer (--from) only"}),
                ("--no-project", {"action": "store_true", "help": "don't suggest the project rebuild"})])
    verb(te, "route", cmd_terrain_route,
         "a walk along waypoints or a GPX track: distance, ground distance, climb/descent, Naismith + Tobler times, "
         "per leg with grid/magnetic bearings (a nav card). Read-only unless --png/--gpkg",
         id_req=None,
         extra=[("--via", {"action": "append", "metavar": "POS", "help": "a waypoint (repeat, in order): MGRS or lat,lon"}),
                ("--points", {"metavar": "FILE", "help": "waypoints file ('name, position' per line, in order) or a "
                              ".gpx route/track, or a line in .gpkg/.kml/.geojson"}),
                ("--off-track", {"action": "store_true", "help": "cross-country: Tobler x0.6"}),
                ("--hysteresis", {"type": float, "default": 3.0, "metavar": "M",
                                  "help": "ignore ups/downs smaller than this when adding climb (default 3 m)"}),
                ("--png", {"metavar": "FILE", "help": "save the height profile"}),
                ("--gpkg", {"metavar": "FILE", "help": "save legs (3D lines) + waypoints for QGIS"})])
    verb(te, "bestroute", cmd_terrain_bestroute,
         "easiest walking line between points (GRASS r.walk: Langmuir walking time over the DEM + friction from OSM "
         "roads/creeks/rivers and QLD lakes), then the same stats as `terrain route`. Read-only unless --png/--gpkg",
         id_req=None,
         extra=[("--from", {"dest": "from_", "metavar": "POS", "help": "start: MGRS (quote it) or lat,lon"}),
                ("--to", {"metavar": "POS", "help": "end: MGRS or lat,lon"}),
                ("--via", {"action": "append", "metavar": "POS", "help": "must pass through (repeat, in order)"}),
                ("--points", {"metavar": "FILE", "help": "ordered points file ('name, position' per line) or .gpx/.kml"}),
                ("--roads", {"choices": ["use", "ignore", "avoid"], "default": "use",
                             "help": "use roads/tracks (default), ignore them, or avoid them (5 s/m extra)"}),
                ("--friction-weight", {"type": float, "default": 1.0, "metavar": "L",
                                       "help": "r.walk lambda: how much the friction counts (default 1; 0 = terrain only)"}),
                ("--margin-km", {"type": float, "default": None, "help": "search area beyond the points (default "
                                 "a quarter of the distance, min 1 km)"}),
                ("--res", {"type": float, "metavar": "M", "help": "grid metres (default the DEM's, min 5)"}),
                ("--png", {"metavar": "FILE", "help": "save the height profile"}),
                ("--gpkg", {"metavar": "FILE", "help": "save the line (3D) + points for QGIS / a GPS"})])
    verb(te, "water", cmd_terrain_water,
         "where water runs and collects, from the offline terrain (no download): gullies/creeks with upstream area, "
         "catchments, a wetness layer and ranked wet spots (where to look/dig for water). Uses the finest terrain with "
         "data there (2 m LiDAR, 10 m LiDAR, else 30 m). Writes data/australia/water/NAME.gpkg + NAME-wetness.cog.tif (--yes)",
         id_req=None,
         extra=[("--from", {"dest": "from_", "metavar": "POS", "help": "centre: MGRS (quote it) or lat,lon"}),
                ("--radius-km", {"type": float, "default": 5.0, "help": "half-width of the square, km (default 5)"}),
                ("--name", {"help": "file/layer name (default water-<100 m MGRS>)"}),
                ("--res", {"type": float, "metavar": "M", "help": "grid metres (default the terrain's own, min 2)"}),
                ("--stream-km2", {"type": float, "default": 0.05, "metavar": "KM2",
                                  "help": "a flow line starts at this much upstream area (default 0.05 km2 = 5 ha)"}),
                ("--catchment-km2", {"type": float, "default": 2.0, "metavar": "KM2",
                                     "help": "smallest catchment drawn (default 2 km2)"}),
                ("--wet-spots", {"type": int, "default": 50, "metavar": "N", "help": "how many wet spots to list (default 50)"}),
                ("--no-breach", {"dest": "breach", "action": "store_false",
                                 "help": "don't cut the terrain through road/rail banks at OSM waterway crossings/culverts"})])
    verb(te, "los", cmd_terrain_los,
         "line of sight over the terrain: can A see B? Where is it blocked, what ground is dead, radio Fresnel check. "
         "Positions as MGRS ('55K DU 81084 70615') or lat,lon. --points FILE = every pair (who sees whom). "
         "Read-only unless --png/--gpkg (overwriting needs --yes). Bare earth: trees/buildings not included",
         id_req=None,
         extra=[("--from", {"dest": "from_", "metavar": "POS", "help": "observer: MGRS (quote it) or lat,lon"}),
                ("--to", {"metavar": "POS", "help": "target: MGRS or lat,lon"}),
                ("--from-name", {"help": "label for --from (default A)"}),
                ("--to-name", {"help": "label for --to (default B)"}),
                ("--points", {"metavar": "FILE", "help": "many positions: text 'name, MGRS-or-lat,lon[, 5m]' per line, "
                              "or .gpx/.kml/.gpkg/.geojson points with a name; checks every pair"}),
                ("--height", {"type": float, "default": 1.7, "help": "eye height above ground, m (default 1.7 standing; "
                              "0.5 prone, 3 vehicle roof, 10 mast). With --points: every point's height"}),
                ("--target-height", {"type": float, "default": 1.7,
                                     "help": "target height above ground, m (default 1.7 person; 0.5 prone, 2.5 vehicle)"}),
                ("--radio", {"type": float, "metavar": "MHZ", "help": "radio link check at this frequency: 60%% first "
                             "Fresnel zone clearance, 4/3 earth (UHF CB 477, 2 m ham 146, 70 cm 438)"}),
                ("--refraction", {"type": float, "metavar": "K", "help": "refraction coefficient (default 0.143 light, "
                                  "0.25 with --radio)"}),
                ("--png", {"metavar": "FILE", "help": "save the profile picture (one line)"}),
                ("--gpkg", {"metavar": "FILE", "help": "save sightlines + points for QGIS (green/red by visible)"})])

    cf = nouns.add_parser("config", help="settings in effect and where each comes from").add_subparsers(dest="verb",
                                                                                                       required=True)
    cf._noun = "config"
    verb(cf, "show", cmd_config_show, "every setting's value and origin (flag / env / config file / default); "
         "unknown keys in the config file are reported", id_req=None)

    mo = nouns.add_parser("moon", help="the Moon (NASA LRO data)").add_subparsers(dest="verb", required=True)
    mo._noun = "moon"
    verb(mo, "project", cmd_moon_project, "write <root>/data/moon/moon.qgz (whole Moon: photo + shading) and moon-spole.qgz "
         "(south pole, polar projection, 20m/5m zoomed in) (--yes; first run builds a hillshade, ~20 min)", id_req=None)

    rf = nouns.add_parser("refresh", parents=[gs], help="refresh everything as one parallel plan (--dry-run shows it)",
                          description="Re-download the main sources and rebuild projects + areas. Independent steps run "
                                      "at once (each its own lmap process + log); dependants wait.",
                          epilog="example: lmap refresh --dry-run ; lmap refresh --yes --areas townsville")
    rf.add_argument("--areas", help="comma list of areas to rebuild (default: every areas/*/area.json; '' = none)")
    rf.add_argument("--only", help="comma list: fetch ids and/or 'projects' (default: all)")
    rf.add_argument("--plan", help="TOML plan file: [step.<id>] cmd=[lmap args], deps=[ids]")
    rf.add_argument("--width", help="steps at once (default 6)")
    rf.add_argument("--resume", action="store_true",
                    help="redo only what the last refresh did not finish (same plan); finished steps are not repeated")
    rf.add_argument("--outdated", action="store_true",
                    help="only sources with newer data upstream (lmap data outdated) + the projects/areas that use them")
    rf.add_argument("--live", action="store_true",
                    help="areas also re-download their live-server parts (area create --refresh-live); otherwise kept")
    rf.set_defaults(fn=cmd_refresh)

    cl = nouns.add_parser("clean", parents=[gs], help="delete lmap's leftovers in scratch (--dry-run lists them)",
                          description="Delete lmap's own leftovers in its scratch dir: temp files of finished or dead runs, "
                                      "caches of finished downloads. Never a running job's files, never files lmap "
                                      "didn't make. Resume points (stopped downloads/builds) stay unless --resume.",
                          epilog="example: " + EXAMPLES["clean"])
    cl.add_argument("--resume", action="store_true", help="also delete resume points (a stopped job restarts from zero)")
    cl.set_defaults(fn=cmd_clean)

    ch = nouns.add_parser("cache", help="built areas/maps (rebuildable): list, rm, prune, pin - never the stored data").add_subparsers(dest="verb", required=True)
    ch._noun = "cache"
    verb(ch, "list", cmd_cache_list, "every area/map: size, made, last used, pinned", id_req=None,
         extra=[("--kind", {"choices": ["areas", "maps"], "help": "only areas or only maps"})])
    verb(ch, "rm", cmd_cache_rm, "delete areas/maps (rebuilt on demand from stored data). --dry-run first", id_req=None,
         extra=[("names", {"nargs": "+", "metavar": "NAME", "help": "area/map name(s); areas/NAME or maps/NAME"}),
                ("--force", {"action": "store_true", "help": "also pinned ones"})])
    verb(ch, "prune", cmd_cache_prune, "delete unpinned areas/maps by age or count. --dry-run first", id_req=None,
         extra=[("--older", {"type": float, "metavar": "DAYS", "help": "not used for DAYS days"}),
                ("--keep", {"type": int, "metavar": "N", "help": "keep only the N most recently used"}),
                ("--kind", {"choices": ["areas", "maps"], "help": "only areas or only maps"})])
    verb(ch, "pin", cmd_cache_pin, "keep an area/map: prune skips it, rm needs --force", id_req=None,
         extra=[("name", {"help": "area/map name"})])
    verb(ch, "unpin", cmd_cache_unpin, "undo pin", id_req=None, extra=[("name", {"help": "area/map name"})])
    verb(dat, "rm", cmd_data_rm, "DELETE a stored source (the main copy). Needs --yes --confirm ID; only-copy sources also --force",
         extra=[("--confirm", {"metavar": "ID", "help": "the source id typed again"}),
                ("--force", {"action": "store_true", "help": "allow deleting an only copy (imported / not downloadable)"})])
    im = nouns.add_parser("import", help="bring layers, lmap bundles or ATAK packages into the store").add_subparsers(dest="verb", required=True)
    im._noun = "import"
    verb(im, "add", cmd_import_add, "import a layer (gpkg/tif/kml/kmz/gpx/shp/geojson), an .lmapbundle or an ATAK .zip", id_req=None,
         extra=[("path", {"help": "file to import"}),
                ("--as", {"dest": "as_", "metavar": "ID", "help": "id (layers) or new name (bundles); default from the file name"}),
                ("--title", {"help": "layer title"})])
    verb(im, "list", cmd_import_list, "imported layers", id_req=None)
    verb(im, "rm", cmd_import_rm, "DELETE an imported layer (the only copy): --yes --confirm ID --force",
         extra=[("--confirm", {"metavar": "ID", "help": "the id typed again"}),
                ("--force", {"action": "store_true", "help": "required: it can't be downloaded again"})])
    verb(exp, "bundle", cmd_export_bundle, "one area/map, or one imported layer, as a single .lmapbundle file for another lmap (`lmap import add`)", id_req=None,
         extra=[("name", {"help": "area/map name, or an imported layer id (local/ID)"}), ("--to", {"required": True, "help": "folder to write NAME.lmapbundle into"})])

    td = nouns.add_parser("tides", parents=[gs], help="offline tide table (MSQ predictions): next high/low tides",
                          description="Offline tide table from the downloaded MSQ predictions (lmap data fetch qld-tides).",
                          epilog="example: lmap tides --near=-19.26,146.82 --days 3")
    td.add_argument("--near", help="lat,lon: nearest gauge (use =: --near=-19.26,146.82)")
    td.add_argument("--station", help="gauge name (part), e.g. townsville")
    td.add_argument("--days", type=int, default=7, help="how many days (default 7)")
    td.add_argument("--date", help="start date YYYY-MM-DD (default now)")
    td.add_argument("--list", action="store_true", help="list gauges")
    td.set_defaults(fn=cmd_tides)

    qu = nouns.add_parser("queue", help="run many sources at once: shared bandwidth cap, per-server limits, "
                                        "shared CPU pool").add_subparsers(dest="verb", required=True)
    qu._noun = "queue"
    ids = ("ids", {"nargs": "+", "metavar": "id", "help": "source ids"})
    bw = ("--bandwidth", {"help": "total download cap, e.g. 20MiB, 160Mbit, 0 = unlimited "
                                  "(env LMAP_BANDWIDTH, config [net] bandwidth)"})
    verb(qu, "add", cmd_queue_add, "add sources (a running queue picks them up within 15 s)", id_req=None,
         extra=[ids, ("--refresh", {"action": "store_true", "help": "re-get them even if the file already exists "
                                    "(e.g. to re-encode); stays set across queue stop/run until each is done"})])
    verb(qu, "remove", cmd_queue_remove, "drop sources not yet started", id_req=None, extra=[ids])
    verb(qu, "run", cmd_queue_run, "run the queue: one thread per server, all servers at once (--serial: one "
         "source at a time, in queue order; --detach: background)",
         id_req=None, extra=[bw, ("--detach", {"action": "store_true", "help": "run in the background, log to queue.log"}),
                             ("--serial", {"action": "store_true", "help": "one source at a time in queue order, each "
                                           "built and cleared from scratch before the next (config queue=\"serial\")"}),
                             ("--jobs", {"help": "MAX parallel downloads per source (default 3; S3 satellite: 8)"}),
                             ("--shrink", {"help": "CPU slots: heavy GDAL jobs at once across ALL sources "
                                                   "(default cores/2, max 4)"}),
                             ("--tmp", {"help": "scratch dir"})])
    verb(qu, "status", cmd_queue_status, "what's queued, running, done, failed", id_req=None)
    verb(qu, "stop", cmd_queue_stop, "pause the whole queue (resumes with queue run); --now aborts",
         id_req=None, extra=[("--now", {"action": "store_true", "help": "abort in-flight work"})])
    return p


SLICE = "lmap.slice"


def slice_props(lim):
    """systemd properties that hold ALL of lmap (every process, together) to the [limits] caps: CPU as a quota of
    cpu% x cores; memory soft at ram% (the kernel reclaims and slows lmap there, nothing is killed) and hard at
    ram% + 10 (only lmap's processes are killed, never the desktop)."""
    cores = os.cpu_count() or 2
    return [f"CPUQuota={lim['cpu'] * cores}%", f"MemoryHigh={lim['ram']}%", f"MemoryMax={min(100, lim['ram'] + 10)}%"]


def mem_scope(argv):
    """Run this command inside the shared lmap.slice (one per user), whose caps cover every lmap process at once:
    two jobs share the 80%, they don't get 80% each. The slice's caps are set on each start, so a [limits] change
    applies to jobs started after it. A systemd user scope; quietly skipped where that is unavailable (no systemd,
    containers, CI): the in-process limits still apply. Children inherit LMAP_SCOPE: one scope per command tree."""
    if os.environ.get("LMAP_SCOPE") or not shutil.which("systemd-run") or not os.environ.get("XDG_RUNTIME_DIR"):
        return
    os.environ["LMAP_SCOPE"] = "1"               # set before the probe: a failed exec can't loop
    try:
        lim = limits()
    except CliError:
        return                                   # bad [limits]: the command itself reports it
    q = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    base = ["systemd-run", "--user", "--scope", "-q", "--collect", f"--slice={SLICE}"]
    try:
        if subprocess.run(base + ["true"], **q).returncode:
            return
        if shutil.which("systemctl"):            # caps on the slice, not the scope: shared by every lmap scope
            if subprocess.run(["systemctl", "--user", "set-property", "--runtime", SLICE, *slice_props(lim)],
                              **q).returncode:
                print(f"lmap: WARNING: could not set the CPU/RAM caps on {SLICE}; thread caps still apply",
                      file=sys.stderr)
        sys.stdout.flush(); sys.stderr.flush()
        os.execvp("systemd-run", base + ["--", sys.executable, os.path.abspath(__file__), *argv])
    except (OSError, subprocess.SubprocessError):
        return


def scoped(a):
    """Commands that start work (anything not a --dry-run, read-only listing or help) run in the slice."""
    return not getattr(a, "dry_run", False) and (getattr(a, "yes", False) or a.fn in MEM_SCOPED)


MEM_SCOPED = {cmd_refresh, cmd_area_create, cmd_qld_project, cmd_nsw_project, cmd_au_project}


def main_inner(argv=None):
    cli = argv is None                           # the real command line (tests pass argv: never re-exec those)
    p = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    glued, i = [], 0                         # "--center -19.2,146.8": glue negative coords so argparse keeps them
    while i < len(argv):
        if argv[i] in ("--center", "--bbox", "--point", "--near", "--from", "--to", "--at") and i + 1 < len(argv) and re.match(r"^-[\d.]", argv[i + 1]):
            glued.append(f"{argv[i]}={argv[i + 1]}"); i += 2
        else:
            glued.append(argv[i]); i += 1
    a = p.parse_args(glued)
    if not getattr(a, "fn", None):
        p.print_help(sys.stderr); return E_USAGE
    if cli and scoped(a):
        mem_scope(argv)                          # re-runs this command inside lmap.slice (no return)
    if cli:
        run_begin(a, argv)                       # run registry (16_runs.py): listed once it runs > RUN_AFTER s
    try:
        if a.fn is cmd_config_show:              # diagnose a config that Ctx itself rejects
            try:
                c_ = Ctx(a)
            except CliError as e:
                c_ = types.SimpleNamespace(a=a, error=e.msg, error_hint=e.hint)
            res = a.fn(c_)
        else:
            res = a.fn(Ctx(a))
        if a.json:
            print(json.dumps(res, default=str))
        elif not a.quiet:
            human(res)
        if isinstance(res, dict) and res.get("stopped") in ("aborted", "drained"):
            return E_ABORTED if res["stopped"] == "aborted" else E_DRAINED   # automation: stop != success
        if isinstance(res, dict) and isinstance(res.get("exit_code"), int):
            return res["exit_code"]                    # a partial result: its JSON is kept, the code says why
        return E_FAIL if isinstance(res, dict) and res.get("ok") is False else E_OK
    except CliError as e:
        print(json.dumps({k: v for k, v in {"error": e.msg, "hint": e.hint}.items() if v}), file=sys.stderr)
        return e.code
    except (KeyboardInterrupt, Aborted):
        print(json.dumps({"error": "interrupted", "hint": "re-run the same command to resume"}), file=sys.stderr)
        return E_ABORTED
    except Exception as e:                       # contract: one-line JSON error, never a traceback
        print(json.dumps({"error": f"{type(e).__name__}: {e}",
                          "hint": "re-run with -v; report if it persists"}), file=sys.stderr)
        if getattr(a, "verbose", False):
            import traceback; traceback.print_exc()
        return E_FAIL


def main(argv=None):
    """The command line: main_inner, then the run registry record gets its final state + exit code."""
    code = E_FAIL
    try:
        code = main_inner(argv)
        if code == E_FAIL and DRAIN.is_set() and (RUN.d or {}).get("drain"):
            code = E_DRAINED                     # stopped by `job stop` / SIGTERM: not a failure
        return code
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else E_FAIL; raise
    except KeyboardInterrupt:
        code = E_ABORTED; raise
    finally:
        RUN.finish(code)
