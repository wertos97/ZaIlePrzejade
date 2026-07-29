#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Serves static files and provides API endpoints for route finding and cost calculation.
"""

import gzip
import io
import json
import math
import os
import heapq
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont

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
# Build search index for fast stop name lookups
# ============================================================
# inverted_index: prefix -> set of (name_lower, group_id)
# This avoids iterating all stop names on every search query
_stop_search_index = {}  # prefix -> [(name_lower, group_id), ...]
for name_lower, group_ids in stops_by_name_grouped.items():
    # Index all substrings of length 2+ for fast prefix matching
    for i in range(len(name_lower)):
        for length in range(2, min(6, len(name_lower) - i + 1)):
            prefix = name_lower[i:i+length]
            if prefix not in _stop_search_index:
                _stop_search_index[prefix] = []
            for gid in group_ids:
                _stop_search_index[prefix].append((name_lower, gid))

# Also index stop codes
for s in stops_list:
    code = s.get('code', '').lower()
    if code:
        group_id = stop_to_group.get(s['id'])
        if group_id:
            for i in range(len(code)):
                for length in range(2, min(6, len(code) - i + 1)):
                    prefix = code[i:i+length]
                    if prefix not in _stop_search_index:
                        _stop_search_index[prefix] = []
                    _stop_search_index[prefix].append(('_code_' + code, group_id))

print(f"  Built search index with {len(_stop_search_index)} prefixes")

# ============================================================
# Ticket pricing configuration (loaded from pricing.json)
# ============================================================
PRICING_PATH = os.path.join(BASE_DIR, 'pricing.json')

with open(PRICING_PATH, encoding='utf-8') as f:
    pricing = json.load(f)

BASE_DISTANCE = pricing['base_distance_km']
BASE_COST_REGULAR = pricing['base_cost_regular']
BASE_COST_REDUCED = pricing['base_cost_reduced']
SEGMENT_DISTANCE = pricing['segment_distance_km']
SEGMENT_COST_REGULAR = pricing['segment_cost_regular']
SEGMENT_COST_REDUCED = pricing['segment_cost_reduced']
MAX_COST_REGULAR = pricing['max_cost_regular']
MAX_COST_REDUCED = pricing['max_cost_reduced']

print(f"  Loaded pricing from {PRICING_PATH}")


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

    # For each segment, find all route lines that share the same stops
    # (different bus/tram lines that run through the same stops)
    for segment in segments:
        if segment['stops'] and len(segment['stops']) >= 2:
            # Find routes that serve the FIRST two stops of this segment
            # (all platforms of those groups)
            first_group = stop_to_group.get(segment['stops'][0], '')
            second_group = stop_to_group.get(segment['stops'][1], '')
            
            # Collect all routes that serve any platform pair from first to second group
            first_pair_routes = set()
            for p1 in stops_grouped.get(first_group, {}).get('platforms', []):
                for p2 in stops_grouped.get(second_group, {}).get('platforms', []):
                    routes = stop_pair_routes.get((p1['id'], p2['id']), [])
                    first_pair_routes.update(routes)
            
            if first_pair_routes:
                segment['all_routes'] = sorted(first_pair_routes)
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

    # Recalculate distances from stop coordinates for consistency
    # (edge distances vary by route, but user sees same stops)
    # First normalize segment distances
    for segment in segments:
        if len(segment['stops']) >= 2:
            seg_dist = 0.0
            for j in range(len(segment['stops']) - 1):
                s1 = stops_by_id.get(segment['stops'][j], {})
                s2 = stops_by_id.get(segment['stops'][j + 1], {})
                if s1 and s2:
                    g1 = stops_grouped.get(stop_to_group.get(segment['stops'][j], ''), {})
                    g2 = stops_grouped.get(stop_to_group.get(segment['stops'][j + 1], ''), {})
                    lat1 = g1.get('lat', s1.get('lat', 0))
                    lon1 = g1.get('lon', s1.get('lon', 0))
                    lat2 = g2.get('lat', s2.get('lat', 0))
                    lon2 = g2.get('lon', s2.get('lon', 0))
                    seg_dist += haversine_km(lat1, lon1, lat2, lon2)
            segment['distance'] = round(seg_dist, 4)

    # Then normalize total distance
    normalized_distance = 0.0
    for i in range(len(path_stops) - 1):
        s1 = path_stops[i]
        s2 = path_stops[i + 1]
        if not s2.get('is_transfer') and not s1.get('is_transfer'):
            normalized_distance += haversine_km(s1['lat'], s1['lon'], s2['lat'], s2['lon'])

    cost_regular, cost_reduced = calculate_cost(normalized_distance)

    return {
        'total_distance': round(normalized_distance, 4),
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
                    # For short mode: just compare real distance
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

# Paths commonly targeted by vulnerability scanners
BLOCKED_PREFIXES = (
    '/.', '/_',
)
BLOCKED_PATHS = (
    '/.env', '/.env.old', '/.env.local', '/.env.production', '/.env.development',
    '/.env.backup', '/.env.bak', '/.env.config', '/.env.staging',
    '/.git', '/.git/', '/.git/config', '/.git/HEAD',
    '/.htaccess', '/.htpasswd',
    '/.vscode', '/.vscode/', '/.vscode/sftp.json',
    '/wp-admin', '/wp-login.php', '/xmlrpc.php',
    '/admin', '/phpmyadmin',
    '/cgi-bin', '/scripts',
    '/server-status', '/server-info',
    '/favicon.ico',  # unnecessary 404s
)


class MPKRequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler for MPK Kraków app."""

    # Hide server version from headers
    server_version = ''
    sys_version = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_HEAD(self):
        """Handle HEAD requests same as GET."""
        self.do_GET()

    def do_POST(self):
        """Reject POST - not needed for this read-only app."""
        self.send_error_page(405)

    def do_PUT(self):
        """Reject PUT."""
        self.send_error_page(405)

    def do_DELETE(self):
        """Reject DELETE."""
        self.send_error_page(405)

    def do_PATCH(self):
        """Reject PATCH."""
        self.send_error_page(405)

    def do_OPTIONS(self):
        """Handle OPTIONS for CORS preflight."""
        self.send_response(204)
        self.send_header('Allow', 'GET, HEAD, OPTIONS')
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith('/api/'):
            self.handle_api(path, query)
            return

        # Block known attack paths and hidden files/dirs
        if path in BLOCKED_PATHS or any(path.startswith(p) for p in BLOCKED_PREFIXES):
            self.send_error_page(404)
            return

        if path == '/' or path == '':
            path = '/index.html'

        # Prevent directory listing - only serve known files
        # Resolve the file path and ensure it stays within PUBLIC_DIR
        file_path = os.path.normpath(os.path.join(PUBLIC_DIR, path.lstrip('/')))
        if not file_path.startswith(PUBLIC_DIR):
            self.send_error_page(403)
            return

        if not os.path.isfile(file_path):
            self.send_error_page(404)
            return

        # Check if this is a crawler (Facebook, Twitter, etc.)
        user_agent = self.headers.get('User-Agent', '').lower()
        is_crawler = any(bot in user_agent for bot in [
            'facebookexternalhit', 'twitterbot', 'linkedinbot', 'slackbot',
            'telegrambot', 'discordbot', 'whatsapp', 'skypeuripreview',
            'applebot', 'bingbot', 'googlebot', 'yandexbot', 'duckduckbot',
            'facebot', 'meta-externalagent'
        ])
        
        # For crawlers with route params, serve modified HTML
        from_stop = query.get('from', [''])[0]
        to_stop = query.get('to', [''])[0]
        mode = query.get('mode', ['short'])[0]
        
        if is_crawler and from_stop and to_stop and path in ('/', '', '/index.html'):
            self.serve_modified_html(from_stop, to_stop, mode)
            return

        # Serve the file using SimpleHTTPRequestHandler
        super().do_GET()
    
    def serve_modified_html(self, from_stop, to_stop, mode):
        """Serve HTML with modified OG tags for crawlers."""
        try:
            # Read the original HTML
            file_path = os.path.join(PUBLIC_DIR, 'index.html')
            with open(file_path, 'r', encoding='utf-8') as f:
                html = f.read()
            
            # Get stop names
            from_name = "Przystanek początkowy"
            to_name = "Przystanek końcowy"
            
            from_group = stops_grouped.get(from_stop)
            to_group = stops_grouped.get(to_stop)
            
            if from_group:
                from_name = from_group['name']
            if to_group:
                to_name = to_group['name']
            
            # Generate dynamic OG image URL
            og_image_url = f"https://zaileprzeja.de/api/og-image?from={from_stop}&to={to_stop}&mode={mode}"
            current_url = f"https://zaileprzeja.de/?from={from_stop}&to={to_stop}&mode={mode}"
            title = f"Za Ile Przejadę? {from_name} → {to_name}"
            description = f"Oblicz koszt przejazdu z {from_name} do {to_name} w nowym systemie biletów MPK Kraków."
            
            # Replace meta tags
            html = html.replace(
                '<meta property="og:title" content="Za Ile Przejadę? - Kalkulator cen biletów MPK Kraków 2027">',
                f'<meta property="og:title" content="{title}">'
            )
            html = html.replace(
                '<meta property="og:description" content="Oblicz koszt przejazdu komunikacją miejską w Krakowie w oparciu o nowy system biletów opartych na odległości.">',
                f'<meta property="og:description" content="{description}">'
            )
            html = html.replace(
                '<meta property="og:url" content="https://zaileprzeja.de/">',
                f'<meta property="og:url" content="{current_url}">'
            )
            html = html.replace(
                '<meta property="og:image" content="https://zaileprzeja.de/og-image.svg">',
                f'<meta property="og:image" content="{og_image_url}">'
            )
            html = html.replace(
                '<meta name="twitter:title" content="Za Ile Przejadę? - Kalkulator cen biletów MPK Kraków 2027">',
                f'<meta name="twitter:title" content="{title}">'
            )
            html = html.replace(
                '<meta name="twitter:description" content="Oblicz koszt przejazdu komunikacją miejską w Krakowie w oparciu o nowy system biletów opartych na odległości.">',
                f'<meta name="twitter:description" content="{description}">'
            )
            html = html.replace(
                '<meta name="twitter:image" content="https://zaileprzeja.de/og-image.svg">',
                f'<meta name="twitter:image" content="{og_image_url}">'
            )
            
            # Add og:site_name if not present
            if 'og:site_name' not in html:
                html = html.replace(
                    '<meta property="og:locale"',
                    f'<meta property="og:site_name" content="Za Ile Przejadę?">\n    <meta property="og:locale"'
                )
            
            # Send modified HTML
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(body)
            
        except Exception as e:
            # Fallback to normal serving
            super().do_GET()

    def handle_api(self, path, query):
        """Handle API requests."""
        try:
            if path == '/api/stops':
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
                # Use precomputed search index for fast lookups
                # Check if query matches any indexed prefix (first 5 chars)
                results = []
                seen = set()
                # Try indexed lookup first (fast path)
                for prefix_len in range(min(5, len(q)), 1, -1):
                    prefix = q[:prefix_len]
                    if prefix in _stop_search_index:
                        for name_lower, group_id in _stop_search_index[prefix]:
                            if group_id not in seen and q in name_lower:
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
                        if results:
                            break
                # Fallback to full scan if index didn't find enough
                if not results:
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
                self.serve_json(results[:50])

            elif path == '/api/stop-platforms':
                group_id = query.get('id', [''])[0]
                if not group_id or not group_id.startswith('group_'):
                    self.serve_json({'error': 'Invalid stop group ID'})
                    return
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

                if not from_stop.startswith('group_') or not to_stop.startswith('group_'):
                    self.serve_json({'error': 'Invalid stop ID format'})
                    return

                if mode not in ('short', 'convenient'):
                    mode = 'short'

                result, error = find_route_between_groups(from_stop, to_stop, mode)
                if result is None:
                    self.serve_json({'error': error})
                else:
                    self.serve_json(result)

            elif path == '/api/cost':
                try:
                    distance = float(query.get('distance', ['0'])[0])
                except (ValueError, TypeError):
                    self.serve_json({'error': 'Invalid distance parameter'})
                    return
                if distance < 0:
                    distance = 0.0
                cost_reg, cost_red = calculate_cost(distance)
                self.serve_json({
                    'distance': distance,
                    'cost_regular': cost_reg,
                    'cost_reduced': cost_red,
                })

            elif path == '/api/shapes':
                route_id = query.get('route_id', [''])[0]
                if not route_id:
                    self.serve_json({'error': 'Missing route_id parameter'})
                    return
                shape = route_shapes.get(route_id, [])
                self.serve_json({'route_id': route_id, 'shape': shape})

            elif path == '/api/health':
                self.serve_json({'status': 'ok'})

            elif path == '/api/routes':
                self.serve_json(routes_list)

            elif path == '/api/stop':
                stop_id = query.get('id', [''])[0]
                if not stop_id:
                    self.serve_json({'error': 'Missing id parameter'})
                    return
                stop = stops_by_id.get(stop_id)
                if stop:
                    self.serve_json(stop)
                else:
                    self.serve_json({'error': 'Stop not found'})

            elif path == '/api/og-image':
                from_stop = query.get('from', [''])[0]
                to_stop = query.get('to', [''])[0]
                mode = query.get('mode', ['short'])[0]
                
                # Generate OG image
                img = self.generate_og_image(from_stop, to_stop, mode)
                buf = io.BytesIO()
                img.save(buf, format='PNG', optimize=True)
                buf.seek(0)
                
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(buf.getvalue())))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(buf.getvalue())

            else:
                self.serve_json({'error': 'Unknown API endpoint'})

        except Exception as e:
            self.serve_json({'error': 'Internal server error'})

    def serve_json(self, data):
        """Serve JSON response. Cloudflare handles compression."""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def send_error_page(self, code):
        """Send a minimal error response without revealing server details."""
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def end_headers(self):
        """Suppress default end_headers behavior."""
        super().end_headers()

    def _wrap_text(self, text, font, max_width, draw):
        """Wrap text to fit within max_width, returning list of lines."""
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = f"{current_line} {word}".strip()
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return lines if lines else [text]

    def generate_og_image(self, from_stop_id, to_stop_id, mode):
        """Generate OG image with route info — optimized for social media readability."""
        # Standard OG image size
        width, height = 1200, 630
        
        # Create image with solid dark background
        img = Image.new('RGB', (width, height), '#0d2137')
        draw = ImageDraw.Draw(img)
        
        # Subtle gradient overlay
        for y in range(height):
            alpha = y / height
            r = int(13 + alpha * 12)
            g = int(33 + alpha * 18)
            b = int(55 + alpha * 25)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        
        # Load fonts
        try:
            font_label = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 26)
            font_stop = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 48)
            font_stop_sm = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 48)
            font_cost_label = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 26)
            font_cost = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 100)
            font_website = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 22)
        except:
            font_label = font_stop = font_stop_sm = font_cost_label = font_cost = font_website = ImageFont.load_default()
        
        # Get stop names
        from_name = "Przystanek początkowy"
        to_name = "Przystanek końcowy"
        cost_text = ""
        
        if from_stop_id and to_stop_id:
            from_group = stops_grouped.get(from_stop_id)
            to_group = stops_grouped.get(to_stop_id)
            if from_group:
                from_name = from_group['name']
            if to_group:
                to_name = to_group['name']
            result, _ = find_route_between_groups(from_stop_id, to_stop_id, mode)
            if result:
                cost_text = f"{result['cost_regular']:.2f} zł"
        
        # Layout constants
        left_margin = 60
        right_margin = 220  # space for logo on right
        text_width = width - left_margin - right_margin
        
        # --- "SKĄD" section ---
        y_cursor = 30
        draw.text((left_margin, y_cursor), "SKĄD", fill='#5dade2', font=font_label)
        y_cursor += 40
        
        # Wrap from_name
        from_lines = self._wrap_text(from_name, font_stop, text_width, draw)
        for i, line in enumerate(from_lines):
            draw.text((left_margin, y_cursor), line, fill='#ecf0f1', font=font_stop)
            bbox = draw.textbbox((0, 0), line, font=font_stop)
            y_cursor += bbox[3] - bbox[1] + 4
        
        y_cursor += 48
        
        # --- "DOKĄD" section ---
        draw.text((left_margin, y_cursor), "DOKĄD", fill='#5dade2', font=font_label)
        y_cursor += 40
        
        to_lines = self._wrap_text(to_name, font_stop, text_width, draw)
        for i, line in enumerate(to_lines):
            draw.text((left_margin, y_cursor), line, fill='#ecf0f1', font=font_stop)
            bbox = draw.textbbox((0, 0), line, font=font_stop)
            y_cursor += bbox[3] - bbox[1] + 4
        
        y_cursor += 48
        
        # --- Separator line ---
        draw.line([(left_margin, y_cursor), (left_margin + 140, y_cursor)], fill='#5dade2', width=3)
        y_cursor += 24
        
        # --- "CENA BILETU" section ---
        if cost_text:
            draw.text((left_margin, y_cursor), "CENA BILETU", fill='#5dade2', font=font_cost_label)
            y_cursor += 36
            draw.text((left_margin, y_cursor), cost_text, fill='#ffffff', font=font_cost)
        
        # --- Logo in bottom right (6 shapes) ---
        logo_x = width - 210
        logo_y = height - 160
        logo_w = 140
        logo_h = 90
        
        # Bus body
        draw.rounded_rectangle([logo_x, logo_y, logo_x + logo_w, logo_y + logo_h - 16], radius=12, fill='#2874a6')
        # Windows (same size, symmetric)
        win_w = 22
        win_h = 22
        win_y = logo_y + 14
        gap_w = (logo_w - 3 * win_w) / 4
        for i in range(3):
            win_x = logo_x + gap_w + i * (win_w + gap_w)
            draw.rounded_rectangle([win_x, win_y, win_x + win_w, win_y + win_h], radius=3, fill='#aed6f1')
        # Wheels
        wheel_r = 12
        wheel_y = logo_y + logo_h - 6
        draw.ellipse([logo_x + 30 - wheel_r, wheel_y - wheel_r, logo_x + 30 + wheel_r, wheel_y + wheel_r], fill='#2c3e50')
        draw.ellipse([logo_x + logo_w - 30 - wheel_r, wheel_y - wheel_r, logo_x + logo_w - 30 + wheel_r, wheel_y + wheel_r], fill='#2c3e50')
        
        # Website text below logo
        bbox = draw.textbbox((0, 0), "zaileprzeja.de", font=font_website)
        text_w = bbox[2] - bbox[0]
        draw.text((logo_x + (logo_w - text_w) // 2, logo_y + logo_h + 12), "zaileprzeja.de", fill='#7f8c8d', font=font_website)
        
        return img

    def log_message(self, format, *args):
        """Log only errors and API requests, suppress static file requests."""
        msg = format % args
        # Log errors and API requests, skip static file requests
        if '/api/' in msg or 'Error' in msg or 'error' in msg:
            import sys
            print(f"  {msg}", file=sys.stderr)


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