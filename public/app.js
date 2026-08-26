// MPK Kraków - Ticket Cost Calculator
// Main application logic

// ============================================================
// Safe DOM manipulation utilities (XSS prevention)
// ============================================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function createElementFromHtml(html) {
    // Parse as DOM and strip scripts and event handlers before insertion.
    // Returns a single wrapper div holding ALL top-level elements —
    // firstElementChild would silently drop everything after the first one.
    const template = document.createElement('template');
    template.innerHTML = html;
    const scripts = template.content.querySelectorAll('script');
    scripts.forEach(s => s.remove());
    const allElements = template.content.querySelectorAll('*');
    allElements.forEach(el => {
        for (const attr of el.attributes) {
            if (attr.name.startsWith('on')) {
                el.removeAttribute(attr.name);
            }
            if ((attr.name === 'href' || attr.name === 'src') && attr.value.startsWith('javascript:')) {
                el.removeAttribute(attr.name);
            }
        }
    });
    const wrapper = document.createElement('div');
    wrapper.appendChild(template.content);
    return wrapper;
}

// ============================================================
// Toast notification (lightweight, no dependencies)
// ============================================================

function showToast(message, duration, type) {
    duration = duration || 3000;
    const existing = document.querySelector('.toast-notification');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'toast-notification' + (type ? ' toast-' + type : '');
    toast.textContent = message;
    document.body.appendChild(toast);
    requestAnimationFrame(function() { toast.classList.add('show'); });
    setTimeout(function() {
        toast.classList.remove('show');
        setTimeout(function() { toast.remove(); }, 300);
    }, duration);
}

function showBanner(message) {
    if (document.querySelector('.server-banner')) return;
    var banner = document.createElement('div');
    banner.className = 'server-banner';
    banner.innerHTML = '<span>' + message + '</span><button class="banner-close" aria-label="Zamknij">&times;</button>';
    banner.querySelector('.banner-close').addEventListener('click', function() {
        banner.remove();
    });
    document.body.appendChild(banner);
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
    routeMode: 'cheap',
    hasRoute: false,
    routeCache: {}, // key: "fromId_toId", value: { short, convenient, cheap }
    currentRouteKey: null,
    shouldFitBounds: true,
};

// Single color for all stops
const STOP_COLOR = '#3498db';

// Keyboard navigation state for search results
var searchSelectedIndex = { from: -1, to: -1 };
var searchAbortController = null;

// ============================================================
// Initialization
// ============================================================

