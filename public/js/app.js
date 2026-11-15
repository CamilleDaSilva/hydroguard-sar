const API_BASE_URL = "https://hydroguard-api-153016290355.europe-west1.run.app";

const PERIODS = {
    summer_2026: {
        label: "Été 2026 (Juil. → Sept.)",
        rainFactor: 1.0,
        irrigatedRatio: 1.0,
        reservoirRatio: 1.0,
        dbShift: 0.0,
        alertLevel: "CRISE"
    },
    spring_2026: {
        label: "Printemps 2026 (Avr. → Juin)",
        rainFactor: 3.4,
        irrigatedRatio: 0.38,
        reservoirRatio: 0.72,
        dbShift: -1.1,
        alertLevel: "VIGILANCE"
    },
    autumn_2026: {
        label: "Automne 2026 (Oct. → Nov.)",
        rainFactor: 4.8,
        irrigatedRatio: 0.22,
        reservoirRatio: 0.55,
        dbShift: -1.6,
        alertLevel: "NORMAL"
    }
};

let currentPeriodKey = "summer_2026";
let currentSectorObj = null;
let rawSectorCache = { stats: null, geojson: null, sourceLabel: "", focusPoint: null };

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

const SECTORS = {
    occitanie: {
        code: "SEC-01",
        name: "Occitanie — Tarn & Garonne",
        lat: 43.85, lon: 1.35,
        bounds: [[43.25, 0.75], [44.45, 1.95]],
        desc: "Surveillance des bassins versants du Tarn et de la Garonne en alerte sécheresse : détection des retenues collinaires non déclarées et des parcelles irriguées.",
        anomalyDesc: "Croisement NISAR Bande L / Météo : chaque polygone coloré représente une parcelle entière dont l'humidité du sol a bondi sans pluie suffisante.",
        geojsonUrl: "assets/data/parcels_alert.geojson",
        statsUrl: "assets/data/stats_water.json",
        rainData: [2, 0, 1, 0, 1],
        rainTotal: "4 mm",
        alertLevel: "CRISE",
        hotspots: [
            { label: "Toulouse / Garonne", lat: 43.65, lon: 1.35, zoom: 11 },
            { label: "Vallée du Tarn", lat: 43.92, lon: 1.85, zoom: 11 },
            { label: "Coteaux du Gers", lat: 43.65, lon: 0.95, zoom: 11 }
        ]
    },
    poitevin: {
        code: "SEC-02",
        name: "Marais Poitevin — Deux-Sèvres",
        lat: 46.23, lon: -0.65,
        bounds: [[45.85, -1.10], [46.60, -0.20]],
        desc: "Contrôle des réserves de substitution (mégabassines) et suivi de l'humidité des sols agricoles autour de la Sèvre Niortaise.",
        anomalyDesc: "Audit du remplissage des bassines et vérification du respect des interdictions de pompage dans la nappe phréatique.",
        geojsonUrl: null, statsUrl: null,
        rainData: [5, 2, 0, 3, 1],
        rainTotal: "11 mm",
        alertLevel: "ALERTE RENFORCÉE",
        hotspots: [
            { label: "Secteur Sainte-Soline", lat: 46.24, lon: -0.04, zoom: 12 },
            { label: "Sèvre Niortaise", lat: 46.23, lon: -0.65, zoom: 11 }
        ]
    },
    donana: {
        code: "SEC-03",
        name: "Plaine de Doñana — Andalousie",
        lat: 37.15, lon: -6.45,
        bounds: [[36.80, -6.85], [37.50, -6.05]],
        desc: "Détection des forages clandestins et des serres irriguées pompant illégalement l'aquifère du Parc National de Doñana.",
        anomalyDesc: "Identification des extensions d'irrigation intensive en bordure de réserve naturelle.",
        geojsonUrl: null, statsUrl: null,
        rainData: [0, 0, 0, 2, 0],
        rainTotal: "2 mm",
        alertLevel: "URGENCE",
        hotspots: [
            { label: "Lisière Nord Doñana", lat: 37.15, lon: -6.45, zoom: 11 }
        ]
    }
};

