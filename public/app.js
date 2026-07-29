// MPK Kraków - Ticket Cost Calculator
// Main application logic

// ============================================================
// Toast notification (lightweight, no dependencies)
// ============================================================

function showToast(message, duration) {
    duration = duration || 3000;
    const existing = document.querySelector('.toast-notification');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'toast-notification';
    toast.textContent = message;
    document.body.appendChild(toast);
    requestAnimationFrame(function() { toast.classList.add('show'); });
    setTimeout(function() {
        toast.classList.remove('show');
        setTimeout(function() { toast.remove(); }, 300);
    }, duration);
}

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
    routeCache: {}, // key: "fromId_toId", value: { short: result, convenient: result }
    currentRouteKey: null,
    shouldFitBounds: true,
};

// Single color for all stops
const STOP_COLOR = '#3498db';

// ============================================================
// Initialization
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
    initMap();
    setupEventListeners();
    // Wait for both stops and routes to load before restoring from URL
    Promise.all([loadStops(), loadRouteInfo()]).then(function() {
        restoreFromURL();
    });
});

// ============================================================
// Deep linking - read/write URL params
// ============================================================

function restoreFromURL() {
    var params = new URLSearchParams(window.location.search);
    var fromId = params.get('from');
    var toId = params.get('to');
    var mode = params.get('mode');
    if (fromId && toId) {
        // Find stop groups by ID
        var fromGroup = state.stopGroups.find(function(g) { return g.id === fromId; });
        var toGroup = state.stopGroups.find(function(g) { return g.id === toId; });
        if (fromGroup && toGroup) {
            state.fromStop = fromGroup;
            state.toStop = toGroup;
            document.getElementById('from-search').value = fromGroup.name;
            document.getElementById('to-search').value = toGroup.name;
            if (mode === 'convenient') {
                setRouteMode('convenient');
            }
            updateSelectedStops();
            // Wait a tick for stops to be fully loaded on map
            setTimeout(function() { findRoute(); }, 100);
        }
    }
}

function updateURL() {
    if (state.fromStop && state.toStop) {
        var url = new URL(window.location);
        url.searchParams.set('from', state.fromStop.id);
        url.searchParams.set('to', state.toStop.id);
        url.searchParams.set('mode', state.routeMode);
        history.replaceState(null, '', url);
    } else {
        history.replaceState(null, '', window.location.pathname);
    }
}

function initMap() {
    state.map = L.map('map', {
        zoomControl: true,
        minZoom: 11,
        maxBounds: [
            [49.85, 19.60], // Southwest corner
            [50.25, 20.30]  // Northeast corner
        ],
        maxBoundsViscosity: 1.0,
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
        if (customPopupEl && !customPopupEl.contains(e.target) && !e.target.closest('.leaflet-marker-icon') && !e.target.closest('.leaflet-interactive') && !e.target.closest('.leaflet-pane')) {
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

    document.getElementById('btn-author').addEventListener('click', function() {
        showModal('Od autora', 'author.txt');
    });

    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('modal-overlay').addEventListener('click', closeModal);

    // Mobile sidebar toggle button - expand/collapse
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', function() {
            sidebar.classList.toggle('expanded');
        });
    }

    // Mobile result close (button in mode bar)
    document.getElementById('mobile-result-close').addEventListener('click', function() {
        document.getElementById('mobile-result').style.display = 'none';
    });

    // Mobile result toggle button - expand/collapse
    const mobileResultToggle = document.getElementById('mobile-result-toggle');
    const mobileResult = document.getElementById('mobile-result');

    if (mobileResultToggle) {
        mobileResultToggle.addEventListener('click', function() {
            mobileResult.classList.toggle('expanded');
        });
    }

    // Mobile mode buttons
    document.getElementById('mobile-mode-short').addEventListener('click', function() {
        setRouteMode('short');
    });
    document.getElementById('mobile-mode-convenient').addEventListener('click', function() {
        setRouteMode('convenient');
    });

}

function clearRoute() {
    state.fromStop = null;
    state.toStop = null;
    state.hasRoute = false;
    state.routeStopIds = null;
    document.getElementById('from-search').value = '';
    document.getElementById('to-search').value = '';
    document.getElementById('right-panel').style.display = 'none';
    document.getElementById('mobile-result').style.display = 'none';
    document.querySelector('.main-content').classList.remove('with-result');
    state.routeLayer.clearLayers();
    updateSelectedStops();
    updateURL();
}

function setRouteMode(mode) {
    state.routeMode = mode;
    
    // Update desktop mode buttons
    document.querySelectorAll('.btn-mode').forEach(btn => {
        btn.classList.toggle('btn-mode-active', btn.dataset.mode === mode);
    });

    // Update mobile mode buttons
    document.querySelectorAll('.btn-mode-sm').forEach(btn => {
        btn.classList.toggle('btn-mode-sm-active', btn.dataset.mode === mode);
    });

    if (state.fromStop && state.toStop) {
        findRoute();
    }
    updateURL();
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
            // Invisible larger hit area for easier clicking
            const hitArea = L.circleMarker([group.lat, group.lon], {
                radius: 14,
                fillColor: STOP_COLOR,
                fillOpacity: 0,
                color: STOP_COLOR,
                opacity: 0,
                weight: 0,
                interactive: true,
            });

            hitArea.on('click', function(e) {
                selectStop(group, e);
            });

            hitArea.stopData = group;
            hitArea.addTo(state.map);
            state.markers.push(hitArea);

            // Visible small dot marker (not interactive - click goes to hitArea)
            const dot = L.circleMarker([group.lat, group.lon], {
                radius: 5,
                fillColor: STOP_COLOR,
                color: '#fff',
                weight: 1.5,
                opacity: 1,
                fillOpacity: 0.7,
                interactive: false,
            });

            dot.addTo(state.map);
            // Store reference to dot for style updates
            hitArea.dot = dot;
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
                    item.innerHTML = `
                        <div class="name">${escapeHtml(group.name)}</div>
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <span class="code">${modeLabels}</span>
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

        // Update the visible dot (marker is the invisible hit area)
        if (marker.dot) {
            marker.dot.setStyle({
                radius: radius,
                fillColor: color,
                color: '#fff',
                weight: 2,
                fillOpacity: opacity,
            });
        }
    });
}

