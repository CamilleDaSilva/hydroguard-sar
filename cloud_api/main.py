import os
import tempfile
import asf_search as asf
import h5py
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pyproj import Transformer
from scipy.ndimage import uniform_filter, label, find_objects

app = FastAPI(title="HydroGuard SAR — Cloud NISAR On-Demand API")

# Autorise les appels depuis votre site Netlify ou localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache en mémoire pour éviter de recalculer deux fois la même zone
RESULTS_CACHE = {}

def process_window_from_h5(h5_path, target_lat, target_lon, radius_deg=0.18, downsample=2):
    with h5py.File(h5_path, "r") as f:
        base = "science/LSAR/GCOV/grids/frequencyA"
        x_coords = f[f"{base}/xCoordinates"][::downsample]
        y_coords = f[f"{base}/yCoordinates"][::downsample]
        epsg = int(f[f"{base}/projection"][()])

        transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        X_mesh, Y_mesh = np.meshgrid(x_coords, y_coords)
        raw_1, raw_2 = transformer.transform(X_mesh, Y_mesh)
        lat_grid, lon_grid = (raw_1, raw_2) if np.mean(np.abs(raw_1)) > 30 else (raw_2, raw_1)

        # Découpe uniquement la fenêtre locale autour du clic (rapide et léger en RAM)
        row_mask = np.any((lat_grid >= target_lat - radius_deg) & (lat_grid <= target_lat + radius_deg), axis=1)
        col_mask = np.any((lon_grid >= target_lon - radius_deg) & (lon_grid <= target_lon + radius_deg), axis=0)

        if not np.any(row_mask) or not np.any(col_mask):
            return None

        r_idx = np.where(row_mask)[0]
        c_idx = np.where(col_mask)[0]
        r0, r1 = int(r_idx[0]), int(r_idx[-1]) + 1
        c0, c1 = int(c_idx[0]), int(c_idx[-1]) + 1

        hhhh = f[f"{base}/HHHH"][::downsample, ::downsample][r0:r1, c0:c1]
        hvhv = f[f"{base}/HVHV"][::downsample, ::downsample][r0:r1, c0:c1]
        lat_sub = lat_grid[r0:r1, c0:c1]
        lon_sub = lon_grid[r0:r1, c0:c1]

    valid = (hhhh > 0) & (hvhv > 0) & np.isfinite(hhhh) & np.isfinite(hvhv)
    hhhh_s = uniform_filter(np.where(valid, hhhh, 0), size=5)
    hvhv_s = uniform_filter(np.where(valid, hvhv, 0), size=5)

    hh_db = np.where(valid & (hhhh_s > 0), 10 * np.log10(np.maximum(hhhh_s, 1e-10)), -30)
    hv_db = np.where(valid & (hvhv_s > 0), 10 * np.log10(np.maximum(hvhv_s, 1e-10)), -30)

    dx = abs(float(x_coords[1] - x_coords[0]))
    dy = abs(float(y_coords[1] - y_coords[0]))
    px_ha = (dx * dy) / 10_000.0

    return hh_db, hv_db, valid, lat_sub, lon_sub, px_ha

def mask_to_features(binary_mask, delta_arr, lat_grid, lon_grid, px_ha, category, max_polygons=150):
    labeled, _ = label(binary_mask)
    slices = find_objects(labeled)
    features = []
    candidates = []

    for idx, sl in enumerate(slices):
        if sl is None:
            continue
        sub = (labeled[sl] == (idx + 1))
        px_count = int(np.sum(sub))
        compactness = px_count / max(sub.shape[0] * sub.shape[1], 1)
        if 6 <= px_count <= 300 and compactness > 0.35:
            candidates.append((px_count, idx, sl))

    candidates.sort(reverse=True)
    for px_count, idx, sl in candidates[:max_polygons]:
        r0, r1 = sl[0].start, sl[0].stop
        c0, c1 = sl[1].start, sl[1].stop
        sub_mask = (labeled[sl] == (idx + 1))
        mean_delta = float(np.mean(delta_arr[sl][sub_mask]))
        area_ha = round(px_count * px_ha, 1)

        r_ind, c_ind = np.where(sub_mask)
        cr = int(round(r0 + np.mean(r_ind)))
        cc = int(round(c0 + np.mean(c_ind)))

        left_pts, right_pts = [], []
        for r in range(r0, r1):
            cols = np.where(labeled[r, c0:c1] == (idx + 1))[0] + c0
            if len(cols) > 0:
                left_pts.append([round(float(lon_grid[r, cols[0]]), 6), round(float(lat_grid[r, cols[0]]), 6)])
                right_pts.append([round(float(lon_grid[r, cols[-1]]), 6), round(float(lat_grid[r, cols[-1]]), 6)])

        if len(left_pts) >= 2:
            ring = left_pts + right_pts[::-1] + [left_pts[0]]
            features.append({
                "type": "Feature",
                "properties": {
                    "category": category,
                    "area_ha": area_ha,
                    "delta_db": round(mean_delta, 1),
                    "center_lat": round(float(lat_grid[cr, cc]), 5),
                    "center_lon": round(float(lon_grid[cr, cc]), 5)
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]}
            })
    return features

