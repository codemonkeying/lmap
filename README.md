# lmap

Offline map library for when the internet's gone. One CLI downloads, resumes, shrinks and indexes the datasets in `sources.toml` into one data folder, then builds QGIS projects, printable topo sheets and phone maps from it.

The catalog is Australian (national plus QLD, NSW, SA, TAS, WA layers). The data isn't in the repo, only the recipe.

## Install

```sh
git clone https://github.com/codemonkeying/lmap.git ~/lmap && ~/lmap/install.sh
```

Linux, Python 3.11+, GDAL. `install.sh` checks the rest and `install.sh --deps` installs it. No pip.

## Use

```sh
lmap status                                  # data root, jobs, every dataset
lmap data fetch qld-tides --yes              # small first download
lmap tides --near=-19.26,146.82 --days 3     # offline from here on
lmap job start dem-au-90m --yes              # big downloads run in the background, resumable
lmap area create maggie --center=-19.14,146.84 --radius-km 6 --yes   # QGIS project for any box
lmap map --place "Mount Isa" --scale 50k --yes   # printable topo sheet (PDF)
lmap export create /media/stick --yes        # copy for USB, phone (MBTiles) or ATAK
```

`lmap <command> --help` for the rest.

Built for scripts and agents: every command takes `--json`, `--dry-run` and `--yes`. With `--yes` or `--json` it never prompts (on a terminal without them, changes ask y/N first). Errors are one JSON line on stderr. Exit codes: `0` ok, `1` fail, `2` bad usage, `4` not found, `5` needs `--yes`, `6` network, `75` stopped (resumable), `130` aborted.

## Tests

```sh
tests/run.sh
```

## License

Copyright (c) 2026 codemonkeying. All rights reserved. Each dataset has its own licence, shown by `lmap source get ID`.