document.addEventListener('DOMContentLoaded', function() {
    initMap();
    setupEventListeners();
    // Non-blocking health check — show banner if server is overloaded
    fetch('/api/health').then(function(r) {
        if (!r.ok) showBanner('Serwer chwilowo niedostępny. Odśwież stronę za chwilę.');
    }).catch(function() {
        showBanner('Serwer chwilowo niedostępny. Odśwież stronę za chwilę.');
    });
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
            if (mode === 'convenient' || mode === 'cheap' || mode === 'short') {
                setRouteMode(mode);
            } else {
                setRouteMode('cheap');
            }
            updateSelectedStops();
            // Both stops and routes are already loaded (Promise.all above),
            // so the route can be computed right away.
            findRoute();
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

    // Show loading spinner while map tiles load
    var loadingEl = document.createElement('div');
    loadingEl.id = 'map-loading';
    loadingEl.style.cssText = 'position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 1000; text-align: center; background: rgba(255,255,255,0.9); padding: 20px 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.15);';
    loadingEl.innerHTML = '<div style="width: 24px; height: 24px; border: 3px solid #c0d8e8; border-top-color: #2874a6; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 8px;"></div><div style="font-size: 0.85em; color: #1a5276; font-weight: 500;">Ładowanie mapy</div>';
    document.getElementById('map').appendChild(loadingEl);

    state.map.whenReady(function() {
        setTimeout(function() {
            var el = document.getElementById('map-loading');
            if (el) el.remove();
        }, 500);
    });

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

    // App version badge (bottom-left, above data version). Public users see only
    // the application version — load/health stats are intentionally NOT shown
    // to the public. Full server stats open via right-click on "Autor".
    const statusEl = document.createElement('div');
    statusEl.className = 'leaflet-control leaflet-control-attribution server-status';
    statusEl.style.cssText = 'position: absolute; left: 0px; bottom: 18px; z-index: 1000; background: rgba(255,255,255,0.8); padding: 0 6px; font-size: 10.5px; color: #555; border-radius: 3px;';
    statusEl.textContent = '…';
    document.getElementById('map').appendChild(statusEl);
    // Hide on narrow screens — keep the map clean on mobile.
    if (window.innerWidth <= 768) {
        statusEl.classList.add('hidden');
    }

    // The app version never changes while the server runs — one fetch on
    // page load, no polling.
    fetch('/api/version')
        .then(r => r.json())
        .then(s => { if (s && s.version) statusEl.textContent = 'v' + s.version; })
        .catch(() => { /* badge stays as '…' */ });

    // Pricing panel: values live in pricing.json on the server; keep the
    // hardcoded HTML as fallback and overwrite when the API responds.
    const zl = n => n.toFixed(2).replace('.', ',') + ' zł';
    fetch('/api/pricing')
        .then(r => r.json())
        .then(p => {
            const set = (id, text) => {
                const el = document.getElementById(id);
                if (el) el.textContent = text;
            };
            set('price-base', `${zl(p.base_cost_regular)} / ${zl(p.base_cost_reduced)}`);
            set('price-segment', `+${zl(p.segment_cost_regular)} / +${zl(p.segment_cost_reduced)}`);
            set('price-max', `${zl(p.max_cost_regular)} / ${zl(p.max_cost_reduced)}`);
            set('price-daily', `${zl(p.max_daily_cost_regular)} / ${zl(p.max_daily_cost_reduced)}`);
        })
        .catch(() => { /* keep static fallback values */ });

}

// ------------------------------------------------------------
// Admin server-stats panel — opened via right-click on "Autor".
// Rendered in the map's top-right corner; hidden from public.
// Refreshes live every few seconds while open.
// ------------------------------------------------------------
const SERVER_PANEL_REFRESH_MS = 4000;
let serverPanelTimer = null;

function closeServerPanel() {
    const el = document.getElementById('server-stats-panel');
    if (el) el.remove();
    if (serverPanelTimer) {
        clearInterval(serverPanelTimer);
        serverPanelTimer = null;
    }
}

function toggleServerPanel() {
    if (document.getElementById('server-stats-panel')) { closeServerPanel(); return; }
    fetch('/api/status')
        .then(r => r.json())
        .then(s => { if (s) renderServerStatsPanel(s); })
        .catch(() => { /* keep closed on transient errors; right-click retries */ });
}

// Colour a value by thresholds: < ok green, < warm amber, else red.
function statColor(value, ok, warm) {
    if (typeof value !== 'number' || !isFinite(value) || value === null) return '#888';
    return value >= warm ? '#e74c3c' : value >= ok ? '#f39c12' : '#27ae60';
}

function fmtUptime(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h > 0) return `${h} h ${m} min`;
    return `${m} min`;
}

