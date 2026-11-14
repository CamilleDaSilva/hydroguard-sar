import os
import io
import glob
import base64
import requests
import numpy as np
import h5py
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image
from pyproj import Transformer
from rasterio.features import shapes
from rasterio.transform import Affine
from rasterio.windows import from_bounds
import rasterio
from scipy.ndimage import uniform_filter, label, find_objects, map_coordinates, binary_opening
import earthaccess
import pystac_client
import planetary_computer

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

METEO_USER = os.getenv("METEO_USER", "")
METEO_PASS = os.getenv("METEO_PASS", "")

try:
    earthaccess.login()
except Exception:
    pass


class AnalyzeRequest(BaseModel):
    lat: float
    lon: float
    name: str = "Zone personnalisée"
    radius_deg: float = 0.22
    start_date: str = "2026-07-01"
    end_date: str = "2026-09-05"


def get_weather_proof(lat: float, lon: float, date_start_str: str, date_end_str: str):
    """Récupère les précipitations réelles exactement entre les deux dates satellites."""
    if METEO_USER:
        try:
            url = f"https://api.meteomatics.com/{date_start_str}T00:00:00Z--{date_end_str}T00:00:00Z:P1D/precip_24h:mm/{lat:.4f},{lon:.4f}/json"
            r = requests.get(url, auth=(METEO_USER, METEO_PASS), timeout=8)
            r.raise_for_status()
            dates = r.json()["data"][0]["coordinates"][0]["dates"]
            daily = [float(d["value"]) for d in dates]
            step = max(1, len(daily) // 5)
            weekly = [round(sum(daily[i:i+step]), 1) for i in range(0, len(daily), step)][:5]
            return weekly, round(sum(daily), 1), "Meteomatics API"
        except Exception:
            pass

    try:
        url = (
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&start_date={date_start_str}&end_date={date_end_str}"
            f"&daily=precipitation_sum&timezone=UTC"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        daily = [float(v or 0.0) for v in r.json()["daily"]["precipitation_sum"]]
        step = max(1, len(daily) // 5)
        weekly = [round(sum(daily[i:i+step]), 1) for i in range(0, len(daily), step)]
        while len(weekly) < 5:
            weekly.append(0.0)
        return weekly[:5], round(sum(daily), 1), "Open-Meteo Archive API"
    except Exception:
        return [0.0, 0.0, 0.0, 0.0, 0.0], 0.0, "Météo indisponible"


def extract_date_from_nisar_name(filename: str):
    for part in os.path.basename(filename).split("_"):
        if len(part) >= 8 and part[:8].isdigit() and part.startswith("202"):
            d = part[:8]
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return None


def try_nisar_api_or_local(lon_min, lat_min, lon_max, lat_max, start_date: str, end_date: str):
    DOWNSAMPLE = 1

    def extract_from_h5_pair(f1, f2, d1_str, d2_str):
        with h5py.File(f1, 'r') as f:
            base = "science/LSAR/GCOV/grids/frequencyA"
            x_full = f[f"{base}/xCoordinates"][:]
            y_full = f[f"{base}/yCoordinates"][:]
            epsg = int(f[f"{base}/projection"][()])

        to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

        ex, ey = to_utm.transform(
            np.array([lon_min, lon_max, lon_min, lon_max]),
            np.array([lat_min, lat_min, lat_max, lat_max])
        )
        cols_ok = np.where((x_full >= ex.min()) & (x_full <= ex.max()))[0]
        rows_ok = np.where((y_full >= ey.min()) & (y_full <= ey.max()))[0]
        if len(cols_ok) < 30 or len(rows_ok) < 30:
            return None

        c0, c1 = int(cols_ok[0]), int(cols_ok[-1]) + 1
        r0, r1 = int(rows_ok[0]), int(rows_ok[-1]) + 1
        x = x_full[c0:c1:DOWNSAMPLE]
        y = y_full[r0:r1:DOWNSAMPLE]

        def read_band(h5_obj):
            with h5py.File(h5_obj, 'r') as f:
                base = "science/LSAR/GCOV/grids/frequencyA"
                hhhh = f[f"{base}/HHHH"][r0:r1:DOWNSAMPLE, c0:c1:DOWNSAMPLE].astype(np.float32)
                hvhv = f[f"{base}/HVHV"][r0:r1:DOWNSAMPLE, c0:c1:DOWNSAMPLE].astype(np.float32)
            valid = (hhhh > 0) & (hvhv > 0) & np.isfinite(hhhh) & np.isfinite(hvhv)
            den = uniform_filter(valid.astype(np.float32), size=3)
            num_h = uniform_filter(np.where(valid, hhhh, 0), size=3)
            num_v = uniform_filter(np.where(valid, hvhv, 0), size=3)
            hh_db = np.where(den > 0.5, 10 * np.log10(np.maximum(num_h / np.maximum(den, 1e-6), 1e-10)), np.nan)
            hv_db = np.where(den > 0.5, 10 * np.log10(np.maximum(num_v / np.maximum(den, 1e-6), 1e-10)), np.nan)
            return hh_db, hv_db

        hh_1, _ = read_band(f1)
        hh_2, hv_2 = read_band(f2)
        return x, y, float(x[1] - x[0]), float(y[1] - y[0]), hh_1, hh_2, hv_2, to_utm, to_ll, d1_str, d2_str

    # 1. Recherche en direct sur NASA Earthdata NISAR dans la fenêtre de dates choisie
    try:
        granules = earthaccess.search_data(
            short_name="NISAR_L2_PR_GCOV",
            bounding_box=(lon_min, lat_min, lon_max, lat_max),
            temporal=(start_date, end_date),
            count=20
        )
        if len(granules) >= 2:
            granules = sorted(granules, key=lambda g: g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"])
            g1, g2 = granules[0], granules[-1]
            t1_hour = g1["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][11:13]
            for g in reversed(granules[1:]):
                if g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][11:13] == t1_hour:
                    g2 = g
                    break

            d1 = g1["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][:10]
            d2 = g2["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][:10]
            opened = earthaccess.open([g1, g2])
            res = extract_from_h5_pair(opened[0], opened[1], d1, d2)
            if res is not None:
                return f"NISAR Bande L ({d1} → {d2})", res
    except Exception:
        pass

    # 2. Fichiers locaux de secours s'ils sont présents
    local_files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "raw_nisar/NISAR_L2_PR_GCOV_*.h5")))
    if len(local_files) >= 2:
        d1 = extract_date_from_nisar_name(local_files[0]) or "2026-07-10"
        d2 = extract_date_from_nisar_name(local_files[1]) or "2026-08-27"
        res = extract_from_h5_pair(local_files[0], local_files[1], d1, d2)
        if res is not None:
            return f"NISAR Bande L ({d1} → {d2})", res

    return None, None


def fallback_sentinel1_global(lon_min, lat_min, lon_max, lat_max, start_date: str, end_date: str):
    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=["sentinel-1-rtc"],
        bbox=[lon_min, lat_min, lon_max, lat_max],
        datetime=f"{start_date}/{end_date}",
        limit=15,
    )
    items = sorted(list(search.items()), key=lambda it: it.datetime)
    if len(items) < 2:
        raise ValueError(f"Moins de 2 passages satellites entre {start_date} et {end_date} sur cette zone.")

    item1, item2 = items[0], items[-1]
    for it in reversed(items[1:]):
        if it.properties.get("sat:relative_orbit") == item1.properties.get("sat:relative_orbit"):
            item2 = it
            break

    d1, d2 = item1.datetime.strftime("%Y-%m-%d"), item2.datetime.strftime("%Y-%m-%d")

    def read_s1_window(item):
        with rasterio.open(item.assets["vv"].href) as src_vv, rasterio.open(item.assets["vh"].href) as src_vh:
            epsg = src_vv.crs.to_epsg()
            to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
            to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

            ex, ey = to_utm.transform(
                np.array([lon_min, lon_max, lon_min, lon_max]),
                np.array([lat_min, lat_min, lat_max, lat_max])
            )
            win = from_bounds(ex.min(), ey.min(), ex.max(), ey.max(), transform=src_vv.transform)
            step = 2
            out_h, out_w = max(20, int(win.height // step)), max(20, int(win.width // step))

            vv = src_vv.read(1, window=win, out_shape=(out_h, out_w)).astype(np.float32)
            vh = src_vh.read(1, window=win, out_shape=(out_h, out_w)).astype(np.float32)
            win_transform = src_vv.window_transform(win) * Affine.scale(win.width / out_w, win.height / out_h)

            x = win_transform.c + (np.arange(out_w) + 0.5) * win_transform.a
            y = win_transform.f + (np.arange(out_h) + 0.5) * win_transform.e

            valid = (vv > 0) & (vh > 0) & np.isfinite(vv) & np.isfinite(vh)
            den = uniform_filter(valid.astype(np.float32), size=3)
            num_vv = uniform_filter(np.where(valid, vv, 0), size=3)
            num_vh = uniform_filter(np.where(valid, vh, 0), size=3)

            vv_db = np.where(den > 0.5, 10 * np.log10(np.maximum(num_vv / np.maximum(den, 1e-6), 1e-10)), np.nan)
            vh_db = np.where(den > 0.5, 10 * np.log10(np.maximum(num_vh / np.maximum(den, 1e-6), 1e-10)), np.nan)
            return x, y, float(win_transform.a), float(win_transform.e), vv_db, vh_db, to_utm, to_ll

    x1, y1, dx, dy, hh_1, _, to_utm, to_ll = read_s1_window(item1)
    x2, y2, _, _, hh_2_raw, hv_2_raw, _, _ = read_s1_window(item2)

    if hh_1.shape != hh_2_raw.shape:
        Y_m, X_m = np.meshgrid(y1, x1, indexing="ij")
        hh_2 = map_coordinates(hh_2_raw, [(Y_m - y2[0]) / dy, (X_m - x2[0]) / dx], order=1, mode="constant", cval=np.nan)
        hv_2 = map_coordinates(hv_2_raw, [(Y_m - y2[0]) / dy, (X_m - x2[0]) / dx], order=1, mode="constant", cval=np.nan)
    else:
        hh_2, hv_2 = hh_2_raw, hv_2_raw

    return f"Sentinel-1 SAR ({d1} → {d2})", (x1, y1, dx, dy, hh_1, hh_2, hv_2, to_utm, to_ll, d1, d2)


def smooth_ring(ring):
    if len(ring) < 5:
        return ring
    pts = ring[:-1]
    smoothed = []
    n = len(pts)
    for i in range(n):
        p_prev, p_curr, p_next = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        smoothed.append([
            round(0.25 * p_prev[0] + 0.5 * p_curr[0] + 0.25 * p_next[0], 6),
            round(0.25 * p_prev[1] + 0.5 * p_curr[1] + 0.25 * p_next[1], 6)
        ])
    smoothed.append(smoothed[0])
    return smoothed


@app.post("/api/analyze")
def analyze_location(req: AnalyzeRequest):
    lat, lon, r = req.lat, req.lon, req.radius_deg
    lat_min, lat_max = lat - r, lat + r
    lon_min, lon_max = lon - r, lon + r

    sensor_used, res_data = try_nisar_api_or_local(lon_min, lat_min, lon_max, lat_max, req.start_date, req.end_date)
    if res_data is None:
        try:
            sensor_used, res_data = fallback_sentinel1_global(lon_min, lat_min, lon_max, lat_max, req.start_date, req.end_date)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Zone ou dates non couvertes : {str(e)}")

    x, y, dx, dy, hh_1, hh_2, hv_2, to_utm, to_ll, d1_str, d2_str = res_data
    weekly_rain, total_rain, meteo_src = get_weather_proof(lat, lon, d1_str, d2_str)
    is_dry_period = total_rain < 20.0

    px_ha = abs(dx * dy) / 10_000.0
    RES = 0.0005

    X_mesh, Y_mesh = np.meshgrid(x, y)
    lon_grid, lat_grid = to_ll.transform(X_mesh, Y_mesh)
    aoi = (
        np.isfinite(hh_1) & np.isfinite(hh_2) & np.isfinite(hv_2)
        & (lat_grid >= lat_min) & (lat_grid <= lat_max)
        & (lon_grid >= lon_min) & (lon_grid <= lon_max)
    )

    delta_hh = hh_2 - hh_1
    shift = float(np.nanmedian(delta_hh[aoi])) if np.any(aoi) else 0.0
    delta_hh -= shift

    flat_mask = aoi & (hv_2 < -13.0)
    valid_deltas = delta_hh[flat_mask & np.isfinite(delta_hh)]
    dyn_thresh = float(np.percentile(valid_deltas, 98.0)) if len(valid_deltas) > 100 else 2.2
    irr_thresh = max(2.1, min(dyn_thresh, 3.1))

    struct = np.ones((2, 2), dtype=bool)
    irrigated_mask = binary_opening(flat_mask & (delta_hh > irr_thresh) & (delta_hh < 8.0) & (hh_2 > -16.0), structure=struct)
    reservoir_mask = binary_opening(flat_mask & (hh_2 < -16.2) & (delta_hh < -2.0), structure=struct)
    affine = Affine(dx, 0, x[0] - dx / 2, 0, dy, y[0] - dy / 2)

    def vectorize(mask, cat, compact=False):
        labeled, n = label(mask)
        if n == 0:
            return []
        counts = np.bincount(labeled.ravel())
        objs = find_objects(labeled)
        feats = []
        for i, sl in enumerate(objs, start=1):
            px = int(counts[i])
            if px < 8 or px > 1200:
                continue
            h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
            if compact and (max(h, w) / max(min(h, w), 1) > 3.5 or px / (h * w) < 0.32):
                continue
            sub = labeled[sl] == i
            mean_d = float(np.mean(delta_hh[sl][sub]))
            rr, cc = np.where(sub)
            c_lon, c_lat = to_ll.transform(
                float(x[sl[1].start + int(np.mean(cc))]),
                float(y[sl[0].start + int(np.mean(rr))])
            )

            for geom, _ in shapes(sub.astype(np.uint8), mask=sub, transform=affine * Affine.translation(sl[1].start, sl[0].start)):
                rings = []
                for ring in geom["coordinates"]:
                    xs, ys = zip(*ring)
                    lons, lats = to_ll.transform(xs, ys)
                    raw_ring = [[round(float(a), 6), round(float(b), 6)] for a, b in zip(lons, lats)]
                    rings.append(smooth_ring(raw_ring))
                feats.append({
                    "type": "Feature",
                    "properties": {
                        "category": cat,
                        "area_ha": round(px * px_ha, 1),
                        "delta_db": round(mean_d, 1),
                        "center_lat": round(float(c_lat), 5),
                        "center_lon": round(float(c_lon), 5),
                        "rain_mm": total_rain,
                        "cause": f"Irrigation artificielle ({total_rain} mm de pluie entre {d1_str} et {d2_str})" if is_dry_period else f"Pluie naturelle ({total_rain} mm entre {d1_str} et {d2_str})"
                    },
                    "geometry": {"type": "Polygon", "coordinates": rings}
                })
        return sorted(feats, key=lambda f: -f["properties"]["area_ha"])[:180]

    irr_features = vectorize(irrigated_mask, "irrigation")
    res_features = vectorize(reservoir_mask, "reservoir", compact=True)

    def merc(la): return np.log(np.tan(np.pi / 4 + np.radians(la) / 2))
    def unmerc(m): return np.degrees(2 * np.arctan(np.exp(m)) - np.pi / 2)

    step = np.radians(RES)
    lons_arr = np.arange(lon_min, lon_max, RES)
    lats_arr = unmerc(np.arange(merc(lat_max), merc(lat_min), -step))
    LON, LAT = np.meshgrid(lons_arr, lats_arr)
    X_r, Y_r = to_utm.transform(LON, LAT)
    g1_r = map_coordinates(hh_1, [(Y_r - y[0]) / dy, (X_r - x[0]) / dx], order=0, mode='constant', cval=np.nan)
    g2_r = map_coordinates(hh_2, [(Y_r - y[0]) / dy, (X_r - x[0]) / dx], order=0, mode='constant', cval=np.nan)

    stretch = lambda a: np.clip((a - (-22)) / (-6 - (-22)), 0, 1)
    rgb = np.dstack([stretch(g2_r), stretch(g1_r), stretch(g1_r)])
    alpha = (np.isfinite(g1_r) & np.isfinite(g2_r)).astype(np.float32) * 0.68
    rgba = (np.dstack([np.nan_to_num(rgb), alpha]) * 255).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    img_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    bounds = [
        [float(unmerc(merc(lats_arr[-1]) - step / 2)), float(lons_arr[0] - RES / 2)],
        [float(unmerc(merc(lats_arr[0]) + step / 2)), float(lons_arr[-1] + RES / 2)]
    ]

    all_sorted = sorted(res_features[:3] + irr_features[:5], key=lambda f: abs(f["properties"]["delta_db"]), reverse=True)[:5]
    anomalies = [{
        "id": f"HYD-0{idx+1}",
        "lat": f["properties"]["center_lat"],
        "lon": f["properties"]["center_lon"],
        "score": round(abs(f["properties"]["delta_db"]) * 2.5, 1),
        "type": f"Retenue remplie ({f['properties']['area_ha']} ha)" if f["properties"]["category"] == "reservoir" else f"Zone humidifiée ({f['properties']['area_ha']} ha)",
        "detail": f"ΔSignal: {f['properties']['delta_db']:+.1f} dB | Pluie ({d1_str}→{d2_str}): {total_rain} mm"
    } for idx, f in enumerate(all_sorted)]

    return {
        "name": f"{req.name} • {sensor_used}",
        "bounds": bounds,
        "rasterDataUrl": img_b64,
        "geojson": {"type": "FeatureCollection", "features": irr_features + res_features},
        "ha_reservoirs": round(sum(f["properties"]["area_ha"] for f in res_features), 1),
        "ha_irrigated": round(sum(f["properties"]["area_ha"] for f in irr_features), 1),
        "rainData": weekly_rain,
        "rainTotal": f"{total_rain} mm ({d1_str} → {d2_str})",
        "meteoSource": meteo_src,
        "alertLevel": "IRRIGATION ARTIFICIELLE (SEC)" if is_dry_period else "HUMIDIFICATION PLUVIALE",
        "anomalies": anomalies
    }