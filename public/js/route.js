// Route finding and display logic

// Route colors for route lines
const ROUTE_COLORS = [
    '#e74c3c', '#3498db', '#9b59b6', '#f39c12', '#1abc9c',
    '#e67e22', '#9b59b6', '#34495e', '#16a085', '#c0392b',
];

// escapeHtml / createElementFromHtml are shared with app.js (loaded first).

function delay(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}

async function fetchWithRetry(url, maxRetries, updateText) {
    // Retries on 429/503 (rate limit / peak shedding). The delays give the
    // search queue time to drain — a retry usually lands on a freshly
    // cached result instead of re-joining the queue.
    var delays = [0, 4000, 10000];
    for (var attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            var response = await fetch(url);
            if ((response.status === 429 || response.status === 503) && attempt < maxRetries) {
                if (updateText) updateText('Ponawiam wyszukiwanie... (' + attempt + '/' + maxRetries + ')');
                await delay(delays[attempt]);
                continue;
            }
            return response;
        } catch (error) {
            if (attempt < maxRetries) {
                if (updateText) updateText('Ponawiam wyszukiwanie... (' + attempt + '/' + maxRetries + ')');
                await delay(delays[attempt]);
                continue;
            }
            throw error;
        }
    }
}

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
            updateEqualityIndicators(cached.cheap, cached.convenient);
            displayRoute(result);
            updateURL();
            return;
        }
    }

    // Show loading indicator
    const isMobile = window.innerWidth <= 768;
    const loadingEl = document.getElementById('loading-indicator');
    const loadingOverlay = document.getElementById('loading-overlay');
    const loadingText = loadingEl ? loadingEl.querySelector('.loading-text') : null;

    // On mobile, only show full-screen overlay; on desktop show inline spinner
    if (isMobile && loadingOverlay) {
        loadingOverlay.classList.add('show');
    } else {
        loadingEl.classList.remove('hidden');
    }

    // Update loading text after 5 seconds for long searches
    var longSearchTimer = setTimeout(function() {
        if (loadingText) loadingText.textContent = 'Szukam trasy... to może potrwać do 30 sekund przy długich trasach';
    }, 5000);

    function updateLoadingText(text) {
        if (loadingText) loadingText.textContent = text;
    }

    try {
        const fromId = state.fromStop.id;
        const toId = state.toStop.id;

        const url = `/api/find-route?from=${fromId}&to=${toId}`;
        const response = await fetchWithRetry(url, 3, updateLoadingText);
        const result = await response.json();

        if (result.error) {
            if (result.error.includes('przeciążony') || result.error.includes('pamięci')) {
                showToast(result.error, 6000, 'warning');
            } else {
                showToast(result.error + ' Kliknij trasę ponownie, aby spróbować.', 5000);
            }
            return;
        }

        // Cache both modes from the response
        state.routeCache[routeKey] = {
            convenient: result.convenient,
            cheap: result.cheap,
        };

        // This is a new route - fit bounds on next draw
        state.shouldFitBounds = true;

        // Update equality indicator
        updateEqualityIndicators(result.cheap, result.convenient);

        // Exact-only product, no fallbacks: if the requested mode's search
        // exceeded its time budget, tell the user and stop — the other mode
        // (if any) is one click away, but we never silently swap it in.
        const route = result[state.routeMode];
        if (!route) {
            showToast('Przekroczono czas wyszukiwania trasy (30 s). '
                + 'Spróbuj ponownie za chwilę.', 6000, 'warning');
            return;
        }

        displayRoute(route);
        updateURL();
    } catch (error) {
        console.error('Route finding error:', error);
        showToast('Serwer chwilowo niedostępny. Spróbuj ponownie za chwilę.', 5000, 'error');
    } finally {
        clearTimeout(longSearchTimer);
        loadingEl.classList.add('hidden');
        if (isMobile && loadingOverlay) loadingOverlay.classList.remove('show');
        if (loadingText) loadingText.textContent = 'Szukam trasy...';
    }
}