// Panel sections: declarative, so the panel can re-render values in place
// on live refresh without rebuilding the DOM (tooltips survive).
// Each row's get(state) derives text/colour from the FRESH /api/status.
function serverPanelSections() {
    return [
        {
            title: 'Serwer',
            rows: [
                { label: 'Wersja',
                  get: st => ({ text: 'v' + (st.version || '?') }),
                  tooltip: 'Wersja aplikacji.' },
                { label: 'Uptime',
                  get: st => ({ text: fmtUptime(st.uptime_seconds || 0) }),
                  tooltip: 'Jak długo serwer działa bez restartu.' },
                { label: 'Obciążenie',
                  get: st => {
                      const active = st.active_requests || 0;
                      const max = st.max_concurrent || 20;
                      const rss = st.rss_mb || 0;
                      return {
                          text: `${active}/${max} żądań, ${rss} MB RAM`,
                          color: statColor(active / max, 0.5, 0.8),
                      };
                  },
                  tooltip: 'Aktywne żądania HTTP względem limitu współbieżności '
                      + 'oraz aktualne zużycie pamięci serwera.' },
            ],
        },
        {
            title: 'Trasy',
            rows: [
                { label: 'Zapytania',
                  get: st => ({
                      text: `${st.route_requests || 0}`,
                  }),
                  tooltip: 'Ile razy ktoś szukał trasy od startu serwera.' },
                { label: 'Timeouty',
                  get: st => {
                      const req = st.route_requests || 0;
                      const timeouts = st.route_timeouts || 0;
                      if (req === 0) return { text: '—', color: '#888' };
                      const pct = timeouts / req;
                      return {
                          text: `${timeouts} / ${req} (${(pct * 100).toFixed(0)}%)`,
                          color: pct > 0 ? '#e74c3c' : '#27ae60',
                      };
                  },
                  tooltip: 'Zapytania które przekroczyły limit czasu serwera (25s). '
                      + 'Wysoki odsetek = serwer nie wyrabia.' },
                { label: 'Obliczone',
                  get: st => ({
                      text: `${st.routes_computed || 0}`,
                      color: st.routes_computed > 0 ? '#f39c12' : '',
                  }),
                  tooltip: 'Trasy wyliczone od zera (nie z cache). '
                      + 'Duża liczba = cache nie wystarcza.' },
                { label: 'Cache',
                  get: st => {
                      const rc = st.find_cache || {};
                      const entries = rc.route_entries || 0;
                      const bytes = rc.route_bytes || 0;
                      const maxBytes = rc.route_max_bytes || 1;
                      return {
                          text: `${entries} wpisów (${(bytes / 1048576).toFixed(1)} MB)`,
                          color: statColor(bytes / maxBytes, 0.6, 0.9),
                      };
                  },
                  tooltip: 'Gotowe trasy w pamięci. Powtórne zapytanie '
                      + 'o tę samą parę przystanków zwraca wynik natychmiast.' },
            ],
        },
        {
            title: 'Wyszukiwania',
            rows: [
                { label: 'Cache A*',
                  get: st => {
                      const f = st.find_cache || {};
                      const entries = f.find_entries || 0;
                      const bytes = f.find_bytes || 0;
                      const maxBytes = f.find_max_bytes || 1;
                      return {
                          text: `${entries} wpisów (${(bytes / 1048576).toFixed(1)} MB)`,
                          color: statColor(bytes / maxBytes, 0.6, 0.9),
                      };
                  },
                  tooltip: 'Wyniki wyszukiwań między peronami. '
                      + 'Przekroczenie budżetu usuwa najstarsze wpisy.' },
                { label: 'Timeouty',
                  get: st => {
                      const cheap = st.cheap || {};
                      const found = cheap.searches || 0;
                      const timeouts = cheap.timeouts || 0;
                      if (found === 0) return { text: '—', color: '#888' };
                      const pct = timeouts / found;
                      return {
                          text: `${timeouts} / ${found} (${(pct * 100).toFixed(0)}%)`,
                          color: statColor(pct, 0.25, 0.6),
                      };
                  },
                  tooltip: 'Wyszukiwania najtańszej trasy które przekroczyły limit czasu. '
                      + 'Wysoki odsetek = serwer nie wyrabia.' },
            ],
        },
    ];
}

function renderServerStatsPanel(s) {
    const mapEl = document.getElementById('map');
    const panel = document.createElement('div');
    panel.id = 'server-stats-panel';
    panel.className = 'server-stats-panel';

    // Header: title + close button
    const header = document.createElement('div');
    header.className = 'ssp-header';
    const title = document.createElement('b');
    title.textContent = 'Statystyki serwera';
    title.title = 'Dane odświeżają się automatycznie co '
        + (SERVER_PANEL_REFRESH_MS / 1000) + ' s.';
    const close = document.createElement('button');
    close.textContent = '✕';
    close.title = 'Zamknij panel';
    close.className = 'ssp-close';
    close.addEventListener('click', closeServerPanel);
    header.appendChild(title);
    header.appendChild(close);
    panel.appendChild(header);

    // Sections with rows: label left, value right; hover shows tooltip.
    function row(def) {
        const r = document.createElement('div');
        if (def.tooltip) {
            r.title = def.tooltip;
            r.style.cursor = 'help';
        }
        r.className = 'ssp-row';
        const l = document.createElement('span');
        l.textContent = def.label;
        l.className = 'ssp-label';
        const v = document.createElement('span');
        v.className = 'ssp-value';
        r.appendChild(l); r.appendChild(v);
        return [r, v];
    }

    const rows = [];
    for (const section of serverPanelSections()) {
        const h = document.createElement('div');
        h.className = 'ssp-section';
        h.textContent = section.title;
        panel.appendChild(h);

        for (const def of section.rows) {
            const [el, span] = row(def);
            panel.appendChild(el);
            rows.push({ span, def });
        }
    }

    // In-place update — no DOM rebuild, so tooltips and hover state survive.
    panel._update = function(s2) {
        for (const { span, def } of rows) {
            const val = def.get(s2);
            span.textContent = val.text;
            span.style.color = val.color || '';
        }
    };
    panel._update(s);

    // Small legend for the colour levels.
    const legend = document.createElement('div');
    legend.className = 'ssp-legend';
    legend.innerHTML = '<span style="color:#27ae60">●</span> ok &nbsp;<span style="color:#f39c12">●</span> busy &nbsp;<span style="color:#e74c3c">●</span> high';
    panel.appendChild(legend);

    mapEl.appendChild(panel);

    // Live refresh while the panel is open
    serverPanelTimer = setInterval(function() {
        if (!document.getElementById('server-stats-panel')) {
            closeServerPanel(); // safety net — stop ticking after manual removal
            return;
        }
        fetch('/api/status')
            .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(s2 => {
                const p = document.getElementById('server-stats-panel');
                if (p && p._update) p._update(s2);
            })
            .catch(() => { /* keep last values on transient errors */ });
    }, SERVER_PANEL_REFRESH_MS);
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
        // Close admin stats panel when clicking away from it.
        const sp = document.getElementById('server-stats-panel');
        if (sp && e.target && !e.target.closest('#server-stats-panel')) closeServerPanel();
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

    // Right-click on "Autor" opens the admin-only server-stats panel
    // (top-right of the map). Left-click keeps the normal modal.
    document.getElementById('btn-author').addEventListener('contextmenu', function(e) {
        e.preventDefault();
        toggleServerPanel();
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
        document.getElementById('mobile-result').classList.add('hidden');
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

    // Escape key closes modals
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeModal();
            closeShareModal();
            closeCustomPopup();
            closeServerPanel();
        }
    });

    // Mobile mode buttons
    document.getElementById('mobile-mode-cheap').addEventListener('click', function() {
        setRouteMode('cheap');
    });
    document.getElementById('mobile-mode-convenient').addEventListener('click', function() {
        setRouteMode('convenient');
    });

}

