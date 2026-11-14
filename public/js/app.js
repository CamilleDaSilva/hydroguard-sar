// 1. INITIALISATION DU GLOBE 3D INTERACTIF
const globeContainer = document.getElementById('globe-container');
const globe = Globe()(globeContainer)
    .globeImageUrl('https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg')
    .bumpImageUrl('https://unpkg.com/three-globe/example/img/earth-topology.png')
    .backgroundImageUrl('https://unpkg.com/three-globe/example/img/night-sky.png')
    .atmosphereColor('#38bdf8')
    .atmosphereAltitude(0.18)
    .onGlobeClick(({ lat, lng }) => {
        openLocation(lat, lng, `Secteur (${lat.toFixed(2)}°, ${lng.toFixed(2)}°)`);
    });

globe.controls().autoRotate = true;
globe.controls().autoRotateSpeed = 0.45;
globe.pointOfView({ lat: 38, lng: 0, altitude: 1.8 });

// Raccourcis instantanés (0 ms) + géocodage mondial Photon/Nominatim
const QUICK_CITIES = {
    "toulouse": { lat: 43.6047, lon: 1.4442, label: "Toulouse, Haute-Garonne" },
    "albi": { lat: 43.9289, lon: 2.1464, label: "Albi, Tarn" },
    "auch": { lat: 43.6464, lon: 0.5855, label: "Auch, Gers" },
    "agen": { lat: 44.2031, lon: 0.6164, label: "Agen, Lot-et-Garonne" },
    "montauban": { lat: 44.0176, lon: 1.3549, label: "Montauban, Tarn-et-Garonne" },
    "niort": { lat: 46.3237, lon: -0.4648, label: "Niort, Marais Poitevin" },
    "sainte-soline": { lat: 46.2450, lon: -0.0360, label: "Sainte-Soline, Deux-Sèvres" },
    "fresno": { lat: 36.7378, lon: -119.7871, label: "Fresno, Vallée de San Joaquin (USA)" },
    "almeria": { lat: 36.8340, lon: -2.4637, label: "Almería, Andalousie" },
    "donana": { lat: 37.1500, lon: -6.4500, label: "Parc de Doñana, Espagne" }
};

