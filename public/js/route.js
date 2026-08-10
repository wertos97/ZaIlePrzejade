// Route finding and display logic

// Route colors for route lines
const ROUTE_COLORS = [
    '#e74c3c', '#3498db', '#9b59b6', '#f39c12', '#1abc9c',
    '#e67e22', '#9b59b6', '#34495e', '#16a085', '#c0392b',
];

// Format a duration in seconds as "X min" or "1 h 5 min"
function formatDuration(seconds) {
    if (!seconds || seconds <= 0 || !isFinite(seconds)) {
        return null;
    }
    const totalMinutes = Math.round(seconds / 60);
    if (totalMinutes < 60) {
        return `${totalMinutes} min`;
    }
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (minutes === 0) {
        return `${hours} h`;
    }
    return `${hours} h ${minutes} min`;
}

async function findRoute() {
    if (!state.fromStop || !state.toStop) {
        return;
    }

    const routeKey = `${state.fromStop.id}_${state.toStop.id}`;
    state.currentRouteKey = routeKey;

    // Check if we have cached results for this route
    if (state.routeCache[routeKey]) {
        const cached = state.routeCache[routeKey];
        const result = cached[state.routeMode];
        if (result) {
            updateEqualityIndicators(cached.short, cached.convenient);
            displayRoute(result);
            updateURL();
            return;
        }
    }

    // Show loading indicator
    const isMobile = window.innerWidth <= 768;
    const loadingEl = document.getElementById('loading-indicator');
    const loadingOverlay = document.getElementById('loading-overlay');

    // On mobile, only show full-screen overlay; on desktop show inline spinner
    if (isMobile && loadingOverlay) {
        loadingOverlay.classList.add('show');
    } else {
        loadingEl.style.display = 'flex';
    }

    try {
        // Fetch only the currently selected mode (reduces server load by ~50%)
        // The other mode is fetched lazily when the user switches modes.
        const url = `/api/find-route?from=${state.fromStop.id}&to=${state.toStop.id}&mode=${state.routeMode}`;
        console.log('[DEBUG] findRoute: wysyłam żądanie', url);
        const response = await fetch(url);
        console.log('[DEBUG] findRoute: status odpowiedzi =', response.status);
        const result = await response.json();
        console.log('[DEBUG] findRoute: odpowiedź =', result);

        if (result.error) {
            console.warn('[DEBUG] findRoute: serwer zwrócił błąd:', result.error);
            loadingEl.style.display = 'none';
            if (isMobile && loadingOverlay) loadingOverlay.classList.remove('show');
            showToast(result.error + ' Kliknij trasę ponownie, aby spróbować.', 5000);
            return;
        }

        // Cache the fetched mode
        if (!state.routeCache[routeKey]) {
            state.routeCache[routeKey] = {};
        }
        state.routeCache[routeKey][state.routeMode] = result;

        // This is a new route - fit bounds on next draw
        state.shouldFitBounds = true;

        // Update equality indicator only if we have both modes cached
        const cached = state.routeCache[routeKey];
        if (cached.short && cached.convenient) {
            updateEqualityIndicators(cached.short, cached.convenient);
        } else {
            // Hide equality indicator until we have both modes
            document.getElementById('mode-equal').style.display = 'none';
            document.getElementById('mobile-mode-equal').style.display = 'none';
        }

        displayRoute(result);
        updateURL();
    } catch (error) {
        console.error('Route finding error:', error);
        showToast('Wystąpił błąd podczas wyszukiwania trasy');
    } finally {
        loadingEl.style.display = 'none';
        if (isMobile && loadingOverlay) loadingOverlay.classList.remove('show');
    }
}