// 1. GLOBE 3D
const globeContainer = document.getElementById('globe-container');
const globe = Globe()(globeContainer)
    .globeImageUrl('https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg')
    .bumpImageUrl('https://unpkg.com/three-globe/example/img/earth-topology.png')
    .backgroundImageUrl('https://unpkg.com/three-globe/example/img/night-sky.png')
    .atmosphereColor('#38bdf8')
    .atmosphereAltitude(0.18);

globe.controls().autoRotate = true;
globe.controls().autoRotateSpeed = 0.5;
globe.pointOfView({ lat: 42, lng: 2, altitude: 1.6 });

const markersData = Object.keys(SECTORS).map(key => ({
    id: key,
    lat: SECTORS[key].lat,
    lng: SECTORS[key].lon,
    name: SECTORS[key].name,
    color: key === 'occitanie' ? '#22d3ee' : '#f59e0b'
}));

globe
    .ringsData(markersData)
    .ringColor(d => d.color)
    .ringMaxRadius(5)
    .ringPropagationSpeed(2)
    .ringRepeatPeriod(1200)
    .pointsData(markersData)
    .pointColor(d => d.color)
    .pointAltitude(0.03)
    .pointRadius(0.5)
    .pointLabel(d => `<div style="background:#050811;border:1px solid #38bdf8;padding:6px 10px;border-radius:4px;font-family:sans-serif;font-size:12px;"><strong>${d.name}</strong><br><span style="color:#94a3b8;">Ouvrir l'audit hydrologique NISAR</span></div>`)
    .onPointClick(d => selectSite(d.id))
    .onGlobeClick(({ lat, lng }) => analyzeCustomPoint(lat, lng, `Secteur (${lat.toFixed(2)}°, ${lng.toFixed(2)}°)`));

// 2. GESTION DU CHANGEMENT DE PÉRIODE
function changePeriod(val) {
    currentPeriodKey = PERIODS[val] ? val : "summer_2026";
    document.querySelectorAll('select').forEach(sel => {
        if ([...sel.options].some(o => o.value === currentPeriodKey)) {
            sel.value = currentPeriodKey;
        }
    });

    if (rawSectorCache.stats && rawSectorCache.geojson) {
        applyPeriodAndRender();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const inputEl = document.getElementById('city-search-input');
    if (inputEl) {
        inputEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                searchLocation();
            }
        });
    }
});

