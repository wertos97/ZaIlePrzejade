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

// Keyboard navigation state for search results
var searchSelectedIndex = { from: -1, to: -1 };

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
        minZoom: 8,
        maxBounds: [
            [49.55, 19.20], // Southwest corner (Małopolska)
            [50.50, 20.80]  // Northeast corner (Małopolska)
        ],
        maxBoundsViscosity: 0.8,
    }).setView([50.0647, 19.9450], 13);

    // Use a cleaner tile style - CartoDB Positron (light, no POI clutter)
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, &copy; <a href="https://carto.com/">CARTO</a>',
        maxZoom: 19,
        subdomains: 'abcd',
    }).addTo(state.map);

    state.routeLayer = L.layerGroup().addTo(state.map);

    // Add data version attribution in the bottom-left corner (Leaflet style),
    // without moving the OSM/CARTO attribution (which stays bottom-right).
    const dataInfoEl = document.createElement('div');
    dataInfoEl.className = 'leaflet-control leaflet-control-attribution data-version';
    dataInfoEl.style.cssText = 'position: absolute; left: 0px; bottom: 1px; z-index: 1000; background: rgba(255,255,255,0.8); padding: 0 5px; font-size: 11px; color: #333;';
    dataInfoEl.textContent = 'Dane: …';
    document.getElementById('map').appendChild(dataInfoEl);

    // Fetch and display the GTFS data version
    fetch('/api/data-info')
        .then(r => r.json())
        .then(info => {
            let versionText = '';
            if (info.version) {
                versionText = `Dane: ${info.version}`;
            }
            if (info.start_date && info.end_date) {
                const fmt = d => `${d.slice(6, 8)}.${d.slice(4, 6)}.${d.slice(0, 4)}`;
                versionText += versionText ? ` (${fmt(info.start_date)}–${fmt(info.end_date)})` : `Dane: ${fmt(info.start_date)}–${fmt(info.end_date)}`;
            }
            if (versionText) {
                dataInfoEl.textContent = versionText;
            } else {
                dataInfoEl.remove();
            }
        })
        .catch(() => { dataInfoEl.remove(); /* ignore - attribution is optional */ });

}

