#!/usr/bin/env python3
"""A raster reused from a neighbouring area/map (map_reuse) is written with the same options as a built one
(area_raster_co): DEM DEFLATE+predictor, photos WEBP, vegetation nearest overviews, 512 blocks - not plain DEFLATE
(several times bigger photos, nearest-neighbour overviews). Also: wms_fetch warps bilinear."""
import sys as _s, os as _o; _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__))); from _src import lmap_source
import re as _re, importlib.machinery, importlib.util, json, subprocess, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(HERE / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

src = lmap_source()
check('b.get("reused_from") and by[k][0] in ("sat", "aerial", "topo")' in src, "no lossy copy of a copy")
check("area_raster_co(ctx, by[k][0], f)" in src, "map_reuse uses area_raster_co")
check('"-of", "COG", "-co", "COMPRESS=DEFLATE", f, part' not in src, "no plain-DEFLATE reuse left")
check('"gdalwarp", "-q", "-r", "bilinear", "-te_srs"' in src, "wms_fetch clip warps bilinear")

T = Path(tempfile.mkdtemp(prefix="lmap-tco-"))
try:
    from osgeo import gdal
    gdal.UseExceptions()
    def mk(name, bands, typ):
        ds = gdal.GetDriverByName("GTiff").Create(str(T / name), 600, 600, bands, typ)
        ds.SetGeoTransform([146.0, 0.0001, 0, -19.0, 0, -0.0001]); ds.SetProjection("EPSG:4326")
        for b in range(1, bands + 1):
            ds.GetRasterBand(b).Fill(100 + b)
        ds = None
        return T / name
    dem, photo, veg = mk("dem.tif", 1, gdal.GDT_Float32), mk("sat.tif", 3, gdal.GDT_Byte), mk("veg.tif", 1, gdal.GDT_Byte)
    for q in ("standard", "original"):
        ctx = types.SimpleNamespace(webp=True, quality=q)
        def info(kind, f):
            o = T / f"o-{kind}-{q}.tif"
            subprocess.run(["gdal_translate", "-q", "-of", "COG", *L.area_raster_co(ctx, kind, f), str(f), str(o)], check=True)
            j = json.loads(subprocess.run(["gdalinfo", "-json", str(o)], capture_output=True, text=True, check=True).stdout)
            return j["metadata"].get("IMAGE_STRUCTURE", {}), j["bands"][0]["block"]
        m, blk = info("dem", dem)
        check(m.get("COMPRESSION") == "DEFLATE" and m.get("PREDICTOR") == "3" and blk == [512, 512], f"{q}: dem DEFLATE+float predictor, 512 blocks")
        m, _ = info("sat", photo)
        check(m.get("COMPRESSION") == "WEBP", f"{q}: photo WEBP")
        check((m.get("COMPRESSION_REVERSIBILITY") == "LOSSLESS") == (q == "original"), f"{q}: photo lossless only under original")
        m, _ = info("veg", veg)
        check(m.get("COMPRESSION") == "DEFLATE", f"{q}: vegetation DEFLATE")
    ctx = types.SimpleNamespace(webp=True, quality="standard")
    idem = mk("idem.tif", 1, gdal.GDT_Int16)
    check("PREDICTOR=2" in L.area_raster_co(ctx, "dem", idem), "integer DEM: predictor 2")
    check("QUALITY=90" in L.area_raster_co(ctx, "topo", photo), "topo: q90 (crisp text)")
    import glob
    for x in glob.glob(str(HERE / "share" / "*.xml")):
        t = Path(x).read_text()
        if "<GDAL_WMS>" in t:
            check(_re.search(r"<BandsCount>[34]</BandsCount>", t) is not None,
                  f"{Path(x).name}: RGB/RGBA bands (bilinear never blends palette indices)")
    check("OVERVIEW_RESAMPLING=NEAREST" in L.area_raster_co(types.SimpleNamespace(webp=True, quality="standard"), "veg", veg),
          "vegetation classes never averaged")
finally:
    subprocess.run(["rm", "-rf", str(T)])
print("FAILED: " + "; ".join(fails) if fails else "OK")
raise SystemExit(1 if fails else 0)
