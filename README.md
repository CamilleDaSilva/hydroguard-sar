# HydroGuard SAR — Satellite Water & Illegal Irrigation Monitoring

**NASA Space Apps Challenge — "Dancing with the SARs"**

HydroGuard SAR is an open-source geospatial intelligence platform powered by **NASA-ISRO NISAR (L-Band SAR)** and meteorological archive data. It detects undocumented water reservoirs (hillside retention basins) and clandestine agricultural irrigation during drought restriction periods, where optical satellites are blinded by clouds or dense crop canopies.

---

## Live Demo
- **Web Application:** `https://hydroguard-sar.netlify.app/` *https://hydroguard-sar.netlify.app/*

---

## Scientific Methodology

1. **L-Band Polarimetric SAR (NASA-ISRO NISAR L2 GCOV):**
   - Uses 24 cm L-band radar (`HHHH` and `HVHV` polarizations) to penetrate cloud cover and crop canopies every 12 days.
   - **Reservoir Filling Detection (Cyan):** Identifies newly flooded calm water surfaces via specular reflection drops ($\Delta\text{HH} < -2.8\text{ dB}$, $\text{HH}_2 < -17.0\text{ dB}$).
   - **Soil Moisture & Irrigation Spikes (Amber):** Detects sudden dielectric constant increases across flat agricultural parcels ($\Delta\text{HH}$ between $+2.3\text{ dB}$ and $+6.0\text{ dB}$).
2. **Spatial Multi-Look Filtering & Parcel Vectorization:**
   - Applies spatial speckle filtering and morphological compactness checks to isolate genuine agricultural parcels and retention basins from isolated pixel noise, exporting clean GeoJSON polygons.
3. **Meteorological Cross-Validation:**
   - Cross-references radar soil moisture anomalies against precipitation records (`0 mm` rainfall during drought bans) to distinguish clandestine water extraction from natural rainfall events.

---

## Project Architecture

```text
NASA_SPACE_APP_CHALLENGE/
├── .gitignore                     # Excludes heavy raw .h5 HDF5 files (>1.8 GB)
├── README.md                      # Project documentation
│
├── data_pipeline/                 # Scientific Python processing pipeline
│   ├── raw_nisar/                 # Raw NASA NISAR L2 GCOV .h5 granules (local only)
│   └── process_nisar.py           # Radiometric calibration, speckle filtering & GeoJSON export
│
└── public/                        # Interactive Web Application (Static Frontend)
    ├── index.html                 # 3D Orbital Globe + Local SAR X-Ray Scanner UI
    ├── css/
    │   └── style.css              # Dark aerospace UI & collapsible inspector panel
    ├── js/
    │   └── app.js                 # Globe.gl, Leaflet vector overlay, pixel-accurate X-Ray wipe & Chart.js
    └── assets/
        └── data/
            ├── parcels_alert.geojson  # Vectorized reservoirs & irrigated parcels
            └── stats_water.json       # Aggregated surface areas (ha) & top priority alerts