function clearRoute() {
    state.fromStop = null;
    state.toStop = null;
    state.hasRoute = false;
    state.routeGroupIds = null;
    document.getElementById('from-search').value = '';
    document.getElementById('to-search').value = '';
    document.getElementById('right-panel').classList.add('hidden');
    document.getElementById('mobile-result').classList.add('hidden');
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
        const modalBody = document.getElementById('modal-body');
        modalBody.innerHTML = '';
        modalBody.appendChild(createElementFromHtml(parseMarkdown(text)));
        document.getElementById('modal-overlay').style.display = 'block';
        document.getElementById('modal-overlay').classList.remove('hidden');
        document.getElementById('modal').style.display = 'flex';
        document.getElementById('modal').classList.remove('hidden');
    } catch (error) {
        console.error('Error loading modal content:', error);
    }
}

function closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
    document.getElementById('modal').classList.add('hidden');
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

    // Set the OG image preview (dynamic). Generation can take a few seconds
    // (server-side A* + SVG), so show a spinner until the image arrives.
    var ogImageUrl = '/api/og-image?from=' + encodeURIComponent(state.fromStop.id) +
        '&to=' + encodeURIComponent(state.toStop.id) +
        '&mode=' + encodeURIComponent(state.routeMode);
    var ogImage = document.getElementById('share-og-image');
    var ogLoading = document.getElementById('share-og-loading');
    var ogRetryCount = 0;
    ogImage.classList.add('hidden');
    ogLoading.style.display = 'flex';
    ogLoading.classList.remove('hidden');
    ogImage.onload = function() {
        ogLoading.classList.add('hidden');
        ogImage.style.display = 'block';
        ogImage.classList.remove('hidden');
    };
    ogImage.onerror = function() {
        if (ogRetryCount < 2) {
            ogRetryCount++;
            setTimeout(function() { ogImage.src = ogImageUrl; }, 2000);
        } else {
            ogLoading.innerHTML = '<span class="loading-text">Podgląd niedostępny. Udostępnij link bezpośrednio.</span>';
        }
    };
    ogImage.src = ogImageUrl;

    // Show the modal
    document.getElementById('share-overlay').style.display = 'block';
    document.getElementById('share-overlay').classList.remove('hidden');
    document.getElementById('share-modal').style.display = 'flex';
    document.getElementById('share-modal').classList.remove('hidden');
}