function updateEqualityIndicators(shortResult, convenientResult) {
    // If either mode is missing, hide the equality indicator
    if (!shortResult || !convenientResult || shortResult.error || convenientResult.error) {
        document.getElementById('mode-equal').style.display = 'none';
        document.getElementById('mobile-mode-equal').style.display = 'none';
        return;
    }

    const equal = (
        Math.abs(shortResult.total_distance - convenientResult.total_distance) < 0.001 &&
        (shortResult.transfers || []).length === (convenientResult.transfers || []).length
    );

    const equalEl = document.getElementById('mode-equal');
    if (equal) {
        equalEl.style.display = 'flex';
        equalEl.style.animation = 'none';
        equalEl.offsetHeight;
        equalEl.style.animation = '';
    } else {
        equalEl.style.display = 'none';
    }

    const mobileEqualEl = document.getElementById('mobile-mode-equal');
    if (equal) {
        mobileEqualEl.style.display = 'flex';
        mobileEqualEl.style.animation = 'none';
        mobileEqualEl.offsetHeight;
        mobileEqualEl.style.animation = '';
    } else {
        mobileEqualEl.style.display = 'none';
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

    // Show right panel (desktop) or mobile result
    const isMobile = window.innerWidth <= 768;
    if (isMobile) {
        document.getElementById('right-panel').style.display = 'none';
        document.querySelector('.main-content').classList.remove('with-result');
        document.getElementById('mobile-result').style.display = 'block';
    } else {
        document.getElementById('right-panel').style.display = 'block';
        document.querySelector('.main-content').classList.add('with-result');
        document.getElementById('mobile-result').style.display = 'none';
    }

    // Update marker visibility
    updateSelectedStops();

    // Fit bounds AFTER the panel is shown, so padding is correct
    if (state.shouldFitBounds && result.path && result.path.length > 0) {
        state.shouldFitBounds = false;
        const bounds = L.latLngBounds(result.path.map(s => [s.lat, s.lon]));
        // Delay fitBounds to allow DOM to update (panel rendering)
        setTimeout(function() {
            state.map.invalidateSize();
            if (isMobile) {
                const panelEl = document.getElementById('mobile-result');
                const panelHeight = panelEl.offsetHeight || Math.round(window.innerHeight * 0.5);
                state.map.fitBounds(bounds, {
                    paddingTopLeft: [20, 72],
                    paddingBottomRight: [20, panelHeight + 20],
                });
            } else {
                state.map.fitBounds(bounds, {
                    paddingTopLeft: [20, 20],
                    paddingBottomRight: [20, 20],
                });
            }
        }, 150);
    }
}

function drawRouteOnMap(result) {
    const path = result.path;
    if (!path || path.length === 0) return;

    const drawPoints = path.filter(s => !s.is_transfer);

    // Build a lookup: stop_id -> {lat, lon} for all non-transfer stops
    const coordLookup = {};
    drawPoints.forEach(s => {
        // route.js path items use 'stop_id'
        if (s.stop_id) coordLookup[s.stop_id] = [s.lat, s.lon];
    });

    // Draw real-world geometry for each segment (ride) using its shape.
    // Fall back to straight lines between the segment's stops if no shape is available.
    let colorIndex = 0;

    if (result.segments && result.segments.length > 0) {
        result.segments.forEach(seg => {
            let points = [];

            if (seg.shape && Array.isArray(seg.shape) && seg.shape.length >= 2) {
                // Real geometry: shape is a list of [lat, lon] pairs
                points = seg.shape.map(pt => [pt[0], pt[1]]);
            } else if (seg.stops && seg.stops.length >= 2) {
                // Fallback: straight line through the stops that have coordinates
                seg.stops.forEach(sid => {
                    const coord = coordLookup[sid] || (result.path || []).find(p => p.stop_id === sid);
                    if (coord) points.push(coord);
                });
            }

            if (points.length >= 2) {
                const color = ROUTE_COLORS[colorIndex % ROUTE_COLORS.length];
                L.polyline(points, {
                    color: color,
                    weight: 5,
                    opacity: 0.75,
                    smoothFactor: 1,
                }).addTo(state.routeLayer);
                colorIndex++;
            }
        });
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

    // Draw price/distance labels at the END of each segment (ride)
    if (result.segments && result.segments.length > 0) {
        result.segments.forEach(seg => {
            if (!seg.stops || seg.stops.length < 2) return;
            const lastId = seg.stops[seg.stops.length - 1];
            const lastCoord = coordLookup[lastId];
            if (!lastCoord) return;

            const distKm = seg.distance ? seg.distance.toFixed(1) : '0.0';
            const costText = seg.cost_regular !== undefined ? `${seg.cost_regular.toFixed(2)} zł` : '? zł';

            const icon = L.divIcon({
                className: 'segment-label',
                html: `<div class="segment-label-box">
                    <span class="segment-label-cost">${costText}</span>
                    <span class="segment-label-dist">${distKm} km</span>
                </div>`,
                // Auto-size to content; CSS uses width:max-content
                iconSize: [0, 0],
                iconAnchor: [0, 0],
            });

            L.marker([lastCoord[0], lastCoord[1]], { icon: icon, interactive: false }).addTo(state.routeLayer);
        });
    }
}

function displayResult(result) {
    document.getElementById('result-distance').textContent =
        `${result.total_distance.toFixed(2)} km`;
    const totalTimeText = formatDuration(result.total_time);
    const timeEl = document.getElementById('result-time');
    if (timeEl) {
        timeEl.textContent = totalTimeText ? `· ${totalTimeText}` : '';
    }
    document.getElementById('result-regular').textContent =
        `${result.cost_regular.toFixed(2)} zł`;
    document.getElementById('result-reduced').textContent =
        `${result.cost_reduced.toFixed(2)} zł`;

    document.getElementById('mobile-result-distance').textContent =
        `${result.total_distance.toFixed(2)} km`;
    const mobileTimeEl = document.getElementById('mobile-result-time');
    if (mobileTimeEl) {
        mobileTimeEl.textContent = totalTimeText ? `· ${totalTimeText}` : '';
    }
    document.getElementById('mobile-result-regular').textContent =
        `${result.cost_regular.toFixed(2)} zł`;
    document.getElementById('mobile-result-reduced').textContent =
        `${result.cost_reduced.toFixed(2)} zł`;

    const transferCount = result.transfers ? result.transfers.length : 0;
    const transferText = transferCount > 0 ? `${transferCount} przesiadka(e/k)` : 'Bez przesiadek';
    document.getElementById('result-transfers').textContent = transferText;
    document.getElementById('mobile-result-transfers').textContent = transferText;

    const stepsHtml = buildStepsHtml(result);
    document.getElementById('route-steps').innerHTML = stepsHtml;
    document.getElementById('mobile-route-steps').innerHTML = stepsHtml;
}

function buildStepsHtml(result) {
    const stopNameLookup = {};
    if (result.path) {
        result.path.forEach(s => {
            stopNameLookup[s.stop_id] = s.name;
        });
    }

    let html = '';

    if (result.segments && result.segments.length > 0) {
        result.segments.forEach((segment, index) => {
            const route = getRouteInfo(segment.route_id);
            const firstStopName = segment.first_stop_name || stopNameLookup[segment.stops[0]] || segment.stops[0];
            const lastStopName = segment.last_stop_name || stopNameLookup[segment.stops[segment.stops.length - 1]] || segment.stops[segment.stops.length - 1];
            const modeIcon = segment.mode === 'tram' ? '🚊' : segment.mode === 'bus' ? '🚌' : '🚍';
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

            const segCost = segment.cost_regular !== undefined ? segment.cost_regular.toFixed(2) : '?';
            const segTime = formatDuration(segment.time);
            const segTimeText = segTime ? ` · ${segTime}` : '';
            html += `
                <div class="route-step">
                    <div class="step-header">
                        <span class="step-title">${modeIcon} Przejazd</span>
                        <span class="step-distance">${segment.distance.toFixed(2)} km${segTimeText} · ${segCost} zł</span>
                    </div>
                    <div class="step-routes">Linie: ${escapeHtml(routeLabel)}</div>
                    <div class="step-detail">
                        ${escapeHtml(firstStopName)} → ${escapeHtml(lastStopName)}
                    </div>
                    <div class="step-detail" style="margin-top: 2px; color: #999;">
                        ${segment.stops.length} przystanków
                    </div>
                </div>
            `;
            
            if (result.transfers && index < result.transfers.length) {
                const transfer = result.transfers[index];
                const transferStopName = transfer.stop_name || stopNameLookup[transfer.stop_id] || transfer.stop_id;
                const fromRoute = getRouteInfo(transfer.from_route);
                const toRoute = getRouteInfo(transfer.to_route);
                html += `
                    <div class="route-step transfer">
                        <div class="step-header">
                            <span class="step-title">🔄 Przesiadka</span>
                        </div>
                        <div class="step-detail" style="font-weight: 500; margin-bottom: 2px;">
                            <strong>${escapeHtml(transferStopName)}</strong>
                        </div>
                        <div class="step-detail" style="color: #888;">
                            ${fromRoute ? `linia ${fromRoute.short_name}` : '?'} → ${toRoute ? `linia ${toRoute.short_name}` : '?'}
                        </div>
                    </div>
                `;
            }
        });
    }

    // Share button at the end of the route steps (mobile)
    html += `
        <button class="btn btn-share-mobile" id="btn-share-mobile" aria-label="Udostępnij trasę">🔗 Udostępnij</button>
    `;

    return html;
}

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