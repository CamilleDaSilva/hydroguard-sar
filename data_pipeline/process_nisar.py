import os
import glob
import json
import time
import h5py
import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.features import shapes
from rasterio.transform import Affine
from scipy.ndimage import uniform_filter, label, find_objects, map_coordinates
os.chdir(os.path.dirname(os.path.abspath(__file__)))

os.makedirs("../public/assets/img", exist_ok=True)
os.makedirs("../public/assets/data", exist_ok=True)

# zone d'étude
LAT_MIN, LAT_MAX = 43.20, 44.20
LON_MIN, LON_MAX = 1.00, 2.20
RES = 0.0008  # résolution du png en degrés (~65 m)
DOWNSAMPLE = 2

# seuils de détection
HV_MAX = -14.5
IRR_DELTA_MIN, IRR_DELTA_MAX = 2.2, 6.5
IRR_HH_MIN, IRR_HH_MAX = -14.0, -6.0
RES_HH_MAX = -16.5
RES_DELTA_MAX = -2.5

t0 = time.time()


def log(msg):
    print(f"[{time.time() - t0:5.0f} s] {msg}", flush=True)


def smooth_db(arr, valid, size=5):
    # moyenne en linéaire sur les pixels valides, puis passage en dB
    num = uniform_filter(np.where(valid, arr, 0), size=size)
    den = uniform_filter(valid.astype(np.float32), size=size)
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    ok = den > 0.5
    out[ok] = 10 * np.log10(np.maximum(num[ok] / den[ok], 1e-10))
    return out


def read_axes(h5_path):
    with h5py.File(h5_path, 'r') as f:
        base = "science/LSAR/GCOV/grids/frequencyA"
        x = f[f"{base}/xCoordinates"][:]
        y = f[f"{base}/yCoordinates"][:]
        epsg = int(f[f"{base}/projection"][()])
    return x, y, epsg


def load_gcov(h5_path, r0, r1, c0, c1):
    with h5py.File(h5_path, 'r') as f:
        base = "science/LSAR/GCOV/grids/frequencyA"
        hhhh = f[f"{base}/HHHH"][r0:r1:DOWNSAMPLE, c0:c1:DOWNSAMPLE].astype(np.float32)
        hvhv = f[f"{base}/HVHV"][r0:r1:DOWNSAMPLE, c0:c1:DOWNSAMPLE].astype(np.float32)

    valid = (hhhh > 0) & (hvhv > 0) & np.isfinite(hhhh) & np.isfinite(hvhv)
    return smooth_db(hhhh, valid), smooth_db(hvhv, valid)


files = sorted(glob.glob("raw_nisar/NISAR_L2_PR_GCOV_*.h5"))
if len(files) < 2:
    raise FileNotFoundError("Il faut 2 fichiers .h5 dans data_pipeline/raw_nisar/")

f_date1, f_date2 = files[0], files[1]
log(f"Date 1 : {os.path.basename(f_date1)}")
log(f"Date 2 : {os.path.basename(f_date2)}")

x_full, y_full, epsg = read_axes(f_date1)
x2, y2, _ = read_axes(f_date2)
if x_full.shape != x2.shape or y_full.shape != y2.shape or not np.allclose(x_full, x2) or not np.allclose(y_full, y2):
    raise ValueError("Les deux scènes n'ont pas la même grille, elles ne sont pas comparables pixel à pixel")

to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

# fenêtre de lecture autour de la zone d'étude (on ne charge pas toute la scène)
edge_lon = np.concatenate([np.linspace(LON_MIN, LON_MAX, 200), np.linspace(LON_MIN, LON_MAX, 200), np.full(200, LON_MIN), np.full(200, LON_MAX)])
edge_lat = np.concatenate([np.full(200, LAT_MIN), np.full(200, LAT_MAX), np.linspace(LAT_MIN, LAT_MAX, 200), np.linspace(LAT_MIN, LAT_MAX, 200)])
ex, ey = to_utm.transform(edge_lon, edge_lat)

