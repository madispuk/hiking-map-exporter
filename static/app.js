/**
 * MapAnt Export Tool - Frontend Logic
 *
 * Initializes a Leaflet map with EPSG:3301 projection,
 * allows drawing A3-ratio rectangles, and exports high-resolution images.
 */

// EPSG:3301 - Estonian Coordinate System of 1997
const EPSG3301 = '+proj=lcc +lat_1=59.33333333333334 +lat_2=58 +lat_0=57.51755393055556 +lon_0=24 +x_0=500000 +y_0=6375000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs';

// Register the CRS with proj4
proj4.defs('EPSG:3301', EPSG3301);

// Create Leaflet CRS
const crs3301 = new L.Proj.CRS(
    'EPSG:3301',
    EPSG3301,
    {
        origin: [40500, 5993000],
        resolutions: [
            4000, 2000, 1000, 500, 250, 125, 62.5, 31.25, 15.625, 7.8125,
            3.90625, 1.953125, 0.9765625, 0.48828125, 0.244140625
        ],
        bounds: L.bounds([40500, 5993000], [1064500, 7017000])
    }
);

// A3 aspect ratios
const A3_RATIO_LANDSCAPE = Math.sqrt(2); // ~1.414
const A3_RATIO_PORTRAIT = 1 / Math.sqrt(2); // ~0.707

// Map bounds (Estonia coverage)
const ESTONIA_BOUNDS = {
    minX: 282560,
    maxX: 751541,
    minY: 6375274,
    maxY: 6658861
};

// State
let map;
let wmsLayer;
let orthoLayer;
let currentLayer = 'mapant';
let selectionRectangle = null;
let isDrawing = false;
let drawStartPoint = null;
let currentBounds = null;

// DOM Elements
const drawBtn = document.getElementById('draw-btn');
const clearBtn = document.getElementById('clear-btn');
const exportBtn = document.getElementById('export-btn');
const orientationSelect = document.getElementById('orientation');
const layerSelect = document.getElementById('layer-select');
const loadingOverlay = document.getElementById('loading');
const selectionInfo = document.getElementById('selection-info');
const attributionText = document.getElementById('attribution');

// Layer attributions
const LAYER_ATTRIBUTIONS = {
    'mapant': 'Map data: MapAnt Estonia (CC BY 4.0)',
    'ortho': 'Map data: Maa-amet Orthophoto'
};

// Initialize map
function initMap() {
    // Calculate center of Estonia in EPSG:3301
    const centerX = (ESTONIA_BOUNDS.minX + ESTONIA_BOUNDS.maxX) / 2;
    const centerY = (ESTONIA_BOUNDS.minY + ESTONIA_BOUNDS.maxY) / 2;

    // Convert to lat/lng for Leaflet
    const centerLatLng = proj4('EPSG:3301', 'WGS84', [centerX, centerY]);

    map = L.map('map', {
        crs: crs3301,
        center: [centerLatLng[1], centerLatLng[0]],
        zoom: 4,
        minZoom: 0,
        maxZoom: 14
    });

    // Add MapAnt WMS layer
    wmsLayer = L.tileLayer.wms('https://mapantee.gokartor.se/ogc/wms.php', {
        layers: 'mapantee',
        format: 'image/png',
        transparent: true,
        attribution: 'MapAnt Estonia (CC BY 4.0)',
        maxZoom: 14
    });

    // Add Orthophoto layer (Maaamet WMS) - supports EPSG:3301
    orthoLayer = L.tileLayer.wms('https://kaart.maaamet.ee/wms/fotokaart', {
        layers: 'EESTIFOTO',
        format: 'image/jpeg',
        transparent: false,
        attribution: 'Maa-amet',
        maxZoom: 14
    });

    // Add layer based on current selection (handles browser form caching)
    if (layerSelect.value === 'ortho') {
        orthoLayer.addTo(map);
        currentLayer = 'ortho';
        attributionText.textContent = LAYER_ATTRIBUTIONS['ortho'];
    } else {
        wmsLayer.addTo(map);
        currentLayer = 'mapant';
    }

    // Setup event listeners
    setupDrawing();
    setupButtons();
    setupLayerToggle();
}

// Setup layer toggle
function setupLayerToggle() {
    layerSelect.addEventListener('change', () => {
        const selectedLayer = layerSelect.value;

        if (selectedLayer === 'ortho' && currentLayer !== 'ortho') {
            map.removeLayer(wmsLayer);
            orthoLayer.addTo(map);
            currentLayer = 'ortho';
        } else if (selectedLayer === 'mapant' && currentLayer !== 'mapant') {
            map.removeLayer(orthoLayer);
            wmsLayer.addTo(map);
            currentLayer = 'mapant';
        }

        // Update attribution text
        attributionText.textContent = LAYER_ATTRIBUTIONS[selectedLayer];
    });
}