function setupEventListeners() {
    const fromInput = document.getElementById('from-search');
    const toInput = document.getElementById('to-search');

    fromInput.addEventListener('input', function(e) {
        handleSearch(e.target, 'from-results', 'from');
    });
    fromInput.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); navigateSearchResults(fromInput, 'from-results', 'from', 'down'); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); navigateSearchResults(fromInput, 'from-results', 'from', 'up'); }
        else if (e.key === 'Enter') { e.preventDefault(); selectHighlightedResult(fromInput, 'from-results', 'from'); }
    });

    toInput.addEventListener('input', function(e) {
        handleSearch(e.target, 'to-results', 'to');
    });
    toInput.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); navigateSearchResults(toInput, 'to-results', 'to', 'down'); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); navigateSearchResults(toInput, 'to-results', 'to', 'up'); }
        else if (e.key === 'Enter') { e.preventDefault(); selectHighlightedResult(toInput, 'to-results', 'to'); }
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
        showModal('O co chodzi?', 'info.md');
    });

    document.getElementById('btn-warning').addEventListener('click', function() {
        showModal('Uwaga', 'warning.md');
    });

    document.getElementById('btn-author').addEventListener('click', function() {
        showModal('Od autora', 'author.md');
    });

    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('modal-overlay').addEventListener('click', closeModal);

    // Share modal
    document.getElementById('btn-share').addEventListener('click', openShareModal);
    document.getElementById('share-close').addEventListener('click', closeShareModal);
    document.getElementById('share-overlay').addEventListener('click', closeShareModal);
    document.getElementById('share-copy').addEventListener('click', copyShareLink);

    // Share button at the end of route steps (mobile) - generated dynamically, use event delegation
    document.addEventListener('click', function(e) {
        if (e.target && e.target.id === 'btn-share-mobile') {
            openShareModal();
        }
    });

    // Mobile sidebar toggle button - expand/collapse
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    const mapEl = document.getElementById('map');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', function() {
            sidebar.classList.toggle('expanded');
            setTimeout(function() { state.map.invalidateSize(); }, 450);
        });
    }

    // Mobile result close (button in mode bar)
    document.getElementById('mobile-result-close').addEventListener('click', function() {
        document.getElementById('mobile-result').style.display = 'none';
        setTimeout(function() { state.map.invalidateSize(); }, 50);
    });

    // Mobile result toggle button - expand/collapse
    const mobileResultToggle = document.getElementById('mobile-result-toggle');
    const mobileResult = document.getElementById('mobile-result');

    if (mobileResultToggle) {
        mobileResultToggle.addEventListener('click', function() {
            mobileResult.classList.toggle('expanded');
            setTimeout(function() { state.map.invalidateSize(); }, 50);
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
    setTimeout(function() { state.map.invalidateSize(); }, 50);
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
// Simple Markdown parser
// ============================================================

function parseMarkdown(md) {
    var html = md;
    // Escape HTML (must be first) - prevent XSS from markdown content
    html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // Headers
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    // Horizontal rule
    html = html.replace(/^---$/gm, '<hr>');
    // Bold
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Links [text](url)
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // Blockquote
    html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
    // Unordered list items
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    // Wrap consecutive <li> in <ul>
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
    // Paragraphs (double newline)
    html = html.replace(/\n\n/g, '</p><p>');
    // Single newline to <br>
    html = html.replace(/\n/g, '<br>');
    // Wrap in paragraph
    html = '<p>' + html + '</p>';
    // Clean up empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');
    html = html.replace(/<p>\s*(<h[1-3]>)/g, '$1');
    html = html.replace(/(<\/h[1-3]>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<hr>)/g, '$1');
    html = html.replace(/(<hr>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<ul>)/g, '$1');
    html = html.replace(/(<\/ul>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<blockquote>)/g, '$1');
    html = html.replace(/(<\/blockquote>)\s*<\/p>/g, '$1');
    return html;
}

// ============================================================
// Modal
// ============================================================

async function showModal(title, file) {
    try {
        var response = await fetch('/' + file);
        if (!response.ok) {
            throw new Error('HTTP ' + response.status);
        }
        var text = await response.text();
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').innerHTML = parseMarkdown(text);
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
// Share modal
// ============================================================

function openShareModal() {
    if (!state.fromStop || !state.toStop) return;

    // Build the shareable URL (absolute, with current mode)
    var url = new URL(window.location.origin + window.location.pathname);
    url.searchParams.set('from', state.fromStop.id);
    url.searchParams.set('to', state.toStop.id);
    url.searchParams.set('mode', state.routeMode);
    var shareUrl = url.toString();

    // Set the link input
    document.getElementById('share-link').value = shareUrl;

    // Set the OG image preview (dynamic)
    var ogImageUrl = '/api/og-image?from=' + encodeURIComponent(state.fromStop.id) +
        '&to=' + encodeURIComponent(state.toStop.id) +
        '&mode=' + encodeURIComponent(state.routeMode);
    document.getElementById('share-og-image').src = ogImageUrl;

    // Show the modal
    document.getElementById('share-overlay').style.display = 'block';
    document.getElementById('share-modal').style.display = 'flex';
}

function closeShareModal() {
    document.getElementById('share-overlay').style.display = 'none';
    document.getElementById('share-modal').style.display = 'none';
}

function copyShareLink() {
    var linkInput = document.getElementById('share-link');
    linkInput.select();
    linkInput.setSelectionRange(0, 99999);

    try {
        // Try the modern clipboard API first
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(linkInput.value).then(function() {
                showToast('Link skopiowany do schowka!');
            }).catch(function() {
                // Fallback to execCommand
                document.execCommand('copy');
                showToast('Link skopiowany do schowka!');
            });
        } else {
            // Fallback for older browsers
            document.execCommand('copy');
            showToast('Link skopiowany do schowka!');
        }
    } catch (error) {
        console.error('Copy error:', error);
        showToast('Nie udało się skopiować linku');
    }
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
        <div class="stop-popup-btn stop-popup-cancel" data-action="cancel">✕ Anuluj</div>
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

    customPopupEl.querySelector('[data-action="cancel"]').addEventListener('click', function(e) {
        e.stopPropagation();
        closeCustomPopup();
    });
    
    document.body.appendChild(customPopupEl);
    
    // Position popup near the click position
    // On mobile there's no sidebar offset; on desktop the sidebar is 340px wide
    const isMobile = window.innerWidth <= 768;
    let clientX, clientY;
    if (latlng.originalEvent) {
        clientX = latlng.originalEvent.clientX;
        clientY = latlng.originalEvent.clientY;
    } else if (latlng.containerPoint) {
        clientX = latlng.containerPoint.x + (isMobile ? 0 : 340);
        clientY = latlng.containerPoint.y;
    } else {
        clientX = window.innerWidth / 2;
        clientY = window.innerHeight / 2;
    }
    
    // Clamp popup within viewport (accounting for popup size)
    const popupWidth = 220;
    const popupHeight = 130;
    customPopupEl.style.left = Math.max(8, Math.min(clientX, window.innerWidth - popupWidth - 8)) + 'px';
    customPopupEl.style.top = Math.max(8, Math.min(clientY, window.innerHeight - popupHeight - 8)) + 'px';
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
        searchSelectedIndex[field] = -1;
        return;
    }

    clearTimeout(state.searchTimeout);
    state.searchTimeout = setTimeout(async function() {
        try {
            const response = await fetch(`/api/stops/search?q=${encodeURIComponent(query)}`);
            const results = await response.json();

            resultsEl.innerHTML = '';
            searchSelectedIndex[field] = -1;
            if (results.length === 0) {
                resultsEl.innerHTML = '<div class="search-item" style="color: #999; text-align: center; padding: 12px;">😕 Nie znaleziono przystanku<br><span style="font-size: 0.85em;">Spróbuj inną nazwę</span></div>';
            } else {
                results.forEach((group, idx) => {
                    const item = document.createElement('div');
                    item.className = 'search-item';
                    item.dataset.index = idx;
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
                        searchSelectedIndex[field] = -1;
                        updateSelectedStops();
                        if (state.fromStop && state.toStop) {
                            findRoute();
                        }
                    });
                    resultsEl.appendChild(item);
                });
            }
            resultsEl.classList.add('show');
            // Auto-highlight first result
            var firstItem = resultsEl.querySelector('.search-item[data-index]');
            if (firstItem) {
                firstItem.classList.add('search-item-active');
                searchSelectedIndex[field] = 0;
            }
        } catch (error) {
            console.error('Search error:', error);
        }
    }, 300);
}

function navigateSearchResults(input, resultsId, field, direction) {
    var resultsEl = document.getElementById(resultsId);
    if (!resultsEl || !resultsEl.classList.contains('show')) return;
    var items = resultsEl.querySelectorAll('.search-item[data-index]');
    if (items.length === 0) return;

    // Remove current highlight
    var curIdx = searchSelectedIndex[field];
    if (curIdx >= 0 && curIdx < items.length) {
        items[curIdx].classList.remove('search-item-active');
    }

    // Calculate new index
    if (direction === 'down') {
        searchSelectedIndex[field] = (searchSelectedIndex[field] + 1) % items.length;
    } else {
        searchSelectedIndex[field] = searchSelectedIndex[field] <= 0 ? items.length - 1 : searchSelectedIndex[field] - 1;
    }

    // Apply new highlight
    var newIdx = searchSelectedIndex[field];
    items[newIdx].classList.add('search-item-active');
    items[newIdx].scrollIntoView({ block: 'nearest' });
}

function selectHighlightedResult(input, resultsId, field) {
    var resultsEl = document.getElementById(resultsId);
    if (!resultsEl || !resultsEl.classList.contains('show')) return;
    var items = resultsEl.querySelectorAll('.search-item[data-index]');
    var idx = searchSelectedIndex[field];

    if (idx >= 0 && idx < items.length) {
        items[idx].click();
    } else if (items.length > 0) {
        // If nothing highlighted, select first item
        items[0].click();
    }
}

// ============================================================
// Stop Selection
// ============================================================

function selectStop(group, e) {
    // Show custom popup instead of auto-selecting
    showCustomPopup(group, e);
}

function collapseMobileSidebar() {
    // On mobile, collapse the sidebar so the map is visible after selecting a stop
    if (window.innerWidth <= 768) {
        const sidebar = document.getElementById('sidebar');
        if (sidebar && sidebar.classList.contains('expanded')) {
            sidebar.classList.remove('expanded');
            setTimeout(function() { state.map.invalidateSize(); }, 450);
        }
    }
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

    // On mobile, collapse the sidebar so the map is visible after selecting a stop
    collapseMobileSidebar();
}