async function searchLocation() {
    const inputEl = document.getElementById('city-search');
    const btnEl = document.querySelector('.btn-primary');
    const query = inputEl.value.trim();
    if (!query) {
        inputEl.focus();
        return;
    }

    const normalized = query.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    for (const [key, city] of Object.entries(QUICK_CITIES)) {
        if (normalized === key || normalized.startsWith(key + " ") || normalized.startsWith(key + ",")) {
            openLocation(city.lat, city.lon, city.label);
            return;
        }
    }

    const originalText = btnEl.textContent;
    btnEl.textContent = "Recherche...";
    btnEl.disabled = true;

    try {
        // API Photon (rapide et sans restriction CORS)
        const resp = await fetch(`https://photon.komoot.io/api/?q=${encodeURIComponent(query)}&limit=1`);
        const data = await resp.json();

        if (data.features && data.features.length > 0) {
            const coords = data.features[0].geometry.coordinates;
            const props = data.features[0].properties;
            const label = [props.name, props.state || props.country].filter(Boolean).join(', ');
            openLocation(coords[1], coords[0], label || query);
        } else {
            // Secours Nominatim
            const respNom = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`);
            const resNom = await respNom.json();
            if (resNom.length > 0) {
                const label = resNom[0].display_name.split(',').slice(0, 2).join(',');
                openLocation(parseFloat(resNom[0].lat), parseFloat(resNom[0].lon), label);
            } else {
                alert(`Lieu "${query}" introuvable. Essayez "Toulouse" ou cliquez directement sur le globe 3D.`);
            }
        }
    } catch (e) {
        console.error("Erreur de géocodage :", e);
        alert("Erreur réseau lors de la recherche. Cliquez directement sur le globe 3D.");
    } finally {
        btnEl.textContent = originalText;
        btnEl.disabled = false;
    }
}

document.getElementById('city-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        searchLocation();
    }
});

// 2. INITIALISATION DE LA CARTE LOCALE LEAFLET
const map = L.map('map', { zoomControl: false });
L.control.zoom({ position: 'bottomright' }).addTo(map);

L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Esri Satellite | NASA-ISRO NISAR | Sentinel-1'
}).addTo(map);

map.createPane('nisarPane');
map.getPane('nisarPane').style.zIndex = 450;

let parcelsLayer = null;
let dataBoundaryBox = null;
let anomalyMarkersGroup = L.layerGroup().addTo(map);
let activeModes = { retenues: true, pompages: true };
let meteoChartInstance = null;

function openLocation(lat, lon, label) {
    globe.controls().autoRotate = false;
    globe.pointOfView({ lat, lng: lon, altitude: 0.35 }, 750);

    setTimeout(() => {
        document.getElementById('globe-view').className = 'view-hidden';
        document.getElementById('local-view').className = 'view-active';

        const latDir = lat >= 0 ? 'N' : 'S';
        const lonDir = lon >= 0 ? 'E' : 'W';
        document.getElementById('active-coords').textContent = `${Math.abs(lat).toFixed(2)}°${latDir}, ${Math.abs(lon).toFixed(2)}°${lonDir}`;
        document.getElementById('active-sector-name').textContent = `${label} • NISAR Bande L`;

        loadSectorData(lat, lon);
    }, 700);
}

function returnToGlobe() {
    document.getElementById('local-view').className = 'view-hidden';
    document.getElementById('globe-view').className = 'view-active';
    globe.controls().autoRotate = true;
    globe.pointOfView({ lat: 38, lng: 0, altitude: 1.8 }, 850);
}

function toggleInspector() {
    const panel = document.getElementById('inspector-panel');
    const text = document.getElementById('toggle-panel-text');
    const isCollapsed = panel.classList.toggle('collapsed');
    text.textContent = isCollapsed ? 'Afficher panneau' : 'Masquer panneau';
}

function loadSectorData(lat, lon) {
    if (parcelsLayer) { map.removeLayer(parcelsLayer); parcelsLayer = null; }
    anomalyMarkersGroup.clearLayers();

    const targetBounds = L.latLngBounds([
        [lat - 0.14, lon - 0.22],
        [lat + 0.14, lon + 0.22]
    ]);

    const applyMapBounds = (boundsToFit, boxBounds) => {
        map.invalidateSize();
        map.fitBounds(boundsToFit, { padding: [30, 30] });

        if (dataBoundaryBox) map.removeLayer(dataBoundaryBox);
        dataBoundaryBox = L.rectangle(boxBounds, {
            color: '#38bdf8', weight: 1.5, dashArray: '6, 6', fill: false, interactive: false
        }).addTo(map);
    };

    Promise.all([
        fetch('assets/data/stats_water.json').then(r => r.json()),
        fetch('assets/data/parcels_alert.geojson').then(r => r.json())
    ]).then(([stats, geojson]) => {
        const realBounds = L.latLngBounds(stats.bounds);
        const isInsideProcessedTile = realBounds.pad(0.5).contains([lat, lon]);

        if (isInsideProcessedTile) {
            // Centre sur la ville demandée tout en affichant l'emprise réelle NISAR
            applyMapBounds(targetBounds, realBounds);
            renderGeoJsonLayer(geojson);
            document.getElementById('kpi-reservoirs').textContent = `${stats.ha_reservoirs} ha`;
            document.getElementById('kpi-irrigated').textContent = `${stats.ha_irrigated} ha`;
            renderAnomalies(stats.anomalies || []);
        } else {
            applyMapBounds(targetBounds, targetBounds);
            document.getElementById('kpi-reservoirs').textContent = "Analyse prête";
            document.getElementById('kpi-irrigated').textContent = "Tuile en attente";
            renderAnomalies([]);
        }
        requestAnimationFrame(updateClip);
    }).catch(() => {
        applyMapBounds(targetBounds, targetBounds);
        renderAnomalies([]);
        requestAnimationFrame(updateClip);
    });

    renderMeteoChart([2, 0, 1, 0, 1]);
}

function renderGeoJsonLayer(geojson) {
    parcelsLayer = L.geoJSON(geojson, {
        pane: 'nisarPane',
        style: getFeatureStyle,
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            const title = p.category === 'reservoir' ? 'Retenue / Plan d\'eau' : 'Champ humidifié';
            layer.bindPopup(`
                <div style="font-family:sans-serif;font-size:12px;color:#0f172a;">
                    <strong>${title} (${p.area_ha} ha)</strong><br>
                    Variation radar NISAR : <strong>${p.delta_db > 0 ? '+' : ''}${p.delta_db} dB</strong>
                </div>
            `);
        }
    }).addTo(map);
}

function getFeatureStyle(feature) {
    const isRes = feature.properties.category === 'reservoir';
    if (isRes && !activeModes.retenues) return { opacity: 0, fillOpacity: 0 };
    if (!isRes && !activeModes.pompages) return { opacity: 0, fillOpacity: 0 };

    return isRes ? {
        color: '#22d3ee', weight: 2, fillColor: '#06b6d4', fillOpacity: 0.55
    } : {
        color: '#f97316', weight: 2, fillColor: '#ef4444', fillOpacity: 0.50
    };
}

function setMode(mode) {
    activeModes[mode] = !activeModes[mode];
    if (!activeModes.retenues && !activeModes.pompages) {
        activeModes[mode] = true;
    }

    document.getElementById('btn-retenues').classList.toggle('active', activeModes.retenues);
    document.getElementById('btn-pompages').classList.toggle('active', activeModes.pompages);

    if (parcelsLayer) {
        parcelsLayer.setStyle(getFeatureStyle);
    }
    requestAnimationFrame(updateClip);
}

function renderAnomalies(anomalies) {
    const listContainer = document.getElementById('anomaly-list-container');
    listContainer.innerHTML = '';
    anomalyMarkersGroup.clearLayers();

    anomalies.forEach(anom => {
        const gmapsUrl = `https://www.google.com/maps/@?api=1&map_action=map&center=${anom.lat},${anom.lon}&zoom=17&basemap=satellite`;

        const marker = L.circleMarker([anom.lat, anom.lon], {
            radius: 10, color: '#f59e0b', weight: 2.5, fillColor: '#ef4444', fillOpacity: 0.55
        }).bindPopup(`
            <div style="font-family:sans-serif;font-size:12px;color:#0f172a;line-height:1.45;">
                <strong>${anom.id} — ${anom.type}</strong><br>
                <span>${anom.detail}</span><br>
                <a href="${gmapsUrl}" target="_blank" style="display:inline-block;margin-top:6px;color:#0284c7;font-weight:600;text-decoration:none;">
                    Vérifier sur Google Maps Satellite →
                </a>
            </div>
        `);
        marker.addTo(anomalyMarkersGroup);

        const card = document.createElement('div');
        card.className = 'anomaly-item';
        card.innerHTML = `
            <div style="display:flex;justify-content:space-between;font-size:11px;color:#f97316;font-weight:700;">
                <span>${anom.id} • SCORE ${anom.score}</span>
                <span style="color:#94a3b8;">${anom.lat}, ${anom.lon}</span>
            </div>
            <div style="font-size:12.5px;font-weight:600;margin:3px 0;">${anom.type}</div>
            <div style="font-size:11px;color:#94a3b8;">${anom.detail}</div>
        `;
        card.onclick = () => {
            slider.value = 100;
            updateClip();
            map.flyTo([anom.lat, anom.lon], 15, { duration: 1.0 });
            setTimeout(() => marker.openPopup(), 1050);
        };
        listContainer.appendChild(card);
    });
}