cols_ok = np.where((x_full >= ex.min()) & (x_full <= ex.max()))[0]
rows_ok = np.where((y_full >= ey.min()) & (y_full <= ey.max()))[0]
if len(cols_ok) == 0 or len(rows_ok) == 0:
    raise ValueError("La zone d'étude est en dehors de la scène NISAR")

c0, c1 = int(cols_ok[0]), int(cols_ok[-1]) + 1
r0, r1 = int(rows_ok[0]), int(rows_ok[-1]) + 1
x = x_full[c0:c1:DOWNSAMPLE]
y = y_full[r0:r1:DOWNSAMPLE]
log(f"Fenêtre lue : {len(y)} x {len(x)} pixels (au lieu de {len(y_full) // DOWNSAMPLE} x {len(x_full) // DOWNSAMPLE})")

hh_1, hv_1 = load_gcov(f_date1, r0, r1, c0, c1)
log("Date 1 chargée")
hh_2, hv_2 = load_gcov(f_date2, r0, r1, c0, c1)
log("Date 2 chargée")

dx = float(x[1] - x[0])
dy = float(y[1] - y[0])  # négatif en général (nord en haut)
px_ha = abs(dx * dy) / 10_000.0

valid = np.isfinite(hh_1) & np.isfinite(hh_2) & np.isfinite(hv_2)

X_mesh, Y_mesh = np.meshgrid(x, y)
lon_grid, lat_grid = to_ll.transform(X_mesh, Y_mesh)
del X_mesh, Y_mesh
aoi = valid & (lat_grid >= LAT_MIN) & (lat_grid <= LAT_MAX) & (lon_grid >= LON_MIN) & (lon_grid <= LON_MAX)
del lat_grid, lon_grid
log("Masque de la zone d'étude prêt")

# on enlève le décalage moyen entre les deux dates
delta_hh = hh_2 - hh_1
shift = float(np.nanmedian(delta_hh[aoi]))
delta_hh = delta_hh - shift
log(f"Décalage médian retiré : {shift:+.2f} dB")

flat_mask = aoi & (hv_2 < HV_MAX)

irrigated_mask = flat_mask & (delta_hh > IRR_DELTA_MIN) & (delta_hh < IRR_DELTA_MAX) & (hh_2 > IRR_HH_MIN) & (hh_2 < IRR_HH_MAX)
reservoir_mask = flat_mask & (hh_2 < RES_HH_MAX) & (delta_hh < RES_DELTA_MAX)

affine = Affine(dx, 0, x[0] - dx / 2, 0, dy, y[0] - dy / 2)


def vectorize(binary_mask, delta_arr, category, min_px=4, max_px=400, compact=False, max_polygons=300):
    labeled, n = label(binary_mask)
    if n == 0:
        return []

    counts = np.bincount(labeled.ravel())
    objs = find_objects(labeled)

    info = {}
    for i, sl in enumerate(objs, start=1):
        px = int(counts[i])
        if px < min_px or px > max_px:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        # une retenue est un blob compact, pas une ligne
        if compact and (max(h, w) / min(h, w) > 3 or px / (h * w) < 0.35):
            continue
        info[i] = (px, sl)

    ids = sorted(info, key=lambda i: -info[i][0])[:max_polygons]
    if not ids:
        return []

    lut = np.zeros(n + 1, dtype=np.int32)
    lut[ids] = ids
    kept = lut[labeled]

    features = []
    for geom, val in shapes(kept, mask=kept > 0, transform=affine):
        i = int(val)
        px, sl = info[i]
        sub = labeled[sl] == i
        mean_delta = float(np.mean(delta_arr[sl][sub]))

        rings = []
        for ring in geom["coordinates"]:
            xs, ys = zip(*ring)
            lons, lats = to_ll.transform(xs, ys)
            rings.append([[round(float(a), 6), round(float(b), 6)] for a, b in zip(lons, lats)])

        cr = (sl[0].start + sl[0].stop) // 2
        cc = (sl[1].start + sl[1].stop) // 2
        c_lon, c_lat = to_ll.transform(float(x[cc]), float(y[cr]))

        features.append({
            "type": "Feature",
            "properties": {
                "category": category,
                "area_ha": round(px * px_ha, 1),
                "delta_db": round(mean_delta, 1),
                "center_lat": round(float(c_lat), 5),
                "center_lon": round(float(c_lon), 5)
            },
            "geometry": {"type": "Polygon", "coordinates": rings}
        })

    features.sort(key=lambda f: -f["properties"]["area_ha"])
    return features