@app.get("/analyze")
def analyze_location(lat: float, lon: float):
    cache_key = f"{round(lat, 2)}_{round(lon, 2)}"
    if cache_key in RESULTS_CACHE:
        return RESULTS_CACHE[cache_key]

    # 1. Recherche des 2 dernières scènes NISAR L2 GCOV sur ASF Vertex
    wkt_point = f"POINT({lon} {lat})"
    results = asf.search(
        platform=asf.PLATFORM.NISAR,
        processingLevel="GCOV",
        intersectsWith=wkt_point,
        maxResults=6
    )

    if len(results) < 2:
        raise HTTPException(status_code=404, detail="Moins de 2 passages NISAR GCOV disponibles sur ce secteur.")

    # Prend la scène la plus récente et une scène antérieure
    scene_latest = results[0]
    scene_baseline = results[-1]

    earthdata_token = os.environ.get("EARTHDATA_TOKEN")
    if not earthdata_token:
        raise HTTPException(status_code=500, detail="Variable EARTHDATA_TOKEN manquante sur Cloud Run.")

    session = asf.ASFSession().auth_with_token(earthdata_token)

    with tempfile.TemporaryDirectory() as tmpdir:
        scene_baseline.download(path=tmpdir, session=session)
        scene_latest.download(path=tmpdir, session=session)

        h5_1 = os.path.join(tmpdir, scene_baseline.properties["fileName"])
        h5_2 = os.path.join(tmpdir, scene_latest.properties["fileName"])

        d1 = process_window_from_h5(h5_1, lat, lon)
        d2 = process_window_from_h5(h5_2, lat, lon)

        if d1 is None or d2 is None:
            raise HTTPException(status_code=404, detail="Coordonnées hors emprise valide.")

        hh_1, hv_1, val_1, lat_g, lon_g, px_ha = d1
        hh_2, hv_2, val_2, _, _, _ = d2

        min_r = min(hh_1.shape[0], hh_2.shape[0])
        min_c = min(hh_1.shape[1], hh_2.shape[1])
        hh_1, hh_2 = hh_1[:min_r, :min_c], hh_2[:min_r, :min_c]
        hv_2 = hv_2[:min_r, :min_c]
        valid = val_1[:min_r, :min_c] & val_2[:min_r, :min_c] & (hv_2 < -15.5)
        lat_g, lon_g = lat_g[:min_r, :min_c], lon_g[:min_r, :min_c]

        delta_hh = hh_2 - hh_1
        irr_mask = valid & (delta_hh > 2.3) & (delta_hh < 6.0) & (hh_2 > -13.5) & (hh_2 < -6.5)
        res_mask = valid & (hh_2 < -17.0) & ((hh_1 - hh_2) > 2.8)

        irr_features = mask_to_features(irr_mask, delta_hh, lat_g, lon_g, px_ha, "irrigation")
        res_features = mask_to_features(res_mask, delta_hh, lat_g, lon_g, px_ha, "reservoir")

        all_sorted = sorted(res_features[:3] + irr_features[:5], key=lambda f: abs(f["properties"]["delta_db"]), reverse=True)[:5]
        anomalies = [
            {
                "id": f"HYD-0{i+1}",
                "lat": f["properties"]["center_lat"],
                "lon": f["properties"]["center_lon"],
                "score": round(abs(f["properties"]["delta_db"]) * 2.5, 1),
                "type": f"Retenue remplie ({f['properties']['area_ha']} ha)" if f["properties"]["category"] == "reservoir" else f"Parcelle irriguée ({f['properties']['area_ha']} ha)",
                "detail": f"Variation HH: {f['properties']['delta_db']:+.1f} dB | API Cloud NISAR"
            }
            for i, f in enumerate(all_sorted)
        ]

        payload = {
            "stats": {
                "bounds": [[float(lat_g.min()), float(lon_g.min())], [float(lat_g.max()), float(lon_g.max())]],
                "ha_reservoirs": round(sum(f["properties"]["area_ha"] for f in res_features), 1),
                "ha_irrigated": round(sum(f["properties"]["area_ha"] for f in irr_features), 1),
                "anomalies": anomalies
            },
            "geojson": {
                "type": "FeatureCollection",
                "features": irr_features + res_features
            }
        }
        RESULTS_CACHE[cache_key] = payload
        return payload