# bash completion for lmap. Installed by install.sh into
# ~/.local/share/bash-completion/completions/lmap (picked up automatically by bash-completion).
# Source ids come live from `lmap list --json`, so new catalog entries complete without editing this.

_lmap_ids() {
    lmap list --json 2>/dev/null | python3 -c 'import json,sys
try:
    print(" ".join(r["id"] for r in json.load(sys.stdin)["list"]))
except Exception:
    pass' 2>/dev/null
}

_lmap() {
    local cur prev words cword
    _init_completion 2>/dev/null || { cur="${COMP_WORDS[COMP_CWORD]}"; prev="${COMP_WORDS[COMP_CWORD-1]}"; words=("${COMP_WORDS[@]}"); cword=$COMP_CWORD; }

    local globals="--json --dry-run --yes -y --verbose -v --quiet -q --root --catalog --help -h"
    local nouns="status list source data job queue refresh clean export vrt pyramid verify map area sheets cache import config terrain qld nsw au moon tides"

    # value-taking flags: complete paths for these, nothing for numbers
    case "$prev" in
        --root|--catalog|--tmp) COMPREPLY=($(compgen -d -- "$cur")); return ;;
        --jobs|--shrink|--wait|--bbox) return ;;
        --format) COMPREPLY=($(compgen -W "copy phone atak all" -- "$cur")); return ;;
        --mode) COMPREPLY=($(compgen -W "ovr cog" -- "$cur")); return ;;
        --tier) COMPREPLY=($(compgen -W "essentials everything" -- "$cur")); return ;;
        --level) COMPREPLY=($(compgen -W "overview regional local street" -- "$cur")); return ;;
        --center|--radius-km) return ;;
    esac

    # find noun + verb (skip global flags and their values)
    local i w noun="" verb="" npos=0
    for ((i = 1; i < cword; i++)); do
        w="${words[i]}"
        case "$w" in
            --root|--catalog|--tmp|--jobs|--shrink|--wait|--format|--bbox|--mode|--level|--center|--radius-km|--tier) ((i++)); continue ;;
            -*) continue ;;
        esac
        if [ -z "$noun" ]; then noun="$w"
        elif [ -z "$verb" ]; then verb="$w"
        else ((npos++)); fi
    done

    if [[ "$cur" == -* ]]; then
        local extra=""
        case "$noun $verb" in
            "data fetch")   extra="--refresh --tmp --jobs --shrink --allow-missing" ;;
            "data verify")  extra="--quick" ;;
            "job start")    extra="--tmp --jobs --shrink" ;;
            "job stop")     extra="--now --wait" ;;
            "job list")     extra="--all" ;;
            "export create") extra="--format --bbox" ;;
            "pyramid create") extra="--mode --refresh" ;;
            "qld fetch")    extra="--tier --refresh" ;;
            "qld status")   extra="--tier" ;;
            "area create")  extra="--level --center --radius-km --bbox --refresh --force" ;;
        esac
        COMPREPLY=($(compgen -W "$globals $extra" -- "$cur")); return
    fi

    if [ -z "$noun" ]; then
        COMPREPLY=($(compgen -W "$nouns" -- "$cur")); return
    fi

    case "$noun" in
        status)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")) ;;
        list|refresh|clean|map|tides) ;;
        verify)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")) ;;
        source)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "list get" -- "$cur"))
            elif [ "$verb" = get ] && [ $npos -eq 0 ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        data)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "status fetch outdated hash verify rm" -- "$cur"))
            elif [ $npos -eq 0 ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        job)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "start stop status log list" -- "$cur"))
            elif [ "$verb" != list ] && [ $npos -eq 0 ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        queue)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "add remove run status stop" -- "$cur"))
            elif [ "$verb" = add ] || [ "$verb" = remove ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        vrt)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "create" -- "$cur"))
            elif [ $npos -eq 0 ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        pyramid)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "create fix" -- "$cur"))
            elif [ $npos -eq 0 ]; then COMPREPLY=($(compgen -W "$(_lmap_ids)" -- "$cur")); fi ;;
        terrain)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "slope viewshed route bestroute water los" -- "$cur")) ;;
        qld)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "fetch project status" -- "$cur")) ;;
        nsw|moon)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "project" -- "$cur")) ;;
        au)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "grid contours project" -- "$cur")) ;;
        sheets)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "index get" -- "$cur")) ;;
        config)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "show" -- "$cur")) ;;
        cache)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "list rm prune pin unpin" -- "$cur")) ;;
        import)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "add list rm" -- "$cur"))
            elif [ "$verb" = add ] && [ $npos -eq 0 ]; then COMPREPLY=($(compgen -f -- "$cur")); fi ;;
        area)
            [ -z "$verb" ] && COMPREPLY=($(compgen -W "create list print export image phone-map viewshed" -- "$cur")) ;;
        export)
            if [ -z "$verb" ]; then COMPREPLY=($(compgen -W "create verify bundle" -- "$cur"))
            elif [ "$verb" != bundle ] && [ $npos -eq 0 ]; then COMPREPLY=($(compgen -d -- "$cur")); fi ;;
    esac
}
complete -F _lmap lmap
