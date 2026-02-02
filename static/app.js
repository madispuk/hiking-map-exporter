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
let selectionPolygon = null;
let isDrawing = false;
let drawStartPoint = null;
let currentBounds3301 = null; // Stored in EPSG:3301 {minX, minY, maxX, maxY}

// Mobile state
let isTouchDevice = false;
const MOBILE_OVERLAY_SCALE = 0.7; // Overlay takes 70% of screen (smaller dimension)

// Number of points per edge for grid-aligned polygon
const POINTS_PER_EDGE = 20;

const MAX_SELECTION_SIZE = 20000; // 20km max in meters

// DOM Elements
const drawBtn = document.getElementById('draw-btn');
const clearBtn = document.getElementById('clear-btn');
const exportBtn = document.getElementById('export-btn');
const orientationSelect = document.getElementById('orientation');
const layerSelect = document.getElementById('layer-select');
const loadingOverlay = document.getElementById('loading');
const selectionInfo = document.getElementById('selection-info');
const attributionText = document.getElementById('attribution');

// Mobile DOM Elements
const selectionOverlay = document.getElementById('selection-overlay');
const overlaySizeLabel = document.getElementById('overlay-size-label');

// Layer attributions
const LAYER_ATTRIBUTIONS = {
    'mapant': 'Map data: MapAnt Estonia (CC BY 4.0)',
    'ortho': 'Map data: Maa-amet Orthophoto'
};

/**
 * Create polygon points that follow EPSG:3301 grid lines exactly.
 * This ensures the drawn polygon matches the exported image bounds.
 */
function createGridAlignedPolygon(bounds3301) {
    const { minX, minY, maxX, maxY } = bounds3301;
    const points = [];

    // Top edge: left to right (Y = maxY, X varies)
    for (let i = 0; i <= POINTS_PER_EDGE; i++) {
        const x = minX + (maxX - minX) * (i / POINTS_PER_EDGE);
        const y = maxY;
        const latLng = proj4('EPSG:3301', 'WGS84', [x, y]);
        points.push(L.latLng(latLng[1], latLng[0]));
    }

    // Right edge: top to bottom (X = maxX, Y varies)
    for (let i = 1; i <= POINTS_PER_EDGE; i++) {
        const x = maxX;
        const y = maxY - (maxY - minY) * (i / POINTS_PER_EDGE);
        const latLng = proj4('EPSG:3301', 'WGS84', [x, y]);
        points.push(L.latLng(latLng[1], latLng[0]));
    }

    // Bottom edge: right to left (Y = minY, X varies)
    for (let i = 1; i <= POINTS_PER_EDGE; i++) {
        const x = maxX - (maxX - minX) * (i / POINTS_PER_EDGE);
        const y = minY;
        const latLng = proj4('EPSG:3301', 'WGS84', [x, y]);
        points.push(L.latLng(latLng[1], latLng[0]));
    }

    // Left edge: bottom to top (X = minX, Y varies) - skip last point (it's the first point)
    for (let i = 1; i < POINTS_PER_EDGE; i++) {
        const x = minX;
        const y = minY + (maxY - minY) * (i / POINTS_PER_EDGE);
        const latLng = proj4('EPSG:3301', 'WGS84', [x, y]);
        points.push(L.latLng(latLng[1], latLng[0]));
    }

    return points;
}

/**
 * Detect if this is a touch device
 */
function detectTouchDevice() {
    return ('ontouchstart' in window) ||
           (navigator.maxTouchPoints > 0) ||
           (navigator.msMaxTouchPoints > 0);
}

/**
 * Get the current overlay dimensions in pixels (fixed to screen size)
 */
function getOverlayDimensions() {
    // Use viewport dimensions since overlay is positioned relative to viewport
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    const orientation = orientationSelect.value;
    const ratio = orientation === 'landscape' ? A3_RATIO_LANDSCAPE : A3_RATIO_PORTRAIT;

    // Calculate overlay size to fit within viewport with current scale
    const maxWidth = viewportWidth * MOBILE_OVERLAY_SCALE;
    const maxHeight = viewportHeight * MOBILE_OVERLAY_SCALE;

    let overlayWidth, overlayHeight;

    if (maxWidth / ratio <= maxHeight) {
        // Width is the limiting factor
        overlayWidth = maxWidth;
        overlayHeight = maxWidth / ratio;
    } else {
        // Height is the limiting factor
        overlayHeight = maxHeight;
        overlayWidth = maxHeight * ratio;
    }

    return { width: overlayWidth, height: overlayHeight };
}

/**
 * Calculate selection bounds from overlay position for mobile mode
 */