function updateEqualityIndicators(cheapResult, convenientResult) {
    // If either mode is missing, hide the equality indicator
    if (!cheapResult || !convenientResult || cheapResult.error || convenientResult.error) {
        document.getElementById('mode-equal').classList.add('hidden');
        document.getElementById('mobile-mode-equal').classList.add('hidden');
        return;
    }

    const equal = (
        Math.abs(cheapResult.total_distance - convenientResult.total_distance) < 0.001 &&
        (cheapResult.transfers || []).length === (convenientResult.transfers || []).length
    );

    const equalEl = document.getElementById('mode-equal');
    if (equal) {
        equalEl.style.display = 'flex';
        equalEl.classList.remove('hidden');
        equalEl.style.animation = 'none';
        equalEl.offsetHeight;
        equalEl.style.animation = '';
    } else {
        equalEl.classList.add('hidden');
    }

    const mobileEqualEl = document.getElementById('mobile-mode-equal');
    if (equal) {
        mobileEqualEl.style.display = 'flex';
        mobileEqualEl.classList.remove('hidden');
        mobileEqualEl.style.animation = 'none';
        mobileEqualEl.offsetHeight;
        mobileEqualEl.style.animation = '';
    } else {
        mobileEqualEl.classList.add('hidden');
    }
}

function displayRoute(result) {
    state.hasRoute = true;
    state.routeLayer.clearLayers();

    // Collect stop group IDs on the route for hiding other stops
    state.routeGroupIds = new Set();
    if (result.path) {
        result.path.forEach(s => {
            if (s.group_id) state.routeGroupIds.add(s.group_id);
        });
    }

    drawRouteOnMap(result);
    displayResult(result);

    // Show right panel (desktop) or mobile result
    const isMobile = window.innerWidth <= 768;
    if (isMobile) {
        document.getElementById('right-panel').classList.add('hidden');
        document.querySelector('.main-content').classList.remove('with-result');
        document.getElementById('mobile-result').style.display = 'block';
        document.getElementById('mobile-result').classList.remove('hidden');
    } else {
        document.getElementById('right-panel').style.display = 'block';
        document.getElementById('right-panel').classList.remove('hidden');
        document.querySelector('.main-content').classList.add('with-result');
        document.getElementById('mobile-result').classList.add('hidden');
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

    // Build a lookup: stop_id -> {lat, lon} for all non-transfer stops.
    // The path list is deduplicated per stop group, so also merge the exact
    // peron positions carried by each segment.
    const coordLookup = {};
    drawPoints.forEach(s => {
        // route.js path items use 'stop_id'
        if (s.stop_id) coordLookup[s.stop_id] = [s.lat, s.lon];
    });
    if (result.segments) {
        result.segments.forEach(seg => {
            if (seg.stop_positions) {
                Object.keys(seg.stop_positions).forEach(sid => {
                    if (!coordLookup[sid]) coordLookup[sid] = seg.stop_positions[sid];
                });
            }
        });
    }

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

    // Draw stop markers on route — these are the REAL peron positions,
    // interactive and clickable (select as start/end).
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

        const marker = L.circleMarker([stop.lat, stop.lon], {
            radius: markerRadius,
            fillColor: markerColor,
            color: '#fff',
            weight: 2,
            fillOpacity: 1,
            interactive: true,
        }).addTo(state.routeLayer);

        // The peron on the route is clickable — same stop-group popup as the
        // overview dots (the averaged overview dot is hidden for this group).
        const groupObj = state.stopGroups.find(g => g.id === stop.group_id);
        if (groupObj) {
            marker.on('click', function(e) {
                showCustomPopup(groupObj, e);
            });
        }
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

    // Use safe DOM manipulation instead of innerHTML
    const stepsHtml = buildStepsHtml(result);
    const desktopSteps = document.getElementById('route-steps');
    const mobileSteps = document.getElementById('mobile-route-steps');
    
    if (desktopSteps) {
        desktopSteps.innerHTML = '';
        desktopSteps.appendChild(createElementFromHtml(stepsHtml));
    }
    if (mobileSteps) {
        mobileSteps.innerHTML = '';
        mobileSteps.appendChild(createElementFromHtml(stepsHtml));
    }
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