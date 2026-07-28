// Route finding and display logic

// Route colors for route lines
const ROUTE_COLORS = [
    '#e74c3c', '#3498db', '#9b59b6', '#f39c12', '#1abc9c',
    '#e67e22', '#9b59b6', '#34495e', '#16a085', '#c0392b',
];

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
            return;
        }
    }

    // Show loading indicator
    const loadingEl = document.getElementById('loading-indicator');
    loadingEl.style.display = 'flex';

    // Show full-screen loading overlay on mobile
    const isMobile = window.innerWidth <= 768;
    const loadingOverlay = document.getElementById('loading-overlay');
    if (isMobile && loadingOverlay) {
        loadingOverlay.classList.add('show');
    }

    try {
        // Fetch both modes simultaneously
        const [shortResponse, convenientResponse] = await Promise.all([
            fetch(`/api/find-route?from=${state.fromStop.id}&to=${state.toStop.id}&mode=short`),
            fetch(`/api/find-route?from=${state.fromStop.id}&to=${state.toStop.id}&mode=convenient`),
        ]);

        const shortResult = await shortResponse.json();
        const convenientResult = await convenientResponse.json();

        if (shortResult.error) {
            loadingEl.style.display = 'none';
            if (isMobile && loadingOverlay) loadingOverlay.classList.remove('show');
            showToast(shortResult.error);
            return;
        }

        // Cache both results
        state.routeCache[routeKey] = {
            short: shortResult,
            convenient: convenientResult,
        };

        const result = state.routeMode === 'short' ? shortResult : convenientResult;

        // This is a new route - fit bounds on next draw
        state.shouldFitBounds = true;

        updateEqualityIndicators(shortResult, convenientResult);
        displayRoute(result);
    } catch (error) {
        console.error('Route finding error:', error);
        showToast('Wystąpił błąd podczas wyszukiwania trasy');
    } finally {
        loadingEl.style.display = 'none';
        if (isMobile && loadingOverlay) loadingOverlay.classList.remove('show');
    }
}

function updateEqualityIndicators(shortResult, convenientResult) {
    const equal = (
        !convenientResult.error &&
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
        if (isMobile) {
            const panelEl = document.getElementById('mobile-result');
            const panelHeight = panelEl.offsetHeight || (window.innerHeight * 0.50);
            state.map.fitBounds(bounds, {
                paddingTopLeft: [0, 72],
                paddingBottomRight: [0, panelHeight],
                maxZoom: 15,
            });
        } else {
            state.map.fitBounds(bounds, {
                paddingTopLeft: [0, 52],
                paddingBottomRight: [340, 20],
                maxZoom: 16,
            });
        }
    }
}

function drawRouteOnMap(result) {
    const path = result.path;
    if (!path || path.length === 0) return;

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
}

function displayResult(result) {
    document.getElementById('result-distance').textContent =
        `${result.total_distance.toFixed(2)} km`;
    document.getElementById('result-regular').textContent =
        `${result.cost_regular.toFixed(2)} zł`;
    document.getElementById('result-reduced').textContent =
        `${result.cost_reduced.toFixed(2)} zł`;

    document.getElementById('mobile-result-distance').textContent =
        `${result.total_distance.toFixed(2)} km`;
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

            html += `
                <div class="route-step">
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