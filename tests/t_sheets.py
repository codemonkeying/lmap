#!/usr/bin/env python3
"""sheets: offline. Synthetic index.gpkg (3 series) + a local GeoPDF standing in for a downloaded 250k sheet:
lookup by point/radius/number, CLI contract, 250k map layer cut to the frame, project wiring.
LMAP_TEST_LIVE=1 also runs `sheets index` + one real 250k download (network)."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr, osr

ROOT = Path(__file__).resolve().parent.parent
ld = importlib.machinery.SourceFileLoader("lmap", str(ROOT / "bin" / "lmap"))
L = importlib.util.module_from_spec(importlib.util.spec_from_loader("lmap", ld)); ld.exec_module(L)
gdal.UseExceptions(); ogr.UseExceptions(); osr.UseExceptions()
fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what); fails += not ok


d = Path(tempfile.mkdtemp(prefix="t_sh_", dir=sys.argv[1] if len(sys.argv) > 1 else None))
SD = d / "data" / "australia" / "sheets"; SD.mkdir(parents=True)
env = dict(os.environ, LMAP_TMP=str(d / "tmp"))
lm = [sys.executable, str(ROOT / "bin" / "lmap"), "--json", "--root", str(d)]


def cli(*a):
    c = subprocess.run(lm + list(a), capture_output=True, text=True, env=env)
    try:
        j = json.loads(c.stdout or c.stderr or "{}")
    except ValueError:
        j = {}
    return c.returncode, j, c.stderr


# no index yet -> not found
rc, j, _ = cli("sheets", "get", "--at=-19.3,146.8", "--dry-run")
check(rc == 4, f"no index: exit 4 ({rc})")

# synthetic index: 250k 1.5x1 deg tiles, 100k 0.5 deg, 50k 0.25 deg, around Townsville
ix = SD / "index.gpkg"
ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(ix)); sr = osr.SpatialReference(); sr.ImportFromEPSG(4326)
sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
def series(name, step_x, step_y, rows):
    lay = ds.CreateLayer(name, sr, ogr.wkbMultiPolygon)
    for fn, t in (("number", ogr.OFTString), ("name", ogr.OFTString), ("year", ogr.OFTInteger),
                  ("ecat", ogr.OFTInteger), ("available", ogr.OFTInteger)):
        lay.CreateField(ogr.FieldDefn(fn, t))
    for num, nm, x0, y1, av, *ec in rows:
        f = ogr.Feature(lay.GetLayerDefn())
        for k, v in (("number", num), ("name", nm), ("available", av)):
            f[k] = v
        if not ec:                                     # (an extra None = no eCAT id, no year: no download link)
            f["year"] = 2025; f["ecat"] = 1000 + len(num)
        f.SetGeometry(ogr.CreateGeometryFromWkt(
            f"MULTIPOLYGON((({x0} {y1},{x0 + step_x} {y1},{x0 + step_x} {y1 - step_y},{x0} {y1 - step_y},{x0} {y1})))"))
        lay.CreateFeature(f)
series("s250k", 1.5, 1.0, [("SE55-14", "TOWNSVILLE", 145.5, -19.0, 1), ("SE55-15", "AYR", 147.0, -19.0, 1),
                          ("SE55-10", "INGHAM", 145.5, -18.0, 1)])
series("s100k", 0.5, 0.5, [("8259", "Townsville", 146.5, -19.0, 1), ("8359", "Bowling Green Bay", 147.0, -19.0, 0)])
series("s50k", 0.25, 0.25, [("8259-2", "Townsville", 146.75, -19.0, 1), ("8259-9", "Nowhere", 146.5, -19.0, 1, None)])
ds.SetMetadataItem("lmap_key", L.SHEETS_VER); ds = None

rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--dry-run")
check(rc == 0 and [w["number"] for w in j.get("sheets", [])] == ["SE55-14"], f"point -> its 250k sheet ({j.get('sheets')})")
rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--radius-km", "60", "--dry-run")
nums = [w["number"] for w in j.get("sheets", [])]
check(rc == 0 and nums[0] == "SE55-14" and set(nums) == {"SE55-14", "SE55-15", "SE55-10"}, f"60 km: 3 sheets, nearest first ({nums})")
# west of Ingham: a 22 km circle's search box takes in Townsville's corner (24.5 km away); only Ingham (17.9 km) is in
rc, j, _ = cli("sheets", "get", "--at=-18.85,145.33", "--radius-km", "22", "--dry-run")
check(rc == 0 and [w["number"] for w in j["sheets"]] == ["SE55-10"],
      f"true circle: sheets only in the search box corner are left out ({[w['number'] for w in j.get('sheets', [])]})")
rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--radius-km", "60", "--scale", "250k,100k,50k", "--dry-run")
ws = {w["number"]: w for w in j.get("sheets", [])}
check(rc == 0 and ws["8259"]["file"].endswith("100k/8259_Townsville.tif") and ws["8259-2"]["file"].endswith("50k/8259_2_Townsville.tif")
      and ws["SE55-14"]["layer"].endswith("250k/SE55_14_TOWNSVILLE.cog.tif"), "file names per series")
check(not ws["8359"]["available"] and j["to_get"] == 6, f"unpublished sheet not counted to get ({j.get('to_get')})")
check(j["est_mb"] == 3 * 35 + 315 + 2 * 315, f"size estimate ({j.get('est_mb')})")
# distance in ground km: a point 0.1 deg (lon) east of a sheet's edge at 19S is ~10.5 km, not 11.1
d_ = [w for w in L.sheets_find(type("C", (), {"path": lambda self, x: d / x})(), "50k", -19.1, 147.1, 30)
      if w["number"] == "8259-2"]
check(d_ and abs(d_[0]["dist_km"] - 0.1 * 111.32 * 0.9455) < 0.3, f"distance in ground km ({d_ and d_[0]['dist_km']})")
rc, j, _ = cli("sheets", "get", "--sheet", "SE55-15", "--dry-run")
check(rc == 0 and [w["number"] for w in j["sheets"]] == ["SE55-15"], "--sheet by number")
rc, j, _ = cli("sheets", "get", "--sheet", "NOPE-1", "--dry-run")
check(rc == 4, f"unknown sheet: exit 4 ({rc})")
rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--sheet", "SE55-15", "--dry-run")
check(rc == 2, f"--at and --sheet together: exit 2 ({rc})")
rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--scale", "25k", "--dry-run")
check(rc == 2, f"bad --scale: exit 2 ({rc})")
rc, j, _ = cli("sheets", "get", "--at=-19.25,146.8", "--radius-km", "999", "--dry-run")
check(rc == 2, f"--radius-km out of range: exit 2 ({rc})")
rc, j, _ = cli("sheets", "get", "--at=-30,100", "--dry-run")
check(rc == 4, f"point outside every sheet: exit 4 ({rc})")
rc, j, _ = cli("sheets", "get", "--sheet", "SE55-14")
check(rc == 5, f"without --yes: exit 5 ({rc})")
check(not list(SD.rglob("*.pdf")) and not (SD / "250k").exists(), "dry-run / refused write nothing")
rc, j, _ = cli("sheets", "index", "--dry-run")
check(rc == 0 and j.get("build") is False, f"index present + current: no rebuild ({j.get('build')})")

# a 'downloaded' GeoPDF (MGA 55, frame 2 km inside the page): get makes the map layer without the network
(SD / "250k").mkdir()
src = d / "src.tif"; W = H = 400
t = gdal.GetDriverByName("GTiff").Create(str(src), W, H, 3, gdal.GDT_Byte)
s55 = osr.SpatialReference(); s55.ImportFromEPSG(7855)
t.SetGeoTransform((480000, 50, 0, 7880000, 0, -50)); t.SetProjection(s55.ExportToWkt())
for b in range(3):
    t.GetRasterBand(b + 1).WriteArray(np.full((H, W), 60 + 60 * b, "uint8"))
t = None
nl = "POLYGON((482000 7862000,482000 7878000,498000 7878000,498000 7862000,482000 7862000))"
pdf = SD / "250k" / "SE55_14_TOWNSVILLE.pdf"
gdal.Translate(str(pdf), str(src), format="PDF", creationOptions=["DPI=72", f"NEATLINE={nl}"])
rc, j, err = cli("sheets", "get", "--sheet", "SE55-14", "--yes")
lay = SD / "250k" / "SE55_14_TOWNSVILLE.cog.tif"
check(rc == 0 and lay.is_file() and j.get("got") == ["SE55-14"], f"existing PDF -> map layer, no download ({rc} {err[-200:]})")
if lay.is_file():
    g = gdal.Open(str(lay)); gt = g.GetGeoTransform()
    x0, y1 = gt[0], gt[3]; x1, y0 = x0 + gt[1] * g.RasterXSize, y1 + gt[5] * g.RasterYSize
    check(abs(x0 - 482000) < 30 and abs(x1 - 498000) < 30 and abs(y0 - 7862000) < 30 and abs(y1 - 7878000) < 30,
          f"cut to the frame ({x0:.0f},{y0:.0f} - {x1:.0f},{y1:.0f})")
    # 254 dpi of the page (a GA 250k sheet comes out at 25.0 m; this synthetic page at ~14 m)
    mk_ = g.GetRasterBand(1).GetMaskBand().ReadAsArray()
    check(5 < gt[1] < 50 and g.RasterCount in (3, 4) and g.GetRasterBand(1).GetMaskFlags() & gdal.GMF_PER_DATASET
          and mk_[mk_.shape[0] // 2, mk_.shape[1] // 2] == 255
          and g.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG",
          f"RGB + mask/alpha (transparent outside the frame), COG ({gt[1]:.2f} m, {g.RasterCount} bands)")
    g = None
rc, j, _ = cli("sheets", "get", "--sheet", "SE55-14", "--yes")
check(rc == 0 and j.get("unchanged"), "second run: nothing to do")
check(not list(d.rglob("*.part*")), "no part files left")

# no download link: exit 6, but the JSON still says what was saved / missing
rc, j, err = cli("sheets", "get", "--sheet", "8259-9", "--scale", "50k", "--yes")
check(rc == 6 and j.get("missing") == ["8259-9"] and j.get("got") == [] and j.get("ok") is False,
      f"no link: exit 6, result kept ({rc} {str(j)[:150]})")

# catalogue answers: list or single-dict fields, a wrong-id hit, junk
class R:
    def __init__(self, b): self.b = b
    def read(self, n=-1): return self.b if n < 0 else self.b[:n]   # like http.client: read(amt)
    def __enter__(self): return self
    def __exit__(self, *a): return False
ans = {}
L.OPENER = type("O", (), {"open": lambda self, rq, timeout=None: R(ans["b"])})()
cx = type("C", (), {"path": lambda self, x: d / "cat" / x})()
hit = lambda ids, links: {"_source": {"resourceIdentifier": ids, "link": links}}
cf = "https://d28rz98at9flks.cloudfront.net/"
for nm_, body, want in (
        ("list fields", {"hits": {"hits": [hit([{"code": "https://pid.geoscience.gov.au/dataset/ga/111"}],
                                               [{"urlObject": {"default": "http://shop/x"}}, {"urlObject": {"default": cf + "111/a.tif"}}])]}}, cf + "111/a.tif"),
        ("single-dict fields", {"hits": {"hits": [hit({"code": "https://pid.geoscience.gov.au/dataset/ga/222"},
                                                     {"urlObject": {"default": cf + "222/b.pdf"}})]}}, cf + "222/b.pdf"),
        ("other id only", {"hits": {"hits": [hit([{"code": "https://pid.geoscience.gov.au/dataset/ga/3339"}],
                                                 [{"urlObject": {"default": cf + "3339/c.tif"}}])]}}, None)):
    ans["b"] = json.dumps(body).encode(); e_ = {"111": 111, "222": 222, "other id only": 333}.get(want and want.split("/")[-2], 333)
    try:
        u = L.sheet_url(cx, e_)
    except Exception as x:
        u = f"raised {x}"
    check(u == want, f"catalogue: {nm_} ({u})")
ans["b"] = b"<html>error</html>"
try:
    L.sheet_url(cx, 444); ok_ = False
except L.CliError as x:
    ok_ = x.code == L.E_UPSTREAM
check(ok_, "catalogue: junk answer -> upstream error, not a traceback")
check(json.loads((d / "cat" / "data" / "australia" / "sheets" / "urls.json").read_text()) == {"111": cf + "111/a.tif", "222": cf + "222/b.pdf"},
      "found links cached, misses not")

# 100k/50k scan fit: a synthetic 200 dpi 1:50k scan of 8259-2 (white page, 3 px black frame, map 'ink' inside,
# a label touching the frame, slight rotation) -> the layer lands on MGA where the AGD66 corners say
SC = SD / "50k"; SC.mkdir(exist_ok=True)
box = (146.75, -19.25, 147.0, -19.0); dpi = 200; pxm = dpi / 0.0254 / 50000
pw = 0.25 * 111320 * np.cos(np.radians(-19.125)) * pxm; ph = 0.25 * 110574 * pxm
M0 = 250
amg = osr.SpatialReference(); amg.ImportFromEPSG(20255); a66 = osr.SpatialReference(); a66.ImportFromEPSG(4202)
for s_ in (amg, a66):
    s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
ta = osr.CoordinateTransformation(a66, amg)
K = [ta.TransformPoint(lo, la)[:2] for lo, la in ((146.75, -19.0), (147.0, -19.0), (147.0, -19.25), (146.75, -19.25))]
ex0, ny0 = min(k[0] for k in K), max(k[1] for k in K)
Q = [((x - ex0) * pxm + M0, (ny0 - y) * pxm + M0) for x, y in K]     # the frame on the paper (AMG), NW NE SE SW
Wp = int(max(q[0] for q in Q) + M0); Hp = int(max(q[1] for q in Q) + M0)
rot = 0.002                                            # radians: a scan fed a touch crooked
c0x, c0y = Wp / 2, Hp / 2
page = np.full((Hp, Wp), 255, "uint8")
yy, xx = np.mgrid[0:Hp, 0:Wp]
ux = c0x + (xx - c0x) * np.cos(rot) + (yy - c0y) * np.sin(rot); uy = c0y - (xx - c0x) * np.sin(rot) + (yy - c0y) * np.cos(rot)
fr = np.zeros((Hp, Wp), bool); sd = []
for (x1, y1), (x2, y2) in zip(Q, Q[1:] + Q[:1]):
    L_ = np.hypot(x2 - x1, y2 - y1); tx, ty = (x2 - x1) / L_, (y2 - y1) / L_
    along = (ux - x1) * tx + (uy - y1) * ty; dist = (ux - x1) * ty - (uy - y1) * tx   # <0 = inside (clockwise, y down)
    fr |= (np.abs(dist) < 1.6) & (along > -2) & (along < L_ + 2); sd.append(dist)
page[fr] = 0
inside = (sd[0] < -2) & (sd[1] < -2) & (sd[2] < -2) & (sd[3] < -2)
page[inside & ((xx // 41 + yy // 67) % 3 == 0)] = 170                   # coloured fill (not black ink)
page[inside & ((xx + yy) % 61 < 2)] = 30                               # thin black ink, not along rows/cols
page[(sd[0] > 4) & (sd[0] < 40) & (ux > Q[0][0] + 900) & (ux < Q[0][0] + 1300)] = 20   # a title block touching the frame
del yy, xx, ux, uy, fr, inside, sd
scan = SC / "8259_2_Townsville.tif"
t = gdal.GetDriverByName("GTiff").Create(str(scan), Wp, Hp, 3, gdal.GDT_Byte, ["COMPRESS=DEFLATE"])
for b in range(3):
    t.GetRasterBand(b + 1).WriteArray(page)
t.SetMetadata({"TIFFTAG_XRESOLUTION": str(dpi), "TIFFTAG_YRESOLUTION": str(dpi), "TIFFTAG_RESOLUTIONUNIT": "2 (pixels/inch)"})
t = None; page = None
g = gdal.Open(str(scan)); check(L.sheet_scan_dpi(g) == dpi, f"scan dpi from tags ({L.sheet_scan_dpi(g)})"); g = None
cm = d / "cm.tif"; t = gdal.GetDriverByName("GTiff").Create(str(cm), 4, 4, 3)
t.SetMetadata({"TIFFTAG_XRESOLUTION": "157.48", "TIFFTAG_RESOLUTIONUNIT": "3 (pixels/cm)"}); t = None
g = gdal.Open(str(cm)); check(abs(L.sheet_scan_dpi(g) - 400) < 0.1, f"px/cm tag -> 400 dpi ({L.sheet_scan_dpi(g)})"); g = None
rc, j, err = cli("sheets", "get", "--sheet", "8259-2", "--scale", "50k", "--yes")
lay = SC / "8259_2_Townsville.cog.tif"
fit = (j.get("sheets") or [{}])[0].get("fit") or {}
check(rc == 0 and lay.is_file() and j.get("got") == ["8259-2"], f"existing scan -> fitted layer, no download ({rc} {j.get('failed')} {j.get('missing')})")
check(fit.get("epsg") == 7855 and fit.get("res_m") == 2.5 and fit.get("corner_fit_m", 99) < 1,
      f"fit stats: MGA55 2.5 m, corners exact ({fit})")
if lay.is_file():
    agd = osr.SpatialReference(); agd.ImportFromEPSG(4202); m55 = osr.SpatialReference(); m55.ImportFromEPSG(7855)
    g94 = osr.SpatialReference(); g94.ImportFromEPSG(4283)
    for s_ in (agd, m55, g94):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    o = osr.CoordinateTransformationOptions(); o.SetOperation("urn:ogc:def:coordinateOperation:EPSG::1278")
    t1 = osr.CoordinateTransformation(agd, g94, o); t2 = osr.CoordinateTransformation(g94, m55)
    nw = t2.TransformPoint(*t1.TransformPoint(146.75, -19.0)[:2])[:2]
    se = t2.TransformPoint(*t1.TransformPoint(147.0, -19.25)[:2])[:2]
    t0 = osr.CoordinateTransformation(agd, m55); nw0 = t0.TransformPoint(146.75, -19.0)[:2]
    check(np.hypot(nw[0] - nw0[0], nw[1] - nw0[1]) > 150, f"AGD66 shift applied, not a no-op ({np.hypot(nw[0]-nw0[0], nw[1]-nw0[1]):.0f} m)")
    g = gdal.Open(str(lay)); gt = g.GetGeoTransform()
    x0, y1 = gt[0], gt[3]; x1, y0 = x0 + gt[1] * g.RasterXSize, y1 + gt[5] * g.RasterYSize
    check(abs(x0 - min(nw[0], se[0])) < 60 and abs(y1 - nw[1]) < 60 and abs(x1 - se[0]) < 60 and abs(y0 - se[1]) < 60,
          f"layer on MGA where the AGD66 corners say ({x0:.0f},{y0:.0f} - {x1:.0f},{y1:.0f} vs {nw[0]:.0f},{se[1]:.0f} - {se[0]:.0f},{nw[1]:.0f})")
    # the frame line itself must land on the MGA corner: sample just inside/outside the NW corner (alpha)
    mb = g.GetRasterBand(1); a = mb.GetMaskBand().ReadAsArray()
    check(g.RasterCount in (3, 4) and mb.GetMaskFlags() & gdal.GMF_PER_DATASET and a[0, 0] == 0
          and a[a.shape[0] // 2, a.shape[1] // 2] == 255 and g.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG",
          f"photo COG, transparent outside the frame ({g.RasterCount} bands)")
    g = None
rc, j, _ = cli("sheets", "get", "--sheet", "8259-2", "--scale", "50k", "--yes")
check(rc == 0 and j.get("unchanged"), "fitted: second run nothing to do")
# an RGBA scan (alpha 255 everywhere): the alpha band must not hide the black ink
rgba = d / "rgba.tif"
src_ = gdal.Open(str(scan)); px_ = src_.GetRasterBand(1).ReadAsArray()
g = gdal.GetDriverByName("GTiff").Create(str(rgba), src_.RasterXSize, src_.RasterYSize, 4, gdal.GDT_Byte,
                                         ["COMPRESS=DEFLATE", "ALPHA=YES"]); src_ = None
g.SetMetadata({"TIFFTAG_XRESOLUTION": str(dpi), "TIFFTAG_YRESOLUTION": str(dpi), "TIFFTAG_RESOLUTIONUNIT": "2 (pixels/inch)"})
for b_ in range(3):
    g.GetRasterBand(b_ + 1).WriteArray(px_)
g.GetRasterBand(4).Fill(255); g = None; px_ = None
os.replace(rgba, scan); lay.unlink()
rc, j, err = cli("sheets", "get", "--sheet", "8259-2", "--scale", "50k", "--yes")
check(rc == 0 and lay.is_file(), f"RGBA scan fitted ({rc} {j.get('failed')})")
rc, j, _ = cli("sheets", "get", "--at=-19.1,146.9", "--scale", "50k,250k", "--dry-run")
check(j.get("est_mb") == 0, f"have the scan + layer: nothing to download ({j.get('est_mb')})")
# a scan with no frame: refused, print file kept, the next run retries
lay.unlink(); blank = gdal.Open(str(scan), gdal.GA_Update)
for b in range(3):
    blank.GetRasterBand(b + 1).Fill(255)
blank = None
rc, j, err = cli("sheets", "get", "--sheet", "8259-2", "--scale", "50k", "--yes")
check(rc != 0 and [f["number"] for f in j.get("failed", [])] == ["8259-2"] and scan.is_file() and not lay.exists(),
      f"no frame: failed, scan kept, no layer ({rc} {str(j.get('failed'))[:120]})")
check(not list(d.rglob("*.part*")) and not list((d / "tmp").rglob("*.vrt")), "no part/vrt files left")

# project wiring is checked on the real root (au project needs the national DEM): see USAGE / the render check

if os.environ.get("LMAP_TEST_LIVE"):
    ix.unlink()
    rc, j, err = cli("sheets", "index", "--yes")
    check(rc == 0 and j["sheets"]["250k"] > 500 and j["sheets"]["100k"] > 2900 and j["sheets"]["50k"] > 2600,
          f"live index ({rc} {j.get('sheets')})")
    for x in (SD / "250k").glob("SE55_15*"):
        x.unlink()
    rc, j, err = cli("sheets", "get", "--sheet", "SE55-15", "--yes")
    check(rc == 0 and (SD / "250k" / "SE55_15_AYR.cog.tif").is_file(), f"live 250k download + layer ({rc} {err[-200:]})")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