function calculateBoundsFromCenter() {
    const mapContainer = map.getContainer();
    const mapRect = mapContainer.getBoundingClientRect();

    const { width: overlayWidth, height: overlayHeight } = getOverlayDimensions();

    // Overlay is centered on viewport, get viewport center
    const viewportCenterX = window.innerWidth / 2;
    const viewportCenterY = window.innerHeight / 2;

    // Convert viewport coordinates to map container coordinates
    const mapCenterX = viewportCenterX - mapRect.left;
    const mapCenterY = viewportCenterY - mapRect.top;

    // Get the lat/lng coordinates of the overlay corners (relative to map container)
    const topLeftPx = L.point(mapCenterX - overlayWidth / 2, mapCenterY - overlayHeight / 2);
    const bottomRightPx = L.point(mapCenterX + overlayWidth / 2, mapCenterY + overlayHeight / 2);

    const topLeftLatLng = map.containerPointToLatLng(topLeftPx);
    const bottomRightLatLng = map.containerPointToLatLng(bottomRightPx);

    // Convert to EPSG:3301
    const topLeft3301 = proj4('WGS84', 'EPSG:3301', [topLeftLatLng.lng, topLeftLatLng.lat]);
    const bottomRight3301 = proj4('WGS84', 'EPSG:3301', [bottomRightLatLng.lng, bottomRightLatLng.lat]);

    return {
        minX: Math.min(topLeft3301[0], bottomRight3301[0]),
        minY: Math.min(topLeft3301[1], bottomRight3301[1]),
        maxX: Math.max(topLeft3301[0], bottomRight3301[0]),
        maxY: Math.max(topLeft3301[1], bottomRight3301[1])
    };
}

/**
 * Update the mobile selection overlay size and label
 */
function updateMobileOverlay() {
    if (!isTouchDevice || !map) return;

    const { width: overlayWidth, height: overlayHeight } = getOverlayDimensions();

    // Set fixed overlay size
    selectionOverlay.style.width = overlayWidth + 'px';
    selectionOverlay.style.height = overlayHeight + 'px';

    // Calculate geographic size for the label
    const bounds = calculateBoundsFromCenter();
    const geoWidth = bounds.maxX - bounds.minX;
    const geoHeight = bounds.maxY - bounds.minY;

    // Check if exceeds max size
    const exceedsMax = geoWidth > MAX_SELECTION_SIZE || geoHeight > MAX_SELECTION_SIZE;

    // Update size label
    let label;
    if (geoWidth >= 1000) {
        label = `${(geoWidth / 1000).toFixed(1)}km × ${(geoHeight / 1000).toFixed(1)}km`;
    } else {
        label = `${geoWidth.toFixed(0)}m × ${geoHeight.toFixed(0)}m`;
    }

    if (exceedsMax) {
        label += ' (zoom in)';
        selectionOverlay.style.borderColor = '#f39c12'; // Orange = too large
        exportBtn.disabled = true;
    } else {
        selectionOverlay.style.borderColor = '#e74c3c'; // Red = valid
        exportBtn.disabled = false;
    }

    overlaySizeLabel.textContent = label;
}

/**
 * Setup mobile mode
 */
function setupMobileMode() {
    // Add touch-device class to body
    document.body.classList.add('touch-device');

    // Show the selection overlay
    selectionOverlay.classList.add('visible');

    // Update overlay on map move/zoom
    map.on('move', updateMobileOverlay);
    map.on('zoom', updateMobileOverlay);
    map.on('resize', updateMobileOverlay);

    // Initial overlay update
    updateMobileOverlay();
}

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

    // Detect touch device and setup mobile mode
    isTouchDevice = detectTouchDevice();
    if (isTouchDevice) {
        setupMobileMode();
    }
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

    // Remove existing polygon
    if (selectionPolygon) {
        map.removeLayer(selectionPolygon);
        selectionPolygon = null;
    }

    // Disable map dragging while drawing
    map.dragging.disable();
}

function onMouseMove(e) {
    if (!isDrawing || !drawStartPoint) return;

    const currentPoint = e.latlng;
    const bounds3301 = calculateA3Bounds(drawStartPoint, currentPoint);
    const polygonPoints = createGridAlignedPolygon(bounds3301);

    // Check if selection is at max size (capped)
    const width = bounds3301.maxX - bounds3301.minX;
    const height = bounds3301.maxY - bounds3301.minY;
    const isAtMax = width >= MAX_SELECTION_SIZE - 1 || height >= MAX_SELECTION_SIZE - 1;
    const polygonColor = isAtMax ? '#e74c3c' : '#27ae60'; // Red if at max, green if valid

    if (selectionPolygon) {
        selectionPolygon.setLatLngs(polygonPoints);
        selectionPolygon.setStyle({ color: polygonColor });
    } else {
        selectionPolygon = L.polygon(polygonPoints, {
            color: polygonColor,
            weight: 2,
            fillOpacity: 0,
            dashArray: '5, 5'
        }).addTo(map);
    }

    currentBounds3301 = bounds3301;
    updateSelectionInfo(bounds3301);
}

