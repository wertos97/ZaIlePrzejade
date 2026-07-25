// MPK Kraków - Ticket Cost Calculator
// Main application logic

// Global state
const state = {
    map: null,
    stopGroups: [],
    markers: [],
    routeLayer: null,
    fromStop: null,
    toStop: null,
    searchTimeout: null,
    routeMode: 'short',
    hasRoute: false,
};

// Single color for all stops
const STOP_COLOR = '#3498db';

// Route colors for route lines
const ROUTE_COLORS = [
    '#e74c3c', '#3498db', '#9b59b6', '#f39c12', '#1abc9c',
    '#e67e22', '#9b59b6', '#34495e', '#16a085', '#c0392b',
];

// ============================================================
// Initialization
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
    initMap();
    loadStops();
    loadRouteInfo();
    setupEventListeners();
});

function initMap() {
    state.map = L.map('map', {
        zoomControl: true,
    }).setView([50.0647, 19.9450], 13);

    // Use a cleaner tile style - CartoDB Positron (light, no POI clutter)
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, &copy; <a href="https://carto.com/">CARTO</a>',
        maxZoom: 19,
        subdomains: 'abcd',
    }).addTo(state.map);

    state.routeLayer = L.layerGroup().addTo(state.map);
}

function setupEventListeners() {
    const fromInput = document.getElementById('from-search');
    const toInput = document.getElementById('to-search');

    fromInput.addEventListener('input', function(e) {
        handleSearch(e.target, 'from-results', 'from');
    });

    toInput.addEventListener('input', function(e) {
        handleSearch(e.target, 'to-results', 'to');
    });

    document.addEventListener('click', function(e) {
        if (!e.target.closest('.search-box')) {
            document.querySelectorAll('.search-results').forEach(el => el.classList.remove('show'));
        }
        // Close custom popup only if clicking outside the popup itself
        if (customPopupEl && !customPopupEl.contains(e.target) && !e.target.closest('.leaflet-marker-icon') && !e.target.closest('.leaflet-interactive')) {
            closeCustomPopup();
        }
    });

    document.getElementById('swap-btn').addEventListener('click', function() {
        const fromInput = document.getElementById('from-search');
        const toInput = document.getElementById('to-search');
        const temp = fromInput.value;
        fromInput.value = toInput.value;
        toInput.value = temp;
        const tempStop = state.fromStop;
        state.fromStop = state.toStop;
        state.toStop = tempStop;
        updateSelectedStops();
        if (state.fromStop && state.toStop) {
            findRoute();
        }
    });

    document.getElementById('clear-btn').addEventListener('click', function() {
        clearRoute();
    });

    document.querySelectorAll('.btn-mode').forEach(btn => {
        btn.addEventListener('click', function() {
            const mode = this.dataset.mode;
            setRouteMode(mode);
        });
    });

    // Modal buttons
    document.getElementById('btn-info').addEventListener('click', function() {
        showModal('Informacja', 'info.txt');
    });

    document.getElementById('btn-warning').addEventListener('click', function() {
        showModal('Uwaga', 'warning.txt');
    });

    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('modal-overlay').addEventListener('click', closeModal);

}

function clearRoute() {
    state.fromStop = null;
    state.toStop = null;
    state.hasRoute = false;
    state.routeStopIds = null;
    document.getElementById('from-search').value = '';
    document.getElementById('to-search').value = '';
    document.getElementById('right-panel').style.display = 'none';
    document.querySelector('.main-content').classList.remove('with-result');
    state.routeLayer.clearLayers();
    updateSelectedStops();
}

function setRouteMode(mode) {
    state.routeMode = mode;
    document.querySelectorAll('.btn-mode').forEach(btn => {
        btn.classList.toggle('btn-mode-active', btn.dataset.mode === mode);
    });

    if (state.fromStop && state.toStop) {
        findRoute();
    }
}

// ============================================================
// Modal
// ============================================================

async function showModal(title, file) {
    try {
        const response = await fetch('/' + file);
        const text = await response.text();
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').textContent = text;
        document.getElementById('modal-overlay').style.display = 'block';
        document.getElementById('modal').style.display = 'flex';
    } catch (error) {
        console.error('Error loading modal content:', error);
    }
}

function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    document.getElementById('modal').style.display = 'none';
}

// ============================================================
// Custom popup for stop selection
// ============================================================

let customPopupEl = null;