function switchTab(tabName) {
    ['radar', 'anomalies', 'meteo'].forEach(t => {
        document.getElementById(`tab-${t}`).classList.toggle('active', t === tabName);
        document.getElementById(`tab-btn-${t}`).classList.toggle('active', t === tabName);
    });
}

// 3. ALIGNEMENT AU PIXEL PRÈS DU TRAIT ROSE ET DES POLYGONES
const slider = document.getElementById('xray-slider');
const scannerLine = document.getElementById('scanner-line');
const scannerHandle = document.getElementById('scanner-handle');
const pctLabel = document.getElementById('pct-label');

function updateClip() {
    const val = Number(slider.value);
    const splitX = (window.innerWidth * (100 - val)) / 100;

    scannerLine.style.left = `${splitX}px`;
    pctLabel.textContent = `${val}%`;

    const nisarPane = map.getPane('nisarPane');
    if (!nisarPane) return;

    const rendererEl = nisarPane.querySelector('svg, canvas');
    if (rendererEl) {
        const rect = rendererEl.getBoundingClientRect();
        const clipLeft = Math.max(0, splitX - rect.left);
        rendererEl.style.clipPath = `inset(0px 0px 0px ${clipLeft}px)`;
        nisarPane.style.clipPath = 'none';
    }
}

let isDraggingScanner = false;
scannerHandle.addEventListener('mousedown', (e) => { isDraggingScanner = true; e.stopPropagation(); });
window.addEventListener('mouseup', () => { isDraggingScanner = false; });
window.addEventListener('mousemove', (e) => {
    if (!isDraggingScanner) return;
    const pct = Math.round(100 - (e.clientX / window.innerWidth) * 100);
    slider.value = Math.max(0, Math.min(100, pct));
    updateClip();
});

slider.addEventListener('input', updateClip);
map.on('move moveend zoom zoomend viewreset', () => requestAnimationFrame(updateClip));
window.addEventListener('resize', updateClip);

function renderMeteoChart(rainData) {
    const ctx = document.getElementById('meteoChart').getContext('2d');
    if (meteoChartInstance) meteoChartInstance.destroy();

    meteoChartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['Période 1', 'Période 2', 'Période 3', 'Période 4', 'Période 5'],
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
                y: { min: 0, max: 30, ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: 'rgba(51, 65, 85, 0.35)' } }
            }
        }
    });
}