#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Serves static files and provides API endpoints for route finding and cost calculation.
"""

import json
import math
import os
import heapq
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')

# ============================================================
# Load processed data
# ============================================================
print("Loading processed data...")

with open(os.path.join(PROCESSED_DIR, 'stops.json'), encoding='utf-8') as f:
    stops_list = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'adjacency.json'), encoding='utf-8') as f:
    adjacency_raw = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'routes.json'), encoding='utf-8') as f:
    routes_list = json.load(f)

with open(os.path.join(PROCESSED_DIR, 'shapes.json'), encoding='utf-8') as f:
    route_shapes = json.load(f)

print(f"  Loaded {len(stops_list)} stops, {len(routes_list)} routes")

# ============================================================
# Build data structures
# ============================================================

# Stop lookup by ID
stops_by_id = {s['id']: s for s in stops_list}

# Group stops by name - combine different platforms of the same stop
stops_grouped = {}  # group_id -> {name, platforms: [{id, code, lat, lon, mode}], lat, lon}
stops_by_name_grouped = defaultdict(list)  # name_lower -> [group_id]

for s in stops_list:
    name_lower = s['name'].lower()
    if name_lower not in stops_by_name_grouped:
        # Create new group
        group_id = f"group_{len(stops_grouped)}"
        stops_grouped[group_id] = {
            'id': group_id,
            'name': s['name'],
            'platforms': [],
            'lat': s['lat'],
            'lon': s['lon'],
            'modes': set(),
        }
        stops_by_name_grouped[name_lower].append(group_id)
    
    # Add to existing group
    group_id = stops_by_name_grouped[name_lower][0]
    group = stops_grouped[group_id]
    group['platforms'].append({
        'id': s['id'],
        'code': s['code'],
        'lat': s['lat'],
        'lon': s['lon'],
        'mode': s['mode'],
    })
    group['modes'].add(s['mode'])
    # Update center position (average)
    n = len(group['platforms'])
    group['lat'] = (group['lat'] * (n - 1) + s['lat']) / n
    group['lon'] = (group['lon'] * (n - 1) + s['lon']) / n

# Convert modes set to list for JSON
for g in stops_grouped.values():
    g['modes'] = sorted(list(g['modes']))

# Build lookup: stop_id -> group_id
stop_to_group = {}
for group_id, group in stops_grouped.items():
    for p in group['platforms']:
        stop_to_group[p['id']] = group_id

print(f"  Grouped {len(stops_list)} stops into {len(stops_grouped)} groups")

# Routes lookup by ID
routes_by_id = {r['route_id']: r for r in routes_list}

# Build bidirectional adjacency list
adjacency = defaultdict(list)
for stop_id, edges in adjacency_raw.items():
    for edge in edges:
        adjacency[stop_id].append(dict(edge))
        # Add reverse edge
        to_stop = edge['to']
        reverse_edge = {
            'to': stop_id,
            'distance': edge['distance'],
            'route_id': edge['route_id'],
            'direction': edge['direction'],
            'mode': edge['mode'],
            'headsign': edge['headsign'],
        }
        adjacency[to_stop].append(reverse_edge)

print(f"  Built bidirectional adjacency list with {len(adjacency)} nodes")

# Precompute stop-to-stop route lookup for fast all_routes calculation
# stop_pair_routes[(from_stop, to_stop)] = [route_id1, route_id2, ...]
stop_pair_routes = {}
for stop_id, edges in adjacency_raw.items():
    for edge in edges:
        if edge['route_id'] != 'transfer':
            key = (stop_id, edge['to'])
            if key not in stop_pair_routes:
                stop_pair_routes[key] = []
            if edge['route_id'] not in stop_pair_routes[key]:
                stop_pair_routes[key].append(edge['route_id'])

print(f"  Built stop pair route lookup with {len(stop_pair_routes)} entries")

# ============================================================
# Ticket pricing configuration
# ============================================================
BASE_DISTANCE = 3.5       # km - base distance included in base price
BASE_COST_REGULAR = 4.00  # PLN
BASE_COST_REDUCED = 2.00  # PLN
SEGMENT_DISTANCE = 0.5    # km - each additional 500m
SEGMENT_COST_REGULAR = 0.50  # PLN per 500m
SEGMENT_COST_REDUCED = 0.25  # PLN per 500m
MAX_COST_REGULAR = 9.00   # PLN
MAX_COST_REDUCED = 4.50   # PLN


def calculate_cost(distance_km):
    """
    Calculate ticket cost based on distance.

    Pricing:
    - Up to 3.5km: 4.00 PLN regular, 2.00 PLN reduced
    - Each additional 500m: +0.50 PLN regular, +0.25 PLN reduced
    - Maximum: 9.00 PLN regular, 4.50 PLN reduced
    """
    if distance_km <= 0:
        return 0.0, 0.0

    if distance_km <= BASE_DISTANCE:
        return BASE_COST_REGULAR, BASE_COST_REDUCED

    additional_distance = distance_km - BASE_DISTANCE
    additional_segments = math.ceil(additional_distance / SEGMENT_DISTANCE)

    cost_regular = BASE_COST_REGULAR + additional_segments * SEGMENT_COST_REGULAR
    cost_reduced = BASE_COST_REDUCED + additional_segments * SEGMENT_COST_REDUCED

    cost_regular = min(cost_regular, MAX_COST_REGULAR)
    cost_reduced = min(cost_reduced, MAX_COST_REDUCED)

    return round(cost_regular, 2), round(cost_reduced, 2)


# ============================================================
# Dijkstra's algorithm for shortest path
# ============================================================

# Precompute stop coordinates for A* heuristic
stop_coords = {}
for stop_id, edges in adjacency.items():
    # Get coordinates from stops_by_id
    stop_info = stops_by_id.get(stop_id, {})
    stop_coords[stop_id] = (stop_info.get('lat', 0), stop_info.get('lon', 0))


def haversine_km(lat1, lon1, lat2, lon2):
    """Approximate distance in km between two coordinates."""
    # Simple flat-earth approximation for Kraków area (good enough for heuristic)
    dlat = (lat1 - lat2) * 111.32
    dlon = (lon1 - lon2) * 111.32 * math.cos((lat1 + lat2) / 2 * math.pi / 180)
    return math.sqrt(dlat * dlat + dlon * dlon)


def find_shortest_path(start_id, end_id):
    """
    Find the shortest path using A* algorithm with Euclidean heuristic.
    Minimizes distance, with a small penalty for route changes to avoid
    unnecessary zigzagging between routes. Still properly tracks route
    changes and reports them as segments/transfers.
    """
    if start_id not in adjacency:
        return None, "Przystanek początkowy nie został znaleziony w grafie"
    if end_id not in adjacency:
        return None, "Przystanek końcowy nie został znaleziony w grafie"

    # Precompute heuristic for the target
    end_coords = stop_coords.get(end_id, (0, 0))

    # Penalty for changing routes (used only for routing decisions, not for display)
    CHANGE_PENALTY = 0.3

    # Track distance by (stop_id, route_id) state
    # Priority queue: (estimated_total, penalized_dist, real_dist, stop, route)
    # estimated_total = penalized_dist + heuristic(stop, end)
    start_coords = stop_coords.get(start_id, (0, 0))
    h_start = haversine_km(start_coords[0], start_coords[1], end_coords[0], end_coords[1])
    pq = [(h_start, 0.0, 0.0, start_id, None)]
    best = {(start_id, None): (0.0, 0.0)}  # (penalized_dist, real_dist)
    prev = {}

    # Track best found real distance for early pruning
    best_found_real = float('inf')

    while pq:
        est_total, pen_dist, real_dist, stop, route = heapq.heappop(pq)

        state = (stop, route)
        best_pen, _ = best.get(state, (float('inf'), 0))
        if pen_dist > best_pen:
            continue

        # Prune: if even the optimistic estimate is worse than best found, skip
        if est_total >= best_found_real:
            continue

        if stop == end_id:
            # Recalculate real distance by summing edge distances along the path
            path_edges = []
            cur = (stop, route)
            while cur in prev:
                prev_stop, prev_route, edge = prev[cur]
                path_edges.append(edge)
                cur = (prev_stop, prev_route)
            path_edges.reverse()
            real_total = sum(e['distance'] for e in path_edges if e is not None)
            return reconstruct_path(prev, start_id, end_id, route, real_total), None

        for edge in adjacency.get(stop, []):
            next_stop = edge['to']
            next_route = edge['route_id']
            new_real = real_dist + edge['distance']
            new_pen = pen_dist + edge['distance']

            # Add a small penalty when changing routes (but not for transfer edges)
            if route is not None and next_route != 'transfer' and next_route != route:
                new_pen += CHANGE_PENALTY

            next_state = (next_stop, next_route)
            best_pen, _ = best.get(next_state, (float('inf'), 0))
            if new_pen < best_pen:
                # A* heuristic: estimated remaining distance
                coords = stop_coords.get(next_stop, (0, 0))
                h = haversine_km(coords[0], coords[1], end_coords[0], end_coords[1])
                estimated = new_pen + h
                best[next_state] = (new_pen, new_real)
                prev[next_state] = (stop, route, edge)
                heapq.heappush(pq, (estimated, new_pen, new_real, next_stop, next_route))

    return None, "Nie znaleziono trasy między tymi przystankami"


def reconstruct_path(prev, start_id, end_id, end_route, total_distance):
    """
    Reconstruct the path from start to end using the prev dictionary.
    Returns segments (rides on same route) and transfers between them.
    """
    path_with_edges = []
    current_state = (end_id, end_route)
    while current_state in prev:
        prev_stop, prev_route, edge = prev[current_state]
        path_with_edges.append((current_state[0], current_state[1], edge))
        current_state = (prev_stop, prev_route)
    path_with_edges.append((start_id, None, None))
    path_with_edges.reverse()

    # Build segments and transfers
    segments = []
    transfers = []
    current_segment = None
    stop_ids = [p[0] for p in path_with_edges]

    for i, (stop_id, route_id, edge) in enumerate(path_with_edges):
        if i == 0:
            # Start stop - initialize first segment
            continue

        prev_stop = path_with_edges[i - 1][0]
        prev_route = path_with_edges[i - 1][1]

        if route_id == 'transfer':
            # A transfer edge connects two different platforms of the same physical stop
            # This is NOT a user-visible transfer - just moving between platforms
            if current_segment is not None:
                # Save current segment and start a pause for the transfer
                segments.append(current_segment)
                current_segment = None
            # Record the transfer between routes (but don't show as a separate step)
            transfer_from = prev_route if prev_route != 'transfer' else path_with_edges[i - 2][1] if i >= 2 else None
            # Look ahead to find what route we're transferring to
            transfer_to = None
            for j in range(i + 1, len(path_with_edges)):
                if path_with_edges[j][1] != 'transfer':
                    transfer_to = path_with_edges[j][1]
                    break
            if transfer_from and transfer_to and transfer_from != transfer_to:
                # Avoid duplicate transfers (consecutive transfer edges)
                if not transfers or transfers[-1]['from_route'] != transfer_from or transfers[-1]['to_route'] != transfer_to:
                    # Get stop name from group
                    group_id = stop_to_group.get(stop_id, '')
                    group = stops_grouped.get(group_id, {})
                    stop_name = group.get('name', stops_by_id.get(stop_id, {}).get('name', ''))
                    transfers.append({
                        'stop_id': stop_id,
                        'stop_name': stop_name,
                        'from_route': transfer_from,
                        'to_route': transfer_to,
                    })
        else:
            if current_segment is None:
                current_segment = {
                    'route_id': route_id,
                    'mode': edge['mode'],
                    'headsign': edge['headsign'],
                    'distance': 0.0,
                    'stops': [prev_stop],
                    'end_stop': stop_id,
                }

            if current_segment['route_id'] != route_id:
                # Route changed - save current segment and start new one.
                # Record the transfer between routes.
                if prev_route and prev_route != 'transfer' and prev_route != route_id:
                    # Avoid duplicate transfers
                    if not transfers or transfers[-1]['from_route'] != prev_route or transfers[-1]['to_route'] != route_id:
                        # Get stop name from group
                        group_id = stop_to_group.get(prev_stop, '')
                        group = stops_grouped.get(group_id, {})
                        stop_name = group.get('name', stops_by_id.get(prev_stop, {}).get('name', ''))
                        transfers.append({
                            'stop_id': prev_stop,
                            'stop_name': stop_name,
                            'from_route': prev_route,
                            'to_route': route_id,
                        })
                segments.append(current_segment)
                current_segment = {
                    'route_id': route_id,
                    'mode': edge['mode'],
                    'headsign': edge['headsign'],
                    'distance': 0.0,
                    'stops': [prev_stop],
                    'end_stop': stop_id,
                }

            current_segment['distance'] += edge['distance']
            current_segment['end_stop'] = stop_id
            # Only add stops that are actual route stops (not transfers)
            current_segment['stops'].append(stop_id)

    if current_segment is not None:
        segments.append(current_segment)

    # Merge consecutive segments that are on the same route
    # (separated only by platform-to-platform transfer edges)
    merged_segments = []
    i = 0
    while i < len(segments):
        merged = segments[i]
        # Look ahead: if the next segment is on the same route, merge them
        while (i + 1 < len(segments) and
               segments[i + 1]['route_id'] == merged['route_id']):
            i += 1
            next_seg = segments[i]
            # Merge stops (avoid duplicate at boundary)
            if merged['stops'] and merged['stops'][-1] == next_seg['stops'][0]:
                merged['stops'].extend(next_seg['stops'][1:])
            else:
                merged['stops'].extend(next_seg['stops'])
            merged['distance'] += next_seg['distance']
            merged['end_stop'] = next_seg['end_stop']
        merged_segments.append(merged)
        i += 1
    segments = merged_segments

    # For each segment, find all route lines that serve the same stop sequence
    # (different bus/tram lines that share the same physical route)
    for segment in segments:
        if segment['stops'] and len(segment['stops']) >= 2:
            first_stop = segment['stops'][0]
            second_stop = segment['stops'][1] if len(segment['stops']) > 1 else None
            # Find all routes that serve this stop pair in the forward direction
            all_routes = stop_pair_routes.get((first_stop, second_stop), [])
            if all_routes:
                segment['all_routes'] = sorted(all_routes)
            else:
                segment['all_routes'] = [segment['route_id']] if segment['route_id'] else []
        else:
            segment['all_routes'] = [segment['route_id']] if segment['route_id'] else []

        # Add human-readable stop names for the first and last stop
        first_stop_id = segment['stops'][0] if segment['stops'] else None
        last_stop_id = segment['stops'][-1] if segment['stops'] else None
        if first_stop_id:
            group_id = stop_to_group.get(first_stop_id, '')
            group = stops_grouped.get(group_id, {})
            segment['first_stop_name'] = group.get('name', stops_by_id.get(first_stop_id, {}).get('name', first_stop_id))
        if last_stop_id:
            group_id = stop_to_group.get(last_stop_id, '')
            group = stops_grouped.get(group_id, {})
            segment['last_stop_name'] = group.get('name', stops_by_id.get(last_stop_id, {}).get('name', last_stop_id))

    # Build path with stop info, using group-averaged coordinates
    # and deduplicating consecutive same-name stops.
    # Skip transfer edges (platform-to-platform connections) as they
    # are not user-visible stops.
    path_stops = []
    last_name = None
    for i, stop_id in enumerate(stop_ids):
        stop_info = stops_by_id.get(stop_id, {})
        route_id = path_with_edges[i][1] if i < len(path_with_edges) else None
        is_transfer = (route_id == 'transfer')
        group_id = stop_to_group.get(stop_id, '')
        group = stops_grouped.get(group_id, {})

        stop_name = group.get('name', stop_info.get('name', ''))

        # Skip transfer edges - they are platform-to-platform connections
        # that are not user-visible stops
        if is_transfer:
            continue

        # Skip consecutive same-name stops (different platforms of same stop)
        if last_name == stop_name:
            continue
        last_name = stop_name

        # Use group-averaged coordinates for consistent distance calculation
        lat = group.get('lat', stop_info.get('lat', 0))
        lon = group.get('lon', stop_info.get('lon', 0))

        path_stops.append({
            'stop_id': stop_id,
            'name': stop_name,
            'lat': lat,
            'lon': lon,
            'code': stop_info.get('code', ''),
            'mode': stop_info.get('mode', ''),
            'route_id': route_id,
            'is_transfer': is_transfer,
        })

    cost_regular, cost_reduced = calculate_cost(total_distance)

    return {
        'total_distance': round(total_distance, 4),
        'cost_regular': cost_regular,
        'cost_reduced': cost_reduced,
        'path': path_stops,
        'segments': segments,
        'transfers': transfers,
    }


def find_route_between_groups(from_group_id, to_group_id, mode):
    """
    Find the best route between two stop groups.
    Tries all platform combinations and returns the best result.
    """
    from_group = stops_grouped.get(from_group_id)
    to_group = stops_grouped.get(to_group_id)

    if not from_group:
        return None, "Przystanek początkowy nie został znaleziony"
    if not to_group:
        return None, "Przystanek końcowy nie został znaleziony"

    if from_group_id == to_group_id:
        cost_reg, cost_red = calculate_cost(0)
        return {
            'total_distance': 0,
            'cost_regular': cost_reg,
            'cost_reduced': cost_red,
            'path': [{
                'stop_id': from_group['platforms'][0]['id'],
                'name': from_group['name'],
                'lat': from_group['lat'],
                'lon': from_group['lon'],
                'code': from_group['platforms'][0]['code'],
                'mode': from_group['platforms'][0]['mode'],
                'route_id': None,
                'is_transfer': False,
            }],
            'segments': [],
            'transfers': [],
        }, None

    best_result = None
    best_error = None

    # Try all platform combinations
    for from_platform in from_group['platforms']:
        for to_platform in to_group['platforms']:
            # Use the same routing algorithm for both modes (with change penalty
            # to avoid unnecessary zigzagging). The difference is in how results
            # are compared between platform combinations.
            result, error = find_shortest_path(from_platform['id'], to_platform['id'])

            if result is not None:
                if mode == 'convenient':
                    # For convenient mode: prioritize fewer transfers, then shorter distance
                    if (best_result is None or
                        len(result['transfers']) < len(best_result['transfers']) or
                        (len(result['transfers']) == len(best_result['transfers']) and
                         result['total_distance'] < best_result['total_distance'])):
                        best_result = result
                else:
                    # For short mode: just compare real distance (penalties are only
                    # used internally for routing decisions, not for comparison)
                    if best_result is None or result['total_distance'] < best_result['total_distance']:
                        best_result = result
            elif best_result is None:
                best_error = error

    if best_result is None:
        return None, best_error or "Nie znaleziono trasy między tymi przystankami"

    return best_result, None


# ============================================================
# HTTP Request Handler
# ============================================================

class MPKRequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler for MPK Kraków app."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith('/api/'):
            self.handle_api(path, query)
            return

        if path == '/' or path == '':
            path = '/index.html'

        super().do_GET()

    def handle_api(self, path, query):
        """Handle API requests."""
        try:
            if path == '/api/stops':
                # Return grouped stops (one per name)
                result = []
                for g in stops_grouped.values():
                    result.append({
                        'id': g['id'],
                        'name': g['name'],
                        'lat': round(g['lat'], 6),
                        'lon': round(g['lon'], 6),
                        'modes': g['modes'],
                        'platform_count': len(g['platforms']),
                    })
                self.serve_json(result)

            elif path == '/api/stops/search':
                q = query.get('q', [''])[0].lower().strip()
                if len(q) < 2:
                    self.serve_json([])
                    return
                results = []
                seen = set()
                for name_lower, group_ids in stops_by_name_grouped.items():
                    if q in name_lower:
                        for group_id in group_ids:
                            if group_id not in seen:
                                seen.add(group_id)
                                g = stops_grouped[group_id]
                                results.append({
                                    'id': g['id'],
                                    'name': g['name'],
                                    'lat': round(g['lat'], 6),
                                    'lon': round(g['lon'], 6),
                                    'modes': g['modes'],
                                    'platform_count': len(g['platforms']),
                                })
                # Also search by code
                for s in stops_list:
                    code = s.get('code', '').lower()
                    if q in code:
                        group_id = stop_to_group.get(s['id'])
                        if group_id and group_id not in seen:
                            seen.add(group_id)
                            g = stops_grouped[group_id]
                            results.append({
                                'id': g['id'],
                                'name': g['name'],
                                'lat': round(g['lat'], 6),
                                'lon': round(g['lon'], 6),
                                'modes': g['modes'],
                                'platform_count': len(g['platforms']),
                            })
                self.serve_json(results[:50])

            elif path == '/api/stop-platforms':
                group_id = query.get('id', [''])[0]
                group = stops_grouped.get(group_id)
                if group:
                    self.serve_json({
                        'id': group['id'],
                        'name': group['name'],
                        'lat': round(group['lat'], 6),
                        'lon': round(group['lon'], 6),
                        'modes': group['modes'],
                        'platforms': group['platforms'],
                    })
                else:
                    self.serve_json({'error': 'Stop group not found'})

            elif path == '/api/find-route':
                from_stop = query.get('from', [''])[0]
                to_stop = query.get('to', [''])[0]
                mode = query.get('mode', ['short'])[0]

                if not from_stop or not to_stop:
                    self.serve_json({'error': 'Missing from or to parameter'})
                    return

                result, error = find_route_between_groups(from_stop, to_stop, mode)
                if result is None:
                    self.serve_json({'error': error})
                else:
                    self.serve_json(result)

            elif path == '/api/cost':
                distance = float(query.get('distance', ['0'])[0])
                cost_reg, cost_red = calculate_cost(distance)
                self.serve_json({
                    'distance': distance,
                    'cost_regular': cost_reg,
                    'cost_reduced': cost_red,
                    'base_distance': BASE_DISTANCE,
                    'base_cost_regular': BASE_COST_REGULAR,
                    'base_cost_reduced': BASE_COST_REDUCED,
                    'segment_distance': SEGMENT_DISTANCE,
                    'segment_cost_regular': SEGMENT_COST_REGULAR,
                    'segment_cost_reduced': SEGMENT_COST_REDUCED,
                    'max_cost_regular': MAX_COST_REGULAR,
                    'max_cost_reduced': MAX_COST_REDUCED,
                })

            elif path == '/api/shapes':
                route_id = query.get('route_id', [''])[0]
                shape = route_shapes.get(route_id, [])
                self.serve_json({'route_id': route_id, 'shape': shape})

            elif path == '/api/routes':
                self.serve_json(routes_list)

            elif path == '/api/stop':
                stop_id = query.get('id', [''])[0]
                stop = stops_by_id.get(stop_id)
                if stop:
                    self.serve_json(stop)
                else:
                    self.serve_json({'error': 'Stop not found'})

            else:
                self.serve_json({'error': 'Unknown API endpoint'})

        except Exception as e:
            self.serve_json({'error': str(e)})

    def serve_json(self, data):
        """Serve JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def main():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), MPKRequestHandler)
    print(f"\nServer running at http://localhost:{port}")
    print(f"Serving static files from: {PUBLIC_DIR}")
    print(f"API endpoints:")
    print(f"  /api/stops - All stops (grouped)")
    print(f"  /api/stops/search?q=<query> - Search stops")
    print(f"  /api/stop-platforms?id=<group_id> - Get platforms for a stop")
    print(f"  /api/find-route?from=<id>&to=<id> - Find route")
    print(f"  /api/cost?distance=<km> - Calculate cost")
    print(f"  /api/shapes?route_id=<id> - Get route shape")
    print(f"  /api/routes - All routes")
    print(f"  /api/stop?id=<id> - Get stop info")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.shutdown()


if __name__ == '__main__':
    main()