function closeShareModal() {
    document.getElementById('share-overlay').classList.add('hidden');
    document.getElementById('share-modal').classList.add('hidden');
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
    let stopGroups = null;

    // /api/stops is cheap; transient failures (429 burst, proxy hiccup) must
    // not leave the map blank — retry briefly before giving up.
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            const response = await fetch('/api/stops');
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            const data = await response.json();
            if (Array.isArray(data)) {
                stopGroups = data;
                break;
            }
            throw new Error('Nieoczekiwana odpowiedź serwera');
        } catch (error) {
            console.warn(`loadStops: próba ${attempt}/3 nieudana`, error);
            if (attempt < 3) {
                await new Promise(r => setTimeout(r, 500 * attempt));
            }
        }
    }

    if (!stopGroups) {
        const notice = document.createElement('div');
        notice.className = 'map-notice';
        notice.style.cssText = 'position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 1000; background: rgba(255,255,255,0.95); border: 1px solid #e74c3c; color: #c0392b; padding: 14px 20px; border-radius: 8px; font-size: 0.9em; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.15); max-width: 80%;';
        notice.textContent = 'Nie udało się załadować przystanków. Odśwież stronę, aby spróbować ponownie.';
        document.getElementById('map').appendChild(notice);
        console.error('loadStops: failed after 3 attempts');
        return;
    }

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

            hitArea._selectHandler = function(e) {
                selectStop(group, e);
            };
            hitArea.on('click', hitArea._selectHandler);
            hitArea._routeModeInteractive = true;

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
}

function getModeLabel(mode) {
    switch (mode) {
        case 'tram': return '🚊 Tramwaj';
        case 'bus': return '🚌 Autobus';
        case 'mobilis': return '🚍 Mobilis';
        default: return mode;
    }
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
        // Abort the previous in-flight search so a slow response for an
        // outdated query can never overwrite results of the current one.
        if (searchAbortController) {
            searchAbortController.abort();
        }
        searchAbortController = new AbortController();
        try {
            const response = await fetch(`/api/stops/search?q=${encodeURIComponent(query)}`,
                { signal: searchAbortController.signal });
            const results = await response.json();

            resultsEl.innerHTML = '';
            searchSelectedIndex[field] = -1;
            if (results.length === 0) {
                const emptyMsg = document.createElement('div');
                emptyMsg.className = 'search-item';
                emptyMsg.style.cssText = 'color: #999; text-align: center; padding: 12px;';
                emptyMsg.innerHTML = '😕 Nie znaleziono przystanku<br><span style="font-size: 0.85em;">Spróbuj inną nazwę</span>';
                resultsEl.appendChild(emptyMsg);
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
                            <span class="mode" style="background: ${STOP_COLOR}; color: white;">${escapeHtml(group.modes.join('/').toUpperCase())}</span>
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
            if (error && error.name === 'AbortError') return; // superseded by a newer query
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
    // "Wyczyść trasę" ma sens tylko gdy trasa jest wytyczona
    document.getElementById('clear-btn').classList.toggle('hidden', !state.hasRoute);

    // Update marker colors and visibility
    const routeShown = !!state.hasRoute;
    state.markers.forEach(marker => {
        const group = marker.stopData;
        let color = STOP_COLOR;
        let radius = 5;
        let fillOpacity = 1;
        let strokeOpacity = 1;

        // Route shown:
        //  - stops ON the route: averaged dot disappears COMPLETELY (fill AND
        //    stroke) — the real peron marker on the route layer takes over.
        //  - stops OFF the route: dimmed but visible (fill 0.15, as before).
        let onRoute = false;
        if (routeShown) {
            onRoute = !!(state.routeGroupIds && state.routeGroupIds.has(group.id));
            if (onRoute) {
                fillOpacity = 0;
                strokeOpacity = 0;
            } else {
                fillOpacity = 0.15;
            }
        }

        // Route mode: only perons on the route are interactive (via the route
        // layer markers). Overview hit areas stay active for off-route stops.
        // Also kill pointer-events on on-route hit areas — otherwise the
        // invisible circle still catches hover and shows the pointer cursor.
        if (marker._selectHandler) {
            const wantsClick = !routeShown || !onRoute;
            if (wantsClick !== marker._routeModeInteractive) {
                marker._routeModeInteractive = wantsClick;
                if (wantsClick) {
                    marker.on('click', marker._selectHandler);
                } else {
                    marker.off('click', marker._selectHandler);
                }
            }
        }
        const hitEl = marker.getElement ? marker.getElement() : null;
        if (hitEl) {
            const interactive = !routeShown || !onRoute;
            if (interactive) {
                hitEl.style.pointerEvents = '';
            } else {
                hitEl.style.pointerEvents = 'none';
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
                fillOpacity: fillOpacity,
                opacity: strokeOpacity,
            });
        }
    });

    // On mobile, collapse the sidebar so the map is visible after selecting a stop
    collapseMobileSidebar();
}

