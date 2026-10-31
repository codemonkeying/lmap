#!/usr/bin/env python3
"""t_tms.py through the fetch="gdal" path: GDAL (gdalwarp) fetches the tiles, lmap decodes each block on its own
pool before installing it. Same checks as t_tms.py.
Usage: t_tmsgdal.py <scratch>"""
import os, subprocess, sys
from pathlib import Path
sys.exit(subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "t_tms.py"), *sys.argv[1:]],
                        env={**os.environ, "T_TMS_GDAL": "1"}).returncode)