irr_features = vectorize(irrigated_mask, delta_hh, "irrigation", max_polygons=300)
log(f"{len(irr_features)} parcelles irriguées vectorisées")
res_features = vectorize(reservoir_mask, delta_hh, "reservoir", compact=True, max_polygons=150)
log(f"{len(res_features)} retenues vectorisées")

with open("../public/assets/data/parcels_alert.geojson", "w") as f:
    json.dump({"type": "FeatureCollection", "features": irr_features + res_features}, f)


# raster NISAR sur une grille lat/lon, lignes régulières en Mercator pour coller à Leaflet
def merc(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def unmerc(m):
    return np.degrees(2 * np.arctan(np.exp(m)) - np.pi / 2)


step = np.radians(RES)
lons = np.arange(LON_MIN, LON_MAX, RES)
lats = unmerc(np.arange(merc(LAT_MAX), merc(LAT_MIN), -step))
LON, LAT = np.meshgrid(lons, lats)
X, Y = to_utm.transform(LON, LAT)
cols = (X - x[0]) / dx
rows = (Y - y[0]) / dy


def resample(arr):
    return map_coordinates(arr, [rows, cols], order=0, mode='constant', cval=np.nan)


g1 = resample(hh_1)
g2 = resample(hh_2)
log("Raster rééchantillonné")


def stretch(a, lo=-25, hi=-5):
    return np.clip((a - lo) / (hi - lo), 0, 1)


# R = HH date 2, G et B = HH date 1 : gris = pas de changement, rouge = hausse, cyan = baisse
rgb = np.dstack([stretch(g2), stretch(g1), stretch(g1)])
alpha = (np.isfinite(g1) & np.isfinite(g2)).astype(np.float32)
rgba = np.dstack([np.nan_to_num(rgb), alpha])
Image.fromarray((rgba * 255).astype(np.uint8), "RGBA").save("../public/assets/img/nisar_hh.png", optimize=True)
log("nisar_hh.png écrit")

bounds = [
    [float(unmerc(merc(lats[-1]) - step / 2)), float(lons[0] - RES / 2)],
    [float(unmerc(merc(lats[0]) + step / 2)), float(lons[-1] + RES / 2)]
]

# les 5 cas les plus nets pour le panneau d'alertes
all_sorted = sorted(res_features[:3] + irr_features[:5], key=lambda feat: abs(feat["properties"]["delta_db"]), reverse=True)[:5]
anomalies = []
for idx, feat in enumerate(all_sorted):
    p = feat["properties"]
    is_res = p["category"] == "reservoir"
    anomalies.append({
        "id": f"HYD-0{idx + 1}",
        "lat": p["center_lat"],
        "lon": p["center_lon"],
        "score": round(abs(p["delta_db"]) * 2.5, 1),
        "type": f"Retenue remplie ({p['area_ha']} ha)" if is_res else f"Parcelle irriguée ({p['area_ha']} ha)",
        "detail": f"Variation HH: {p['delta_db']:+.1f} dB | Pluie Meteomatics: 0 mm"
    })

stats = {
    "bounds": bounds,
    "ha_reservoirs": round(sum(f["properties"]["area_ha"] for f in res_features), 1),
    "ha_irrigated": round(sum(f["properties"]["area_ha"] for f in irr_features), 1),
    "anomalies": anomalies
}

with open("../public/assets/data/stats_water.json", "w") as f:
    json.dump(stats, f, indent=2)

log(f"Terminé : {len(res_features)} retenues ({stats['ha_reservoirs']} ha) | {len(irr_features)} parcelles irriguées ({stats['ha_irrigated']} ha)")