// Setup rectangle drawing
function setupDrawing() {
    map.on('mousedown', onMouseDown);
    map.on('mousemove', onMouseMove);
    map.on('mouseup', onMouseUp);
}

function onMouseDown(e) {
    if (!isDrawing) return;

    drawStartPoint = e.latlng;

    // Remove existing rectangle
    if (selectionRectangle) {
        map.removeLayer(selectionRectangle);
    }

    // Disable map dragging while drawing
    map.dragging.disable();
}

function onMouseMove(e) {
    if (!isDrawing || !drawStartPoint) return;

    const currentPoint = e.latlng;
    const bounds = calculateA3Bounds(drawStartPoint, currentPoint);

    if (selectionRectangle) {
        selectionRectangle.setBounds(bounds);
    } else {
        selectionRectangle = L.rectangle(bounds, {
            color: '#e74c3c',
            weight: 2,
            fillOpacity: 0.2,
            dashArray: '5, 5'
        }).addTo(map);
    }

    updateSelectionInfo(bounds);
}

function onMouseUp(e) {
    if (!isDrawing || !drawStartPoint) return;

    map.dragging.enable();

    if (selectionRectangle) {
        currentBounds = selectionRectangle.getBounds();
        updateSelectionInfo(currentBounds);

        // Update UI
        clearBtn.disabled = false;
        exportBtn.disabled = false;

        // Update rectangle style to solid
        selectionRectangle.setStyle({
            dashArray: null,
            fillOpacity: 0.15
        });
    }

    // Exit drawing mode
    isDrawing = false;
    drawStartPoint = null;
    drawBtn.classList.remove('active');
    drawBtn.textContent = 'Draw Rectangle';
    map.getContainer().style.cursor = '';
}

// Calculate bounds maintaining A3 aspect ratio
function calculateA3Bounds(startLatLng, endLatLng) {
    // Convert to EPSG:3301 for accurate measurements
    const start3301 = proj4('WGS84', 'EPSG:3301', [startLatLng.lng, startLatLng.lat]);
    const end3301 = proj4('WGS84', 'EPSG:3301', [endLatLng.lng, endLatLng.lat]);

    const dx = end3301[0] - start3301[0];
    const dy = end3301[1] - start3301[1];

    const orientation = orientationSelect.value;
    const ratio = orientation === 'landscape' ? A3_RATIO_LANDSCAPE : A3_RATIO_PORTRAIT;

    let width, height;

    // Use the larger dimension to determine size, maintain aspect ratio
    if (Math.abs(dx) / ratio > Math.abs(dy)) {
        width = Math.abs(dx);
        height = width / ratio;
    } else {
        height = Math.abs(dy);
        width = height * ratio;
    }

    // Cap max size to 20km for either dimension
    const MAX_SIZE = 20000; // 20km in meters
    if (width > MAX_SIZE) {
        width = MAX_SIZE;
        height = width / ratio;
    }
    if (height > MAX_SIZE) {
        height = MAX_SIZE;
        width = height * ratio;
    }

    // Apply direction
    const signX = dx >= 0 ? 1 : -1;
    const signY = dy >= 0 ? 1 : -1;

    const endX = start3301[0] + width * signX;
    const endY = start3301[1] + height * signY;

    // Convert corners back to lat/lng
    const sw = proj4('EPSG:3301', 'WGS84', [
        Math.min(start3301[0], endX),
        Math.min(start3301[1], endY)
    ]);
    const ne = proj4('EPSG:3301', 'WGS84', [
        Math.max(start3301[0], endX),
        Math.max(start3301[1], endY)
    ]);

    return L.latLngBounds(
        L.latLng(sw[1], sw[0]),
        L.latLng(ne[1], ne[0])
    );
}