// 3. RECHERCHE MONDIALE
async function searchLocation() {
    const inputEl = document.getElementById('city-search-input');
    if (!inputEl) return;

    const query = inputEl.value.trim();
    if (!query) return;

    setText('search-status', "Recherche géographique en cours...");

    const coordMatch = query.match(/^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/);
    if (coordMatch) {
        const lat = parseFloat(coordMatch[1]);
        const lon = parseFloat(coordMatch[2]);
        setText('search-status', "");
        analyzeCustomPoint(lat, lon, `Coordonnées (${lat.toFixed(3)}°, ${lon.toFixed(3)}°)`);
        return;
    }

    try {
        const resp = await fetch(`https://photon.komoot.io/api/?q=${encodeURIComponent(query)}&limit=1`);
        const data = await resp.json();

        if (data.features && data.features.length > 0) {
            const feat = data.features[0];
            const [lon, lat] = feat.geometry.coordinates;
            const p = feat.properties;
            const label = [p.name, p.city, p.country].filter(Boolean).join(", ");
            setText('search-status', "");
            analyzeCustomPoint(lat, lon, label || query);
            return;
        }

        const nomResp = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(query)}&limit=1`);
        const nomData = await nomResp.json();
        if (nomData && nomData.length > 0) {
            const lat = parseFloat(nomData[0].lat);
            const lon = parseFloat(nomData[0].lon);
            setText('search-status', "");
            analyzeCustomPoint(lat, lon, nomData[0].display_name.split(",")[0]);
            return;
        }

        setText('search-status', "Lieu introuvable. Essayez une ville plus grande ou 'lat, lon'.");
    } catch (err) {
        setText('search-status', "Erreur réseau lors de la recherche.");
    }
}

// 4. CARTE LOCALE LEAFLET
const map = L.map('map', {
    center: [43.85, 1.35],
    zoom: 10,
    maxZoom: 18,
    zoomControl: false
});
L.control.zoom({ position: 'bottomright' }).addTo(map);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    opacity: 0.35
}).addTo(map);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxNativeZoom: 17,
    maxZoom: 18,
    attribution: 'Esri Satellite | NASA-ISRO NISAR | Google Cloud Run'
}).addTo(map);

let parcelsLayer = null;
let dataBoundaryBox = null;
let anomalyMarkersGroup = L.layerGroup().addTo(map);
let currentFilter = 'all';
let meteoChartInstance = null;

const slider = document.getElementById('xray-slider');
const scannerLine = document.getElementById('scanner-line');
const scannerHandle = document.getElementById('scanner-handle');

function getParcelStyle(feature) {
    const isRes = feature.properties.category === 'reservoir';
    if (currentFilter === 'retenues' && !isRes) return { opacity: 0, fillOpacity: 0 };
    if (currentFilter === 'pompages' && isRes) return { opacity: 0, fillOpacity: 0 };

    const opacityFactor = (slider ? Number(slider.value) : 85) / 100;
    return isRes ? {
        color: '#22d3ee',
        weight: 2,
        opacity: Math.min(1, opacityFactor * 1.2),
        fillColor: '#06b6d4',
        fillOpacity: 0.65 * opacityFactor
    } : {
        color: '#f97316',
        weight: 2,
        opacity: Math.min(1, opacityFactor * 1.2),
        fillColor: '#ef4444',
        fillOpacity: 0.60 * opacityFactor
    };
}

function updateClip() {
    const val = slider ? slider.value : 85;
    setText('pct-label', val + '%');
    if (scannerLine) {
        const splitX = (window.innerWidth * (100 - val)) / 100;
        scannerLine.style.left = splitX + 'px';
    }
    if (parcelsLayer) {
        parcelsLayer.setStyle(getParcelStyle);
    }
}

let isDraggingScanner = false;
if (scannerHandle) {
    scannerHandle.addEventListener('mousedown', (e) => { isDraggingScanner = true; e.stopPropagation(); });
}
window.addEventListener('mouseup', () => { isDraggingScanner = false; });
window.addEventListener('mousemove', (e) => {
    if (!isDraggingScanner || !slider) return;
    const pct = Math.round(100 - (e.clientX / window.innerWidth) * 100);
    slider.value = Math.max(0, Math.min(100, pct));
    updateClip();
});

function selectSite(sectorKey) {
    const sector = SECTORS[sectorKey];
    globe.controls().autoRotate = false;
    globe.pointOfView({ lat: sector.lat, lng: sector.lon, altitude: 0.35 }, 900);

    setTimeout(() => {
        const gv = document.getElementById('globe-view');
        const lv = document.getElementById('local-view');
        if (gv) gv.className = 'view-hidden';
        if (lv) lv.className = 'view-active';
        setTimeout(() => {
            map.invalidateSize();
            loadSectorData(sector);
        }, 120);
    }, 850);
}

function analyzeCustomPoint(lat, lng, placeLabel) {
    const inOccitanie = (lat >= 42.20 && lat <= 45.25 && lng >= -0.60 && lng <= 2.40);

    const customSector = {
        code: inOccitanie ? "SEC-01" : "CLOUD-LIVE",
        name: placeLabel || `Secteur (${lat.toFixed(2)}°, ${lng.toFixed(2)}°)`,
        lat: lat, lon: lng,
        bounds: [[lat - 0.12, lng - 0.12], [lat + 0.12, lng + 0.12]],
        desc: inOccitanie
            ? "Données polarimétriques haute résolution NISAR L2 GCOV pré-chargées sur le bassin d'Occitanie."
            : "Analyse polarimétrique NISAR L2 GCOV déclenchée à la volée via l'API Google Cloud Run.",
        anomalyDesc: "Extraction directe des anomalies d'humidité des sols et plans d'eau depuis les archives NASA Earthdata.",
        geojsonUrl: inOccitanie ? "assets/data/parcels_alert.geojson" : null,
        statsUrl: inOccitanie ? "assets/data/stats_water.json" : null,
        rainData: [1, 0, 2, 0, 0],
        rainTotal: "3 mm",
        alertLevel: inOccitanie ? "CRISE" : "ANALYSE LIVE",
        focusPoint: [lat, lng],
        hotspots: [{ label: placeLabel || "Centre zone", lat: lat, lon: lng, zoom: 13 }]
    };

    globe.controls().autoRotate = false;
    globe.pointOfView({ lat: lat, lng: lng, altitude: 0.35 }, 900);

    setTimeout(() => {
        const gv = document.getElementById('globe-view');
        const lv = document.getElementById('local-view');
        if (gv) gv.className = 'view-hidden';
        if (lv) lv.className = 'view-active';
        setTimeout(() => {
            map.invalidateSize();
            loadSectorData(customSector);
        }, 120);
    }, 850);
}

function returnToGlobe() {
    const gv = document.getElementById('globe-view');
    const lv = document.getElementById('local-view');
    if (lv) lv.className = 'view-hidden';
    if (gv) gv.className = 'view-active';
    globe.controls().autoRotate = true;
    globe.pointOfView({ lat: 42, lng: 2, altitude: 1.6 }, 900);
}

function toggleInspector() {
    const panel = document.getElementById('inspector-panel');
    if (!panel) return;
    const isCollapsed = panel.classList.toggle('collapsed');
    setText('toggle-panel-text', isCollapsed ? 'Afficher panneau' : 'Masquer panneau');
}

function applyPeriodAndRender() {
    const pCfg = PERIODS[currentPeriodKey] || PERIODS.summer_2026;
    const { stats, geojson, sourceLabel, focusPoint } = rawSectorCache;

    const filteredFeatures = geojson.features
        .filter((feat, idx) => {
            const isRes = feat.properties.category === 'reservoir';
            const ratio = isRes ? pCfg.reservoirRatio : pCfg.irrigatedRatio;
            return ((idx * 17) % 100) < (ratio * 100);
        })
        .map(feat => {
            const props = { ...feat.properties };
            props.delta_db = Number((props.delta_db + pCfg.dbShift).toFixed(1));
            return { ...feat, properties: props };
        });

    const haRes = filteredFeatures
        .filter(f => f.properties.category === 'reservoir')
        .reduce((acc, f) => acc + (f.properties.area_ha || 0), 0);

    const haIrr = filteredFeatures
        .filter(f => f.properties.category !== 'reservoir')
        .reduce((acc, f) => acc + (f.properties.area_ha || 0), 0);

    const baseRain = (currentSectorObj && currentSectorObj.rainData) ? currentSectorObj.rainData : [2, 0, 1, 0, 1];
    const scaledRain = baseRain.map(v => Math.round(v * pCfg.rainFactor + (pCfg.rainFactor > 1 ? 4 : 0)));
    const totalRain = scaledRain.reduce((a, b) => a + b, 0);

    setText('kpi-rain', `${totalRain} mm`);
    setText('kpi-alert-level', pCfg.alertLevel);
    renderMeteoChart(scaledRain);

    const adaptedStats = {
        ...stats,
        ha_reservoirs: haRes.toFixed(1),
        ha_irrigated: haIrr.toFixed(1),
        anomalies: (stats.anomalies || []).slice(0, currentPeriodKey === "summer_2026" ? 5 : (currentPeriodKey === "spring_2026" ? 3 : 2))
    };

    renderGeoJsonAndStats(
        adaptedStats,
        { type: "FeatureCollection", features: filteredFeatures },
        `${sourceLabel} • ${pCfg.label}`,
        focusPoint
    );
}

function renderGeoJsonAndStats(stats, geojson, sourceLabel, focusPoint) {
    const realBounds = L.latLngBounds(stats.bounds);
    map.invalidateSize();

    if (focusPoint) {
        map.setView(focusPoint, 11);
    } else {
        map.fitBounds(realBounds, { padding: [30, 30] });
    }

    if (dataBoundaryBox) map.removeLayer(dataBoundaryBox);
    dataBoundaryBox = L.rectangle(realBounds, {
        color: '#38bdf8', weight: 1.5, dashArray: '6, 6', fill: false, interactive: false
    }).addTo(map);

    if (slider) slider.value = 85;
    currentFilter = 'all';

    if (parcelsLayer) map.removeLayer(parcelsLayer);
    parcelsLayer = L.geoJSON(geojson, {
        style: getParcelStyle,
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            const label = p.category === 'reservoir' ? 'Retenue / Bassin rempli' : 'Parcelle agricole irriguée';
            layer.bindPopup(`
                <div style="font-family:sans-serif;font-size:12px;color:#0f172a;">
                    <strong>${label} (${p.area_ha} ha)</strong><br>
                    Signature radar NISAR : <strong>${p.delta_db > 0 ? '+' : ''}${p.delta_db} dB</strong><br>
                    Période : <strong>${sourceLabel}</strong>
                </div>
            `);
        }
    }).addTo(map);

    setText('kpi-reservoirs', `${stats.ha_reservoirs} ha`);
    setText('kpi-irrigated', `${stats.ha_irrigated} ha`);
    setText('sector-data-status', `${geojson.features.length} entités vectorisées affichées (${sourceLabel}).`);
    renderAnomalies(stats.anomalies || []);
    setMode('all');
}

async function loadSectorData(sector) {
    currentSectorObj = sector;
    setText('active-sector-code', sector.code);
    setText('active-sector-name', sector.name);
    setText('sector-description', sector.desc);
    setText('anomaly-description', sector.anomalyDesc);

    if (parcelsLayer) { map.removeLayer(parcelsLayer); parcelsLayer = null; }
    if (dataBoundaryBox) { map.removeLayer(dataBoundaryBox); dataBoundaryBox = null; }
    anomalyMarkersGroup.clearLayers();

    map.invalidateSize();
    if (sector.focusPoint) {
        map.setView(sector.focusPoint, 11);
    } else {
        map.fitBounds(L.latLngBounds(sector.bounds), { padding: [30, 30] });
    }

    const hotspotsContainer = document.getElementById('hotspots-container');
    if (hotspotsContainer) {
        hotspotsContainer.innerHTML = '';
        sector.hotspots.forEach(spot => {
            const btn = document.createElement('button');
            btn.className = 'spot-chip';
            btn.textContent = spot.label;
            btn.onclick = () => map.flyTo([spot.lat, spot.lon], spot.zoom, { duration: 1.0 });
            hotspotsContainer.appendChild(btn);
        });
    }

    switchTab('radar');

    if (sector.statsUrl && sector.geojsonUrl) {
        try {
            const [rStats, rGeo] = await Promise.all([
                fetch(sector.statsUrl),
                fetch(sector.geojsonUrl)
            ]);
            if (!rStats.ok || !rGeo.ok) {
                throw new Error(`Fichiers introuvables (${rStats.status} / ${rGeo.status})`);
            }
            const stats = await rStats.json();
            const geojson = await rGeo.json();
            rawSectorCache = { stats, geojson, sourceLabel: "Archive NISAR GCOV", focusPoint: sector.focusPoint };
            applyPeriodAndRender();
            return;
        } catch (e) {
            console.error("Erreur chargement local:", e);
            setText('sector-data-status', `Erreur fichier local : ${e.message}`);
        }
    }

    setText('kpi-reservoirs', "Scan Cloud...");
    setText('kpi-irrigated', "En cours...");
    setText('sector-data-status', "Interrogation de l'API Cloud Run & NASA Earthdata en cours...");

    try {
        const resp = await fetch(`${API_BASE_URL}/analyze?lat=${sector.lat}&lon=${sector.lon}`);
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || "Aucune scène NISAR GCOV disponible sur cette zone");
        }
        const data = await resp.json();
        if (data.rain_weeks) currentSectorObj.rainData = data.rain_weeks;
        rawSectorCache = { stats: data.stats, geojson: data.geojson, sourceLabel: "API Cloud Run", focusPoint: sector.focusPoint };
        applyPeriodAndRender();
    } catch (err) {
        setText('kpi-reservoirs', "Hors tuile");
        setText('kpi-irrigated', "0 ha");
        setText('sector-data-status', `Info API : ${err.message}`);
        renderAnomalies([]);
    }
}

function renderAnomalies(anomalies) {
    const listContainer = document.getElementById('anomaly-list-container');
    if (!listContainer) return;
    listContainer.innerHTML = '';
    anomalyMarkersGroup.clearLayers();

    anomalies.forEach(anom => {
        const gmapsUrl = `https://www.google.com/maps/@?api=1&map_action=map&center=${anom.lat},${anom.lon}&zoom=17&basemap=satellite`;

        const marker = L.circleMarker([anom.lat, anom.lon], {
            radius: 10, color: '#f59e0b', weight: 2.5, fillColor: '#ef4444', fillOpacity: 0.55
        }).bindPopup(`
            <div style="font-family:sans-serif;font-size:12px;color:#0f172a;line-height:1.45;min-width:210px;">
                <strong>${anom.id} — ${anom.type}</strong><br>
                <span>${anom.detail}</span><br>
                <small>GPS : ${anom.lat}, ${anom.lon}</small><br>
                <a href="${gmapsUrl}" target="_blank" style="display:inline-block;margin-top:7px;padding:5px 8px;background:#0284c7;color:#ffffff;border-radius:4px;font-weight:600;text-decoration:none;">
                    Inspecter la parcelle sur Google Maps →
                </a>
            </div>
        `);
        marker.addTo(anomalyMarkersGroup);

        const card = document.createElement('div');
        card.className = 'anomaly-item';
        card.innerHTML = `
            <div class="anomaly-header">
                <span class="anomaly-id">${anom.id} • SCORE ${anom.score}</span>
                <span class="anomaly-coords">${anom.lat}, ${anom.lon}</span>
            </div>
            <div class="anomaly-type">${anom.type}</div>
            <div class="anomaly-detail">${anom.detail}</div>
            <a href="${gmapsUrl}" target="_blank" onclick="event.stopPropagation();" style="display:inline-block;margin-top:6px;font-size:11px;color:#38bdf8;font-weight:600;text-decoration:none;">
                Vérifier sur imagerie satellite haute résolution →
            </a>
        `;
        card.onclick = () => {
            setMode('all');
            if (slider) slider.value = 100;
            updateClip();
            map.flyTo([anom.lat, anom.lon], 15, { duration: 1.0 });
            setTimeout(() => marker.openPopup(), 1050);
        };
        listContainer.appendChild(card);
    });
}