function onMouseUp(e) {
    if (!isDrawing || !drawStartPoint) return;

    map.dragging.enable();

    if (selectionPolygon && currentBounds3301) {
        updateSelectionInfo(currentBounds3301);

        // Check if at max size
        const width = currentBounds3301.maxX - currentBounds3301.minX;
        const height = currentBounds3301.maxY - currentBounds3301.minY;
        const isAtMax = width >= MAX_SELECTION_SIZE - 1 || height >= MAX_SELECTION_SIZE - 1;
        const polygonColor = isAtMax ? '#e74c3c' : '#27ae60';

        // Update UI
        clearBtn.disabled = false;
        exportBtn.disabled = false;

        // Update polygon style to solid
        selectionPolygon.setStyle({
            color: polygonColor,
            dashArray: null,
            fillOpacity: 0
        });
    }

    // Exit drawing mode
    isDrawing = false;
    drawStartPoint = null;
    drawBtn.classList.remove('active');
    drawBtn.textContent = 'Draw Rectangle';
    map.getContainer().style.cursor = '';
}

// Calculate bounds maintaining A3 aspect ratio - returns EPSG:3301 coordinates
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

    // Return bounds in EPSG:3301
    return {
        minX: Math.min(start3301[0], endX),
        minY: Math.min(start3301[1], endY),
        maxX: Math.max(start3301[0], endX),
        maxY: Math.max(start3301[1], endY)
    };
}

function updateSelectionInfo(bounds3301) {
    if (!bounds3301) {
        selectionInfo.style.display = 'none';
        return;
    }

    const width = bounds3301.maxX - bounds3301.minX;
    const height = bounds3301.maxY - bounds3301.minY;

    selectionInfo.style.display = 'block';
    selectionInfo.innerHTML = `
        <strong>Selection:</strong><br>
        Size: ${width.toFixed(0)}m x ${height.toFixed(0)}m<br>
        <span class="coords">
            SW: ${bounds3301.minX.toFixed(0)}, ${bounds3301.minY.toFixed(0)}<br>
            NE: ${bounds3301.maxX.toFixed(0)}, ${bounds3301.maxY.toFixed(0)}
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
        if (selectionPolygon) {
            map.removeLayer(selectionPolygon);
            selectionPolygon = null;
        }
        currentBounds3301 = null;
        clearBtn.disabled = true;
        exportBtn.disabled = true;
        selectionInfo.style.display = 'none';
    });

    exportBtn.addEventListener('click', exportMap);

    // Update polygon/overlay when orientation changes
    orientationSelect.addEventListener('change', () => {
        if (isTouchDevice) {
            // Mobile: update overlay
            updateMobileOverlay();
        } else if (selectionPolygon && currentBounds3301) {
            // Desktop: update polygon
            // Get current size and center
            const currentWidth = currentBounds3301.maxX - currentBounds3301.minX;
            const currentHeight = currentBounds3301.maxY - currentBounds3301.minY;
            const currentArea = currentWidth * currentHeight;
            const centerX = (currentBounds3301.minX + currentBounds3301.maxX) / 2;
            const centerY = (currentBounds3301.minY + currentBounds3301.maxY) / 2;

            // Calculate new dimensions with same area but new ratio
            const orientation = orientationSelect.value;
            const ratio = orientation === 'landscape' ? A3_RATIO_LANDSCAPE : A3_RATIO_PORTRAIT;

            const newHeight = Math.sqrt(currentArea / ratio);
            const newWidth = newHeight * ratio;

            // Calculate new bounds in EPSG:3301
            currentBounds3301 = {
                minX: centerX - newWidth / 2,
                minY: centerY - newHeight / 2,
                maxX: centerX + newWidth / 2,
                maxY: centerY + newHeight / 2
            };

            // Update polygon
            const polygonPoints = createGridAlignedPolygon(currentBounds3301);
            selectionPolygon.setLatLngs(polygonPoints);
            updateSelectionInfo(currentBounds3301);
        }
    });
}

// Export map
async function exportMap() {
    const orientation = orientationSelect.value;
    const layer = layerSelect.value;

    let bounds;

    if (isTouchDevice) {
        // Mobile: calculate bounds from current viewport center
        bounds = calculateBoundsFromCenter();
    } else {
        // Desktop: use drawn selection
        if (!currentBounds3301) {
            alert('Please draw a selection rectangle first.');
            return;
        }
        bounds = currentBounds3301;
    }

    const bbox = {
        minx: bounds.minX,
        miny: bounds.minY,
        maxx: bounds.maxX,
        maxy: bounds.maxY
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
        a.download = `${layer}_a3_${orientation}.png`;
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