function showCustomPopup(group, latlng) {
    closeCustomPopup();
    
    customPopupEl = document.createElement('div');
    customPopupEl.className = 'stop-popup';
    customPopupEl.innerHTML = `
        <div class="stop-popup-header">${escapeHtml(group.name)}</div>
        <div class="stop-popup-btn" data-action="from">🚀 Wybierz jako początek</div>
        <div class="stop-popup-btn" data-action="to">🎯 Wybierz jako koniec</div>
    `;
    
    customPopupEl.querySelector('[data-action="from"]').addEventListener('click', function(e) {
        e.stopPropagation();
        state.fromStop = group;
        document.getElementById('from-search').value = group.name;
        closeCustomPopup();
        updateSelectedStops();
        if (state.fromStop && state.toStop) {
            findRoute();
        }
    });
    
    customPopupEl.querySelector('[data-action="to"]').addEventListener('click', function(e) {
        e.stopPropagation();
        state.toStop = group;
        document.getElementById('to-search').value = group.name;
        closeCustomPopup();
        updateSelectedStops();
        if (state.fromStop && state.toStop) {
            findRoute();
        }
    });
    
    document.body.appendChild(customPopupEl);
    
    // Position popup near the click position
    const clientX = latlng.originalEvent ? latlng.originalEvent.clientX : (latlng.containerPoint ? latlng.containerPoint.x + 340 : window.innerWidth / 2);
    const clientY = latlng.originalEvent ? latlng.originalEvent.clientY : (latlng.latlng ? 200 : window.innerHeight / 2);
    
    customPopupEl.style.left = Math.min(clientX, window.innerWidth - 220) + 'px';
    customPopupEl.style.top = Math.min(clientY, window.innerHeight - 130) + 'px';
}

function closeCustomPopup() {
    if (customPopupEl) {
        customPopupEl.remove();
        customPopupEl = null;
    }
}

// ============================================================
// Stop Loading and Display
// ============================================================

async function loadStops() {
    try {
        const response = await fetch('/api/stops');
        const stopGroups = await response.json();
        state.stopGroups = stopGroups;

        stopGroups.forEach(group => {
            const marker = L.circleMarker([group.lat, group.lon], {
                radius: 5,
                fillColor: STOP_COLOR,
                color: '#fff',
                weight: 1.5,
                opacity: 1,
                fillOpacity: 0.7,
            });

            marker.on('click', function(e) {
                selectStop(group, e);
            });

            marker.stopData = group;
            marker.addTo(state.map);
            state.markers.push(marker);
        });

        console.log(`Loaded ${stopGroups.length} stop groups`);
    } catch (error) {
        console.error('Error loading stops:', error);
    }
}