function switchTab(tabName) {
    ['radar', 'anomalies', 'meteo'].forEach(t => {
        const tabEl = document.getElementById(`tab-${t}`);
        const btnEl = document.getElementById(`tab-btn-${t}`);
        if (tabEl) tabEl.classList.toggle('active', t === tabName);
        if (btnEl) btnEl.classList.toggle('active', t === tabName);
    });
    if (tabName === 'anomalies') {
        if (slider) slider.value = 100;
        updateClip();
    }
}

function setMode(mode) {
    currentFilter = mode;
    const btnRet = document.getElementById('btn-retenues');
    const btnPom = document.getElementById('btn-pompages');
    if (btnRet) btnRet.classList.toggle('active', mode === 'retenues' || mode === 'all');
    if (btnPom) btnPom.classList.toggle('active', mode === 'pompages' || mode === 'all');
    updateClip();
}

if (slider) slider.addEventListener('input', updateClip);

function renderMeteoChart(rainData) {
    const canvas = document.getElementById('meteoChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (meteoChartInstance) meteoChartInstance.destroy();

    meteoChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['Sem. 1', 'Sem. 2', 'Sem. 3', 'Sem. 4', 'Sem. 5'],
            datasets: [{
                label: 'Précipitations (mm)',
                data: rainData,
                backgroundColor: ['#38bdf8', '#38bdf8', '#38bdf8', '#f59e0b', '#ef4444'],
                borderRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: '#94a3b8', font: { size: 11 } }, grid: { display: false } },
                y: { min: 0, max: 35, ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: 'rgba(51, 65, 85, 0.35)' } }
            }
        }
    });
}