#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Serves static files and provides API endpoints for route finding and cost calculation.
"""

import functools
import gzip
import html
import io
import json
import math
import os
import heapq
import re
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from collections import defaultdict


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread, with a concurrency limit."""
    daemon_threads = True
    # Limit concurrent requests to protect the 256MB RAM / low-CPU environment
    request_queue_size = 64
    _active_requests = 0
    _active_lock = threading.Lock()
    MAX_CONCURRENT = 20

    def process_request(self, request, client_address):
        """Limit concurrent requests to prevent resource exhaustion."""
        with self._active_lock:
            if self._active_requests >= self.MAX_CONCURRENT:
                # Too many concurrent requests - reject immediately with 503
                try:
                    request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                    b'Content-Type: text/plain; charset=utf-8\r\n'
                                    b'Content-Length: 0\r\n'
                                    b'Connection: close\r\n'
                                    b'Retry-After: 1\r\n'
                                    b'\r\n')
                except OSError:
                    pass
                finally:
                    try:
                        request.close()
                    except OSError:
                        pass
                return
            self._active_requests += 1

        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active_requests -= 1
            raise

    def process_request_thread(self, request, client_address):
        """Run the request in a thread, decrementing the active counter when done."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active_requests -= 1

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

stops_by_id = {s['id']: s for s in stops_list}

stops_grouped = {}
stops_by_name_grouped = defaultdict(list)

for s in stops_list:
    name_lower = s['name'].lower()
    if name_lower not in stops_by_name_grouped:
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
    n = len(group['platforms'])
    group['lat'] = (group['lat'] * (n - 1) + s['lat']) / n
    group['lon'] = (group['lon'] * (n - 1) + s['lon']) / n

for g in stops_grouped.values():
    g['modes'] = sorted(list(g['modes']))

stop_to_group = {}
for group_id, group in stops_grouped.items():
    for p in group['platforms']:
        stop_to_group[p['id']] = group_id

print(f"  Grouped {len(stops_list)} stops into {len(stops_grouped)} groups")

routes_by_id = {r['route_id']: r for r in routes_list}

adjacency = defaultdict(list)
for stop_id, edges in adjacency_raw.items():
    for edge in edges:
        adjacency[stop_id].append(dict(edge))
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

# Free memory: adjacency_raw is no longer needed after building adjacency and stop_pair_routes
del adjacency_raw

# ============================================================
# Build search index for fast stop name lookups
# ============================================================
_stop_search_index = {}
for name_lower, group_ids in stops_by_name_grouped.items():
    for i in range(len(name_lower)):
        for length in range(2, min(6, len(name_lower) - i + 1)):
            prefix = name_lower[i:i+length]
            if prefix not in _stop_search_index:
                _stop_search_index[prefix] = []
            for gid in group_ids:
                _stop_search_index[prefix].append((name_lower, gid))

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

# Free memory: stops_list is no longer needed after building all stop structures
del stops_list

# ============================================================
# Ticket pricing configuration
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

# Load logo SVG for OG image generation
_logo_svg_path = os.path.join(PUBLIC_DIR, 'logo.svg')
with open(_logo_svg_path, encoding='utf-8') as f:
    _logo_svg_content = f.read()


def _clean_logo_svg(content):
    """Clean logo SVG: strip XML declaration, Inkscape/Sodipodi metadata,
    and empty defs. Keeps style= attributes (they may override fill colors)."""
    content = re.sub(r'<\?xml[^>]*\?>', '', content, count=1)
    content = re.sub(r'<sodipodi:namedview.*?</sodipodi:namedview>', '', content, flags=re.S)
    content = re.sub(r'<inkscape:grid[^>]*/>', '', content)
    content = re.sub(r'<defs[^>]*>\s*</defs>', '', content)
    content = re.sub(r'<defs[^>]*/>', '', content)
    content = re.sub(r'\s+id="[^"]*"', '', content)
    content = re.sub(r'\n\s*\n+', '\n', content)
    return content.strip()


_logo_svg_content = _clean_logo_svg(_logo_svg_content)
print(f"  Loaded logo from {_logo_svg_path}")
# ============================================================
# Warmup: pre-compute routes for popular stop pairs at startup
# ============================================================
def _warmup_cache():
    """Pre-compute routes for the most common stop pairs to warm up caches."""
    import random
    group_ids = list(stops_grouped.keys())
    if len(group_ids) > 100:
        sample = random.sample(group_ids, 100)
    else:
        sample = group_ids
    
    count = 0
    for i, g1 in enumerate(sample):
        for g2 in sample[i+1:i+5]:
            find_route_between_groups(g1, g2, 'short')
            count += 1
    
    print(f"  Warmed up {count} route cache entries")


def calculate_cost(distance_km):
    """Calculate ticket cost based on distance."""
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
# A* pathfinding
# ============================================================

stop_coords = {}
for stop_id in adjacency:
    stop_info = stops_by_id.get(stop_id, {})
    stop_coords[stop_id] = (stop_info.get('lat', 0), stop_info.get('lon', 0))


def haversine_km(lat1, lon1, lat2, lon2):
    """Approximate distance in km between two coordinates."""
    dlat = (lat1 - lat2) * 111.32
    dlon = (lon1 - lon2) * 111.32 * math.cos((lat1 + lat2) / 2 * math.pi / 180)
    return math.sqrt(dlat * dlat + dlon * dlon)


# --- Pathfinding cache (bounded by size, thread-safe via GIL) ---
_FIND_CACHE_MAX = 10000
_FIND_CACHE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB max
_find_cache = {}
_find_cache_bytes = 0

def _cache_put_find(key, value):
    """Store a value in the find cache, evicting oldest entries if over size limit."""
    global _find_cache_bytes
    # Estimate size of the cached value (rough but effective)
    size = len(json.dumps(value, ensure_ascii=False)) if value[0] is not None else 64
    if size > _FIND_CACHE_MAX_BYTES:
        return  # Don't cache very large results
    if key in _find_cache:
        _find_cache_bytes -= _find_cache[key][1]
    # Evict oldest entries if over budget
    while _find_cache_bytes + size > _FIND_CACHE_MAX_BYTES and _find_cache:
        oldest_key = next(iter(_find_cache))
        _find_cache_bytes -= _find_cache[oldest_key][1]
        del _find_cache[oldest_key]
    _find_cache[key] = (value, size)
    _find_cache_bytes += size

def _cache_get_find(key):
    """Retrieve a value from the find cache."""
    entry = _find_cache.get(key)
    if entry is not None:
        return entry[0]
    return None

def find_shortest_path(start_id, end_id):
    """Find shortest path with cache."""
    cache_key = (start_id, end_id)
    cached = _cache_get_find(cache_key)
    if cached is not None:
        return cached

    if start_id not in adjacency:
        return None, "Przystanek początkowy nie został znaleziony w grafie"
    if end_id not in adjacency:
        return None, "Przystanek końcowy nie został znaleziony w grafie"

    end_coords = stop_coords.get(end_id, (0, 0))
    CHANGE_PENALTY = 0.3

    start_coords = stop_coords.get(start_id, (0, 0))
    h_start = haversine_km(start_coords[0], start_coords[1], end_coords[0], end_coords[1])
    pq = [(h_start, 0.0, 0.0, start_id, None)]
    best = {(start_id, None): (0.0, 0.0)}
    prev = {}
    best_found_real = float('inf')

    while pq:
        est_total, pen_dist, real_dist, stop, route = heapq.heappop(pq)

        state = (stop, route)
        best_pen, _ = best.get(state, (float('inf'), 0))
        if pen_dist > best_pen:
            continue
        if est_total >= best_found_real:
            continue

        if stop == end_id:
            path_edges = []
            cur = (stop, route)
            while cur in prev:
                prev_stop, prev_route, edge = prev[cur]
                path_edges.append(edge)
                cur = (prev_stop, prev_route)
            path_edges.reverse()
            real_total = sum(e['distance'] for e in path_edges if e is not None)
            result = reconstruct_path(prev, start_id, end_id, route, real_total), None
            _cache_put_find(cache_key, result)
            return result

        for edge in adjacency.get(stop, []):
            next_stop = edge['to']
            next_route = edge['route_id']
            new_real = real_dist + edge['distance']
            new_pen = pen_dist + edge['distance']

            if route is not None and next_route != 'transfer' and next_route != route:
                new_pen += CHANGE_PENALTY

            next_state = (next_stop, next_route)
            best_pen, _ = best.get(next_state, (float('inf'), 0))
            if new_pen < best_pen:
                coords = stop_coords.get(next_stop, (0, 0))
                h = haversine_km(coords[0], coords[1], end_coords[0], end_coords[1])
                estimated = new_pen + h
                best[next_state] = (new_pen, new_real)
                prev[next_state] = (stop, route, edge)
                heapq.heappush(pq, (estimated, new_pen, new_real, next_stop, next_route))

    result = None, "Nie znaleziono trasy między tymi przystankami"
    _cache_put_find(cache_key, result)
    return result


def reconstruct_path(prev, start_id, end_id, end_route, total_distance):
    """Reconstruct the path from start to end."""
    path_with_edges = []
    current_state = (end_id, end_route)
    while current_state in prev:
        prev_stop, prev_route, edge = prev[current_state]
        path_with_edges.append((current_state[0], current_state[1], edge))
        current_state = (prev_stop, prev_route)
    path_with_edges.append((start_id, None, None))
    path_with_edges.reverse()

    segments = []
    transfers = []
    current_segment = None
    stop_ids = [p[0] for p in path_with_edges]

    for i, (stop_id, route_id, edge) in enumerate(path_with_edges):
        if i == 0:
            continue

        prev_stop = path_with_edges[i - 1][0]
        prev_route = path_with_edges[i - 1][1]

        if route_id == 'transfer':
            if current_segment is not None:
                segments.append(current_segment)
                current_segment = None
            transfer_from = prev_route if prev_route != 'transfer' else path_with_edges[i - 2][1] if i >= 2 else None
            transfer_to = None
            for j in range(i + 1, len(path_with_edges)):
                if path_with_edges[j][1] != 'transfer':
                    transfer_to = path_with_edges[j][1]
                    break
            if transfer_from and transfer_to and transfer_from != transfer_to:
                if not transfers or transfers[-1]['from_route'] != transfer_from or transfers[-1]['to_route'] != transfer_to:
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
                if prev_route and prev_route != 'transfer' and prev_route != route_id:
                    if not transfers or transfers[-1]['from_route'] != prev_route or transfers[-1]['to_route'] != route_id:
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
            current_segment['stops'].append(stop_id)

    if current_segment is not None:
        segments.append(current_segment)

    # Merge consecutive segments on same route
    merged_segments = []
    i = 0
    while i < len(segments):
        merged = segments[i]
        while (i + 1 < len(segments) and
               segments[i + 1]['route_id'] == merged['route_id']):
            i += 1
            next_seg = segments[i]
            if merged['stops'] and merged['stops'][-1] == next_seg['stops'][0]:
                merged['stops'].extend(next_seg['stops'][1:])
            else:
                merged['stops'].extend(next_seg['stops'])
            merged['distance'] += next_seg['distance']
            merged['end_stop'] = next_seg['end_stop']
        merged_segments.append(merged)
        i += 1
    segments = merged_segments

    for segment in segments:
        if segment['stops'] and len(segment['stops']) >= 2:
            first_group = stop_to_group.get(segment['stops'][0], '')
            second_group = stop_to_group.get(segment['stops'][1], '')
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

    path_stops = []
    last_name = None
    for i, stop_id in enumerate(stop_ids):
        stop_info = stops_by_id.get(stop_id, {})
        route_id = path_with_edges[i][1] if i < len(path_with_edges) else None
        is_transfer = (route_id == 'transfer')
        group_id = stop_to_group.get(stop_id, '')
        group = stops_grouped.get(group_id, {})
        stop_name = group.get('name', stop_info.get('name', ''))

        if is_transfer:
            continue
        if last_name == stop_name:
            continue
        last_name = stop_name

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


# --- Group-to-group route cache ---
_ROUTE_CACHE_MAX = 5000
_route_cache = {}

# Max platforms to try per group (prevents N^2 blowup for stops with many platforms)
_MAX_PLATFORMS_TO_TRY = 3

def find_route_between_groups(from_group_id, to_group_id, mode):
    """Find the best route between two stop groups, with cache."""
    cache_key = (from_group_id, to_group_id, mode)
    cached = _route_cache.get(cache_key)
    if cached is not None:
        return cached

    from_group = stops_grouped.get(from_group_id)
    to_group = stops_grouped.get(to_group_id)

    if not from_group:
        return None, "Przystanek początkowy nie został znaleziony"
    if not to_group:
        return None, "Przystanek końcowy nie został znaleziony"

    if from_group_id == to_group_id:
        cost_reg, cost_red = calculate_cost(0)
        result = ({
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
        }, None)
        if len(_route_cache) < _ROUTE_CACHE_MAX:
            _route_cache[cache_key] = result
        return result

    # Limit platforms to try to prevent N^2 blowup
    from_platforms = from_group['platforms'][:_MAX_PLATFORMS_TO_TRY]
    to_platforms = to_group['platforms'][:_MAX_PLATFORMS_TO_TRY]

    best_result = None
    best_error = None

    for from_platform in from_platforms:
        for to_platform in to_platforms:
            result, error = find_shortest_path(from_platform['id'], to_platform['id'])

            if result is not None:
                if mode == 'convenient':
                    if (best_result is None or
                        len(result['transfers']) < len(best_result['transfers']) or
                        (len(result['transfers']) == len(best_result['transfers']) and
                         result['total_distance'] < best_result['total_distance'])):
                        best_result = result
                else:
                    if best_result is None or result['total_distance'] < best_result['total_distance']:
                        best_result = result
            elif best_result is None:
                best_error = error

    if best_result is None:
        return None, best_error or "Nie znaleziono trasy między tymi przystankami"

    if len(_route_cache) < _ROUTE_CACHE_MAX:
        _route_cache[cache_key] = (best_result, None)
    return best_result, None


# ============================================================
# HTTP Request Handler
# ============================================================

BLOCKED_PREFIXES = ('/.', '/_')
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
    '/favicon.ico',
)

# Pre-computed static JSON responses (cached at startup, both plain and gzip)
_cached_stops_json = None
_cached_routes_json = None
_cached_stops_json_gz = None
_cached_routes_json_gz = None


def html_escape(s):
    """Escape a string for safe insertion into HTML."""
    return html.escape(str(s), quote=True)


# ============================================================
# Rate limiting (simple token bucket per IP)
# ============================================================
_RATE_LIMIT_WINDOW = 10.0      # seconds
_RATE_LIMIT_MAX = 30           # max requests per window per IP
_RATE_LIMIT_EXPENSIVE_MAX = 5  # max expensive requests per window per IP
_rate_limits = {}              # ip -> deque of timestamps
_rate_limits_lock = threading.Lock()


def _rate_limit_ok(ip, expensive=False):
    """Check if a request from this IP is within rate limits."""
    now = time.time()
    with _rate_limits_lock:
        timestamps = _rate_limits.get(ip)
        if timestamps is None:
            timestamps = []
            _rate_limits[ip] = timestamps

        # Remove old timestamps
        while timestamps and timestamps[0] < now - _RATE_LIMIT_WINDOW:
            timestamps.pop(0)

        limit = _RATE_LIMIT_EXPENSIVE_MAX if expensive else _RATE_LIMIT_MAX
        if len(timestamps) >= limit:
            return False

        timestamps.append(now)

        # Prevent unbounded growth of the rate limit dict
        if len(_rate_limits) > 10000:
            # Drop entries that have no recent activity
            for k in [k for k, v in _rate_limits.items() if not v or v[-1] < now - _RATE_LIMIT_WINDOW * 2]:
                del _rate_limits[k]
        return True


class MPKRequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler for MPK Kraków app."""

    server_version = ''
    sys_version = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_HEAD(self):
        # HEAD should not send a body - set a flag and reuse do_GET logic
        self._head_only = True
        self.do_GET()

    def do_POST(self):
        self.send_error_page(405)

    def do_PUT(self):
        self.send_error_page(405)

    def do_DELETE(self):
        self.send_error_page(405)

    def do_PATCH(self):
        self.send_error_page(405)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, HEAD, OPTIONS')
        self.end_headers()

    def _send_body(self, body):
        """Write the response body, unless this is a HEAD request."""
        if not getattr(self, '_head_only', False):
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith('/api/'):
            self.handle_api(path, query)
            return

        # Rate limit static file requests (protects against crawler floods)
        client_ip = self.client_address[0] if self.client_address else 'unknown'
        if not _rate_limit_ok(client_ip):
            self.send_error_page(429)
            return

        if path in BLOCKED_PATHS or any(path.startswith(p) for p in BLOCKED_PREFIXES):
            self.send_error_page(404)
            return

        # Serve the generated favicon.svg (clean, logo embedded inline)
        if path == '/favicon.svg':
            favicon_path = os.path.join(PUBLIC_DIR, 'favicon.svg')
            if os.path.isfile(favicon_path):
                with open(favicon_path, 'r', encoding='utf-8') as f:
                    body = f.read().encode('utf-8')
            else:
                # Fallback: serve cleaned logo content
                body = _logo_svg_content.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self._send_body(body)
            return

        if path == '/' or path == '':
            path = '/index.html'

        file_path = os.path.normpath(os.path.join(PUBLIC_DIR, path.lstrip('/')))
        if not file_path.startswith(PUBLIC_DIR):
            self.send_error_page(403)
            return

        if not os.path.isfile(file_path):
            self.send_error_page(404)
            return

        # Check if this is a crawler
        user_agent = self.headers.get('User-Agent', '').lower()
        is_crawler = any(bot in user_agent for bot in [
            'facebookexternalhit', 'twitterbot', 'linkedinbot', 'slackbot',
            'telegrambot', 'discordbot', 'whatsapp', 'skypeuripreview',
            'applebot', 'bingbot', 'googlebot', 'yandexbot', 'duckduckbot',
            'facebot', 'meta-externalagent'
        ])
        
        from_stop = query.get('from', [''])[0]
        to_stop = query.get('to', [''])[0]
        mode = query.get('mode', ['short'])[0]
        
        if is_crawler and from_stop and to_stop and path in ('/', '', '/index.html'):
            self.serve_modified_html(from_stop, to_stop, mode)
            return

        super().do_GET()
    
    def serve_modified_html(self, from_stop, to_stop, mode):
        """Serve HTML with modified OG tags for crawlers."""
        try:
            file_path = os.path.join(PUBLIC_DIR, 'index.html')
            with open(file_path, 'r', encoding='utf-8') as f:
                html = f.read()
            
            # Validate mode to prevent injection via query string
            if mode not in ('short', 'convenient'):
                mode = 'short'
            
            from_name = "Przystanek początkowy"
            to_name = "Przystanek końcowy"
            
            from_group = stops_grouped.get(from_stop)
            to_group = stops_grouped.get(to_stop)
            
            if from_group:
                from_name = from_group['name']
            if to_group:
                to_name = to_group['name']
            
            # Escape all values inserted into HTML to prevent XSS
            from_name_esc = html_escape(from_name)
            to_name_esc = html_escape(to_name)
            from_stop_esc = html_escape(from_stop)
            to_stop_esc = html_escape(to_stop)
            mode_esc = html_escape(mode)
            
            og_image_url = f"https://zaileprzeja.de/api/og-image?from={from_stop_esc}&to={to_stop_esc}&mode={mode_esc}"
            current_url = f"https://zaileprzeja.de/?from={from_stop_esc}&to={to_stop_esc}&mode={mode_esc}"
            title = f"Za Ile Przejadę? {from_name_esc} → {to_name_esc}"
            description = f"Oblicz koszt przejazdu z {from_name_esc} do {to_name_esc} w nowym systemie biletów MPK Kraków."
            
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
            
            if 'og:site_name' not in html:
                html = html.replace(
                    '<meta property="og:locale"',
                    f'<meta property="og:site_name" content="Za Ile Przejadę?">\n    <meta property="og:locale"'
                )
            
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self._send_body(body)
            
        except Exception:
            super().do_GET()

    def handle_api(self, path, query):
        """Handle API requests."""
        try:
            # Rate limit API requests (expensive endpoints get a stricter limit)
            client_ip = self.client_address[0] if self.client_address else 'unknown'
            expensive = path in ('/api/find-route', '/api/og-image')
            if not _rate_limit_ok(client_ip, expensive=expensive):
                self.serve_json({'error': 'Zbyt wiele zapytań. Spróbuj ponownie za chwilę.'}, status=429)
                return

            if path == '/api/stops':
                self.serve_json_cached(path)

            elif path == '/api/stops/search':
                q = query.get('q', [''])[0].lower().strip()
                if len(q) > 100:
                    q = q[:100]
                if len(q) < 2:
                    self.serve_json([])
                    return
                results = []
                seen = set()
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
                if len(group_id) > 64:
                    group_id = group_id[:64]
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

                if len(from_stop) > 64:
                    from_stop = from_stop[:64]
                if len(to_stop) > 64:
                    to_stop = to_stop[:64]

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
                    distance_str = query.get('distance', ['0'])[0]
                    if len(distance_str) > 32:
                        distance_str = distance_str[:32]
                    distance = float(distance_str)
                except (ValueError, TypeError):
                    self.serve_json({'error': 'Invalid distance parameter'})
                    return
                # Reject NaN/Infinity which could cause invalid results
                if not math.isfinite(distance):
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
                if len(route_id) > 64:
                    route_id = route_id[:64]
                if not route_id:
                    self.serve_json({'error': 'Missing route_id parameter'})
                    return
                shape = route_shapes.get(route_id, [])
                self.serve_json({'route_id': route_id, 'shape': shape})

            elif path == '/api/health':
                self.serve_json({'status': 'ok'})

            elif path == '/api/routes':
                self.serve_json_cached(path)

            elif path == '/api/stop':
                stop_id = query.get('id', [''])[0]
                if len(stop_id) > 64:
                    stop_id = stop_id[:64]
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
                if len(from_stop) > 64:
                    from_stop = from_stop[:64]
                if len(to_stop) > 64:
                    to_stop = to_stop[:64]
                if mode not in ('short', 'convenient'):
                    mode = 'short'
                svg = self.generate_og_image_svg(from_stop, to_stop, mode)
                body = svg.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self._send_body(body)

            else:
                self.serve_json({'error': 'Unknown API endpoint'})

        except Exception as e:
            self.serve_json({'error': 'Internal server error'})

    def _security_headers(self):
        """Add common security headers to a response."""
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-XSS-Protection', '1; mode=block')

    def end_headers(self):
        """Send security headers on ALL responses (including static files)."""
        # Add security headers to every response (static files, API, errors)
        self._security_headers()
        super().end_headers()

    def serve_json(self, data, cache=False, status=200):
        """Serve JSON response with optional gzip compression."""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        
        # Compress with gzip if response > 1KB and client supports it
        accept = self.headers.get('Accept-Encoding', '')
        if len(body) > 1024 and 'gzip' in accept:
            body = gzip.compress(body, compresslevel=6)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
        else:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
        self._send_body(body)

    def serve_json_cached(self, path):
        """Serve pre-computed JSON for static endpoints (gzip pre-compressed)."""
        global _cached_stops_json, _cached_routes_json, _cached_stops_json_gz, _cached_routes_json_gz
        
        if path == '/api/stops':
            if _cached_stops_json is None:
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
                _cached_stops_json = json.dumps(result, ensure_ascii=False).encode('utf-8')
                _cached_stops_json_gz = gzip.compress(_cached_stops_json, compresslevel=6)
            body = _cached_stops_json
            body_gz = _cached_stops_json_gz
        elif path == '/api/routes':
            if _cached_routes_json is None:
                _cached_routes_json = json.dumps(routes_list, ensure_ascii=False).encode('utf-8')
                _cached_routes_json_gz = gzip.compress(_cached_routes_json, compresslevel=6)
            body = _cached_routes_json
            body_gz = _cached_routes_json_gz
        else:
            return
        
        accept = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body_gz)))
            self.send_header('Cache-Control', 'public, max-age=60')
            self.end_headers()
            self._send_body(body_gz)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'public, max-age=60')
            self.end_headers()
            self._send_body(body)

    def send_error_page(self, code):
        """Send a minimal error response."""
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def generate_og_image_svg(self, from_stop_id, to_stop_id, mode):
        """Generate OG image as SVG."""
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
                cost_text = f"{result['cost_regular']:.2f} / {result['cost_reduced']:.2f} zł"

        def esc(s):
            return html.escape(str(s), quote=True)

        def truncate_name(name, max_chars=26):
            """Truncate a stop name with '...' if it exceeds max_chars.
            Truncates at a word boundary for a cleaner result."""
            if len(name) <= max_chars:
                return name
            # Cut at max_chars, then back off to the last space
            cut = name[:max_chars - 3]
            last_space = cut.rfind(' ')
            if last_space > max_chars * 0.5:
                cut = cut[:last_space]
            return cut.rstrip() + '...'

        # Truncate long names so they fit within the image (max ~820px at 55px font)
        from_name = truncate_name(from_name)
        to_name = truncate_name(to_name)

        from_name_esc = esc(from_name)
        to_name_esc = esc(to_name)
        cost_text_esc = esc(cost_text)

        # For names that are still long after truncation, force-fit to 820px width
        # (prevents overflow past the logo area on the right)
        from_fit = ' textLength="820" lengthAdjust="spacingAndGlyphs"' if len(from_name) > 24 else ''
        to_fit = ' textLength="820" lengthAdjust="spacingAndGlyphs"' if len(to_name) > 24 else ''

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630">\n'
            '  <defs>\n'
            '    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">\n'
            '      <stop offset="0%" stop-color="#0d2137"/>\n'
            '      <stop offset="100%" stop-color="#1a3a52"/>\n'
            '    </linearGradient>\n'
            '  </defs>\n'
            '  <rect width="1200" height="630" fill="url(#bg)"/>\n'
            '\n'
            '  <text x="80" y="77" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#5dade2">SKĄD</text>\n'
            f'  <text x="80" y="145" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1"{from_fit}>{from_name_esc}</text>\n'
            '  <text x="80" y="230" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#5dade2">DOKĄD</text>\n'
            f'  <text x="80" y="298" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1"{to_fit}>{to_name_esc}</text>\n'
            '  <line x1="80" y1="346" x2="240" y2="346" stroke="#5dade2" stroke-width="4"/>\n'
            '  <text x="80" y="431" font-family="Arial, Helvetica, sans-serif" font-size="36" font-weight="normal" fill="#5dade2">CENA BILETU</text>\n'
            f'  <text x="80" y="548" font-family="Arial, Helvetica, sans-serif" font-size="120" font-weight="bold" fill="#ffffff">{cost_text_esc}</text>\n'
            f'  <g transform="translate(930,400) scale(2.5)">\n'
        )
        # Extract inner elements from logo.svg (strip <svg> wrapper)
        import re
        logo_inner = re.sub(r'<svg[^>]*>', '', _logo_svg_content, count=1)
        logo_inner = re.sub(r'</svg>', '', logo_inner)
        svg += f'  {logo_inner.strip()}\n'
        svg += '  </g>\n'
        svg += (
            '  <text x="1010" y="577" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#7f8c8d" text-anchor="middle">zaileprzeja.de</text>\n'
            '</svg>'
        )
        return svg

    def log_message(self, format, *args):
        """Log only errors and API requests."""
        msg = format % args
        if '/api/' in msg or 'Error' in msg or 'error' in msg:
            import sys
            print(f"  {msg}", file=sys.stderr)


def main():
    port = int(os.environ.get('PORT', 8080))
    
    server = ThreadedHTTPServer(('0.0.0.0', port), MPKRequestHandler)
    
    # Warmup cache in background thread (non-blocking)
    import threading
    threading.Thread(target=_warmup_cache, daemon=True).start()
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
    print(f"  /api/health - Health check")
    print(f"\nOptimizations: threading, route cache ({_FIND_CACHE_MAX} pathfinding + {_ROUTE_CACHE_MAX} routes), gzip compression")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.shutdown()


if __name__ == '__main__':
    main()