function getModeLabel(mode) {
    switch (mode) {
        case 'tram': return '🚊 Tramwaj';
        case 'bus': return '🚌 Autobus';
        case 'mobilis': return '🚍 Mobilis';
        default: return mode;
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============================================================
// Search Functionality
// ============================================================

function handleSearch(input, resultsId, field) {
    const query = input.value.trim();
    const resultsEl = document.getElementById(resultsId);

    if (query.length < 2) {
        resultsEl.classList.remove('show');
        resultsEl.innerHTML = '';
        return;
    }

    clearTimeout(state.searchTimeout);
    state.searchTimeout = setTimeout(async function() {
        try {
            const response = await fetch(`/api/stops/search?q=${encodeURIComponent(query)}`);
            const results = await response.json();

            resultsEl.innerHTML = '';
            if (results.length === 0) {
                resultsEl.innerHTML = '<div class="search-item" style="color: #999;">Brak wyników</div>';
            } else {
                results.forEach(group => {
                    const item = document.createElement('div');
                    item.className = 'search-item';
                    const modeLabels = group.modes.map(m => getModeLabel(m).replace(/[^a-zA-Ząęćłńóśźż]/g, '')).join(', ');
                    const platformInfo = group.platform_count > 1 ? ` · ${group.platform_count} perony` : '';
                    item.innerHTML = `
                        <div class="name">${escapeHtml(group.name)}</div>
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <span class="code">${modeLabels}${platformInfo}</span>
                            <span class="mode" style="background: ${STOP_COLOR}; color: white;">${group.modes.join('/').toUpperCase()}</span>
                        </div>
                    `;
                    item.addEventListener('click', function() {
                        state[field === 'from' ? 'fromStop' : 'toStop'] = group;
                        input.value = group.name;
                        resultsEl.classList.remove('show');
                        updateSelectedStops();
                        if (state.fromStop && state.toStop) {
                            findRoute();
                        }
                    });
                    resultsEl.appendChild(item);
                });
            }
            resultsEl.classList.add('show');
        } catch (error) {
            console.error('Search error:', error);
        }
    }, 300);
}

// ============================================================
// Stop Selection
// ============================================================

function selectStop(group, e) {
    // Show custom popup instead of auto-selecting
    showCustomPopup(group, e);
}

function updateSelectedStops() {
    // Update marker colors and visibility
    state.markers.forEach(marker => {
        const group = marker.stopData;
        let color = STOP_COLOR;
        let radius = 5;
        let opacity = 1;

        // If route is shown, hide stops not on the route
        if (state.hasRoute && state.routeStopIds) {
            if (!state.routeStopIds.has(group.id)) {
                opacity = 0.15;
            }
        }

        if (state.fromStop && group.id === state.fromStop.id) {
            color = '#e74c3c';
            radius = 10;
        } else if (state.toStop && group.id === state.toStop.id) {
            color = '#e74c3c';
            radius = 10;
        }

        marker.setStyle({
            radius: radius,
            fillColor: color,
            color: '#fff',
            weight: 2,
            fillOpacity: opacity,
        });
    });
}

// ============================================================
// Route Finding
// ============================================================

async function findRoute() {
    if (!state.fromStop || !state.toStop) {
        return;
    }

    // Show loading indicator
    const loadingEl = document.getElementById('loading-indicator');
    loadingEl.style.display = 'flex';

    try {
        // Fetch both modes to compare
        const otherMode = state.routeMode === 'short' ? 'convenient' : 'short';
        const [currentResponse, otherResponse] = await Promise.all([
            fetch(`/api/find-route?from=${state.fromStop.id}&to=${state.toStop.id}&mode=${state.routeMode}`),
            fetch(`/api/find-route?from=${state.fromStop.id}&to=${state.toStop.id}&mode=${otherMode}`),
        ]);

        const currentResult = await currentResponse.json();
        const otherResult = await otherResponse.json();

        if (currentResult.error) {
            loadingEl.style.display = 'none';
            alert(`Błąd: ${currentResult.error}`);
            return;
        }

        // Check if both modes give the same result
        const equal = (
            !otherResult.error &&
            Math.abs(currentResult.total_distance - otherResult.total_distance) < 0.001 &&
            (currentResult.transfers || []).length === (otherResult.transfers || []).length
        );

        const equalEl = document.getElementById('mode-equal');
        if (equal) {
            equalEl.style.display = 'flex';
            // Re-trigger animation by removing and re-adding
            equalEl.style.animation = 'none';
            equalEl.offsetHeight; // trigger reflow
            equalEl.style.animation = '';
        } else {
            equalEl.style.display = 'none';
        }

        displayRoute(currentResult);
    } catch (error) {
        console.error('Route finding error:', error);
        alert('Wystąpił błąd podczas wyszukiwania trasy');
    } finally {
        loadingEl.style.display = 'none';
    }
}

function displayRoute(result) {
    state.hasRoute = true;
    state.routeLayer.clearLayers();

    // Collect stop IDs on the route for hiding others
    state.routeStopIds = new Set();
    if (result.path) {
        result.path.forEach(s => {
            state.routeStopIds.add(s.stop_id);
        });
    }

    drawRouteOnMap(result);
    displayResult(result);

    // Show right panel
    document.getElementById('right-panel').style.display = 'block';
    document.querySelector('.main-content').classList.add('with-result');

    // Update marker visibility
    updateSelectedStops();
}

function drawRouteOnMap(result) {
    const path = result.path;
    if (!path || path.length === 0) return;

    // Filter out transfer-only stops for drawing lines
    const drawPoints = path.filter(s => !s.is_transfer);

    let currentRouteId = null;
    let currentPoints = [];
    let colorIndex = 0;

    for (let i = 0; i < drawPoints.length; i++) {
        const stop = drawPoints[i];
        const routeId = stop.route_id;

        if (routeId && routeId !== currentRouteId) {
            if (currentPoints.length > 1) {
                const color = ROUTE_COLORS[colorIndex % ROUTE_COLORS.length];
                L.polyline(currentPoints, {
                    color: color,
                    weight: 5,
                    opacity: 0.7,
                    smoothFactor: 1,
                }).addTo(state.routeLayer);
                colorIndex++;
            }
            if (currentPoints.length > 0) {
                currentPoints = [currentPoints[currentPoints.length - 1], [stop.lat, stop.lon]];
            } else {
                currentPoints = [[stop.lat, stop.lon]];
            }
            currentRouteId = routeId;
        } else {
            currentPoints.push([stop.lat, stop.lon]);
        }
    }

    if (currentPoints.length > 1) {
        const color = ROUTE_COLORS[colorIndex % ROUTE_COLORS.length];
        L.polyline(currentPoints, {
            color: color,
            weight: 5,
            opacity: 0.7,
            smoothFactor: 1,
        }).addTo(state.routeLayer);
    }

    // Draw stop markers on route
    path.forEach((stop, index) => {
        let markerColor = '#3498db';
        let markerRadius = 5;

        if (index === 0) {
            markerColor = '#27ae60';
            markerRadius = 8;
        } else if (index === path.length - 1) {
            markerColor = '#e74c3c';
            markerRadius = 8;
        } else if (stop.is_transfer) {
            markerColor = '#f39c12';
            markerRadius = 7;
        }

        L.circleMarker([stop.lat, stop.lon], {
            radius: markerRadius,
            fillColor: markerColor,
            color: '#fff',
            weight: 2,
            fillOpacity: 1,
        }).addTo(state.routeLayer);
    });

    // Fit bounds with padding for the right panel (340px)
    const bounds = L.latLngBounds(path.map(s => [s.lat, s.lon]));
    state.map.fitBounds(bounds, {
        paddingTopLeft: [0, 20],
        paddingBottomRight: [340, 20],
        maxZoom: 16,
    });
}

function displayResult(result) {
    document.getElementById('result-distance').textContent =
        `${result.total_distance.toFixed(2)} km`;

    document.getElementById('result-regular').textContent =
        `${result.cost_regular.toFixed(2)} zł`;
    document.getElementById('result-reduced').textContent =
        `${result.cost_reduced.toFixed(2)} zł`;

    const transferCount = result.transfers ? result.transfers.length : 0;
    document.getElementById('result-transfers').textContent =
        transferCount > 0 ? `${transferCount} przesiadka(e/k)` : 'Bez przesiadek';

    const stepsEl = document.getElementById('route-steps');
    stepsEl.innerHTML = '';

    const stopNameLookup = {};
    if (result.path) {
        result.path.forEach(s => {
            stopNameLookup[s.stop_id] = s.name;
        });
    }

    // Display segments interleaved with transfers
    if (result.segments && result.segments.length > 0) {
        result.segments.forEach((segment, index) => {
            const route = getRouteInfo(segment.route_id);
            const firstStopName = segment.first_stop_name || stopNameLookup[segment.stops[0]] || segment.stops[0];
            const lastStopName = segment.last_stop_name || stopNameLookup[segment.stops[segment.stops.length - 1]] || segment.stops[segment.stops.length - 1];
            const modeIcon = segment.mode === 'tram' ? '🚊' : segment.mode === 'bus' ? '🚌' : '🚍';
            // Get all routes that serve this segment
            let routeNames = [];
            if (segment.all_routes && segment.all_routes.length > 0) {
                routeNames = segment.all_routes.map(rid => {
                    const r = getRouteInfo(rid);
                    return r ? r.short_name : rid;
                });
            } else if (route) {
                routeNames = [route.short_name];
            } else {
                routeNames = [segment.route_id];
            }
            const routeLabel = routeNames.join(', ');

            const stepEl = document.createElement('div');
            stepEl.className = 'route-step';
            stepEl.innerHTML = `
                <div class="step-header">
                    <span class="step-title">${modeIcon} Przejazd</span>
                    <span class="step-distance">${segment.distance.toFixed(2)} km</span>
                </div>
                <div class="step-routes">Linie: ${escapeHtml(routeLabel)}</div>
                <div class="step-detail">
                    ${escapeHtml(firstStopName)} → ${escapeHtml(lastStopName)}
                </div>
                <div class="step-detail" style="margin-top: 2px; color: #999;">
                    ${segment.stops.length} przystanków
                </div>
            `;
            stepsEl.appendChild(stepEl);
            
            // Show transfer after this segment
            if (result.transfers && index < result.transfers.length) {
                const transfer = result.transfers[index];
                const transferStopName = transfer.stop_name || stopNameLookup[transfer.stop_id] || transfer.stop_id;
                const fromRoute = getRouteInfo(transfer.from_route);
                const toRoute = getRouteInfo(transfer.to_route);
                const transferEl = document.createElement('div');
                transferEl.className = 'route-step transfer';
                transferEl.innerHTML = `
                    <div class="step-header">
                        <span class="step-title">🔄 Przesiadka</span>
                    </div>
                    <div class="step-detail" style="font-weight: 500; margin-bottom: 2px;">
                        <strong>${escapeHtml(transferStopName)}</strong>
                    </div>
                    <div class="step-detail" style="color: #888;">
                        ${fromRoute ? `linia ${fromRoute.short_name}` : '?'} → ${toRoute ? `linia ${toRoute.short_name}` : '?'}
                    </div>
                `;
                stepsEl.appendChild(transferEl);
            }
        });
    }
}

// ============================================================
// Utility Functions
// ============================================================

async function loadRouteInfo() {
    try {
        const response = await fetch('/api/routes');
        const routes = await response.json();
        state.routes = {};
        routes.forEach(r => {
            state.routes[r.route_id] = r;
        });
    } catch (error) {
        console.error('Error loading routes:', error);
    }
}

function getRouteInfo(routeId) {
    if (state.routes && state.routes[routeId]) {
        const route = state.routes[routeId];
        return {
            short_name: route.short_name,
            mode: route.mode,
        };
    }
    return null;
}