function updateSelectionInfo(bounds) {
    if (!bounds) {
        selectionInfo.style.display = 'none';
        return;
    }

    // Convert to EPSG:3301 for measurements
    const sw = bounds.getSouthWest();
    const ne = bounds.getNorthEast();

    const sw3301 = proj4('WGS84', 'EPSG:3301', [sw.lng, sw.lat]);
    const ne3301 = proj4('WGS84', 'EPSG:3301', [ne.lng, ne.lat]);

    const width = Math.abs(ne3301[0] - sw3301[0]);
    const height = Math.abs(ne3301[1] - sw3301[1]);

    selectionInfo.style.display = 'block';
    selectionInfo.innerHTML = `
        <strong>Selection:</strong><br>
        Size: ${(width).toFixed(0)}m x ${(height).toFixed(0)}m<br>
        <span class="coords">
            SW: ${sw3301[0].toFixed(0)}, ${sw3301[1].toFixed(0)}<br>
            NE: ${ne3301[0].toFixed(0)}, ${ne3301[1].toFixed(0)}
        </span>
    `;
}

// Setup button handlers
function setupButtons() {
    drawBtn.addEventListener('click', () => {
        if (isDrawing) {
            // Cancel drawing
            isDrawing = false;
            drawBtn.classList.remove('active');
            drawBtn.textContent = 'Draw Rectangle';
            map.getContainer().style.cursor = '';
            map.dragging.enable();
            drawStartPoint = null;
        } else {
            // Start drawing
            isDrawing = true;
            drawBtn.classList.add('active');
            drawBtn.textContent = 'Cancel Drawing';
            map.getContainer().style.cursor = 'crosshair';
        }
    });

    clearBtn.addEventListener('click', () => {
        if (selectionRectangle) {
            map.removeLayer(selectionRectangle);
            selectionRectangle = null;
        }
        currentBounds = null;
        clearBtn.disabled = true;
        exportBtn.disabled = true;
        selectionInfo.style.display = 'none';
    });

    exportBtn.addEventListener('click', exportMap);

    // Update rectangle when orientation changes
    orientationSelect.addEventListener('change', () => {
        if (selectionRectangle && currentBounds) {
            // Recalculate with new aspect ratio
            const center = currentBounds.getCenter();
            const sw = currentBounds.getSouthWest();
            const ne = currentBounds.getNorthEast();

            // Get current size in meters
            const sw3301 = proj4('WGS84', 'EPSG:3301', [sw.lng, sw.lat]);
            const ne3301 = proj4('WGS84', 'EPSG:3301', [ne.lng, ne.lat]);

            const currentWidth = Math.abs(ne3301[0] - sw3301[0]);
            const currentHeight = Math.abs(ne3301[1] - sw3301[1]);
            const currentArea = currentWidth * currentHeight;

            // Calculate new dimensions with same area but new ratio
            const orientation = orientationSelect.value;
            const ratio = orientation === 'landscape' ? A3_RATIO_LANDSCAPE : A3_RATIO_PORTRAIT;

            const newHeight = Math.sqrt(currentArea / ratio);
            const newWidth = newHeight * ratio;

            // Convert center to EPSG:3301
            const center3301 = proj4('WGS84', 'EPSG:3301', [center.lng, center.lat]);

            // Calculate new bounds
            const newSW = proj4('EPSG:3301', 'WGS84', [
                center3301[0] - newWidth / 2,
                center3301[1] - newHeight / 2
            ]);
            const newNE = proj4('EPSG:3301', 'WGS84', [
                center3301[0] + newWidth / 2,
                center3301[1] + newHeight / 2
            ]);

            currentBounds = L.latLngBounds(
                L.latLng(newSW[1], newSW[0]),
                L.latLng(newNE[1], newNE[0])
            );

            selectionRectangle.setBounds(currentBounds);
            updateSelectionInfo(currentBounds);
        }
    });
}

// Export map
async function exportMap() {
    if (!currentBounds) {
        alert('Please draw a selection rectangle first.');
        return;
    }

    const orientation = orientationSelect.value;
    const layer = layerSelect.value;

    // Convert bounds to EPSG:3301
    const sw = currentBounds.getSouthWest();
    const ne = currentBounds.getNorthEast();

    const sw3301 = proj4('WGS84', 'EPSG:3301', [sw.lng, sw.lat]);
    const ne3301 = proj4('WGS84', 'EPSG:3301', [ne.lng, ne.lat]);

    const bbox = {
        minx: sw3301[0],
        miny: sw3301[1],
        maxx: ne3301[0],
        maxy: ne3301[1]
    };

    // Show loading
    loadingOverlay.classList.remove('hidden');
    exportBtn.disabled = true;

    try {
        const response = await fetch('/api/export', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ bbox, orientation, layer })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Export failed');
        }

        // Download the image
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `mapant_a3_${orientation}.png`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

    } catch (error) {
        console.error('Export error:', error);
        alert('Export failed: ' + error.message);
    } finally {
        loadingOverlay.classList.add('hidden');
        exportBtn.disabled = false;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initMap);
