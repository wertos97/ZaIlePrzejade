"""A* pathfinding for MPK Kraków route planner.

Provides shortest-path search between individual stops and between
stop groups (platforms sharing a name/location). Results are cached
with bounded memory usage.

Key differences from the original server.py implementation:
  - find_route_between_groups returns BOTH short and convenient results
    in a single pass through platform pairs (dual-mode).
  - A* has a 30-second timeout (time check + max iterations).
  - _extract_shape_segment properly handles reverse-direction shapes.
"""

import atexit
import heapq
import json
import math
import os
import threading
import time

from .cost import (
    calculate_cost,
    calculate_route_cost,
    MAX_DAILY_COST_REGULAR,
    MAX_DAILY_COST_REDUCED,
)

# ============================================================
# Module globals — set once by init_pathfinding()
# ============================================================

adjacency: dict = {}
stops_by_id: dict = {}
stops_grouped: dict = {}
stop_to_group: dict = {}
routes_by_id: dict = {}
route_shapes: dict = {}
stop_coords: dict = {}

# Derived: stop_pair_routes built from adjacency during init
stop_pair_routes: dict = {}

# Transfer time added per transfer (5 minutes, in seconds)
TRANSFER_TIME_SECONDS = 300


# ============================================================
# Initialisation
# ============================================================

def init_pathfinding(adj, stops_by_id_ref, stops_grouped_ref,
                     stop_to_group_ref, routes_by_id_ref, route_shapes_ref):
    """Set module-level references from the data module.

    Must be called once before any pathfinding functions are used.
    Builds stop_coords and stop_pair_routes from the adjacency list.
    """
    global adjacency, stops_by_id, stops_grouped, stop_to_group
    global routes_by_id, route_shapes, stop_coords, stop_pair_routes

    adjacency = adj
    stops_by_id = stops_by_id_ref
    stops_grouped = stops_grouped_ref
    stop_to_group = stop_to_group_ref
    routes_by_id = routes_by_id_ref
    route_shapes = route_shapes_ref

    # Disk cache: restore previously computed routes on restart and save the
    # route cache periodically + at exit. A background flusher (every 5 min,
    # only when entries changed) keeps the disk copy fresh even if the
    # process is killed with SIGKILL/SIGTERM (atexit doesn't run then).
    global _feed_version
    try:
        from . import data as _data
        _feed_version = _data.feed_metadata.get('version', '')
    except Exception:
        _feed_version = ''
    loaded = _load_route_cache()
    atexit.register(_save_route_cache)

    def _flush_loop():
        last_sig = None
        last_count = 0
        while True:
            time.sleep(120)
            with _route_cache_lock:
                sig = (len(_route_cache), _route_cache_bytes)
            # flush: every 2 min or when 30+ entries were added since last
            if len(_route_cache) - last_count >= 30:
                last_count = len(_route_cache)
                _save_route_cache()
                last_sig = sig
            elif sig != last_sig and len(_route_cache) > 0:
                last_sig = sig
                last_count = len(_route_cache)
                _save_route_cache()

    threading.Thread(target=_flush_loop, daemon=True).start()

    # startup log line
    print(f"  Route cache: {len(_route_cache)} entries "
          f"({loaded} loaded from disk, "
          f"{_route_cache_bytes // 1024} KB)")

    # Build coordinate lookup from adjacency + stops_by_id
    stop_coords = {}
    for stop_id in adjacency:
        stop_info = stops_by_id.get(stop_id, {})
        stop_coords[stop_id] = (stop_info.get('lat', 0), stop_info.get('lon', 0))

    # Build stop-pair route lookup from adjacency edges
    stop_pair_routes = {}
    for stop_id, edges in adjacency.items():
        for edge in edges:
            if edge['route_id'] != 'transfer':
                key = (stop_id, edge['to'])
                if key not in stop_pair_routes:
                    stop_pair_routes[key] = []
                if edge['route_id'] not in stop_pair_routes[key]:
                    stop_pair_routes[key].append(edge['route_id'])


# ============================================================
# Geometry helpers
# ============================================================

def haversine_km(lat1, lon1, lat2, lon2):
    """Approximate distance in km between two coordinates."""
    dlat = (lat1 - lat2) * 111.32
    dlon = (lon1 - lon2) * 111.32 * math.cos((lat1 + lat2) / 2 * math.pi / 180)
    return math.sqrt(dlat * dlat + dlon * dlon)


def _nearest_shape_index(shape_points, lat, lon, start_idx, end_idx):
    """Find the index in shape_points (within [start_idx, end_idx]) closest to (lat, lon)."""
    best_idx = start_idx
    best_dist = float('inf')
    for i in range(start_idx, end_idx):
        d = haversine_km(shape_points[i][0], shape_points[i][1], lat, lon)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


# ============================================================
# Pathfinding cache (bounded by size, thread-safe via GIL)
# ============================================================

_FIND_CACHE_MAX = 10000
_FIND_CACHE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB max
_find_cache: dict = {}
_find_cache_bytes: int = 0


def _cache_put_find(key, value):
    """Store a value in the find cache, evicting oldest entries if over size limit."""
    global _find_cache_bytes
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


# ============================================================
# A* shortest path
# ============================================================

# A* safety limits
_ASTAR_MAX_ITERATIONS = 200000
_ASTAR_TIMEOUT_SECONDS = 30

# Cancellation event for cooperative cancellation of long-running searches
_search_cancel_event = threading.Event()


def _check_cancelled():
    """Check if search was cancelled. Returns True if cancelled."""
    return _search_cancel_event.is_set()


def cancel_all_searches():
    """Signal all in-progress searches to cancel."""
    _search_cancel_event.set()


def reset_cancel_flag():
    """Reset the cancellation flag for new searches."""
    _search_cancel_event.clear()


def find_shortest_path(start_id, end_id):
    """A* shortest path between two individual stops.

    Uses penalty-weighted distance (penalises route changes) as the
    primary cost and haversine as the heuristic. Results are cached.
    Times out after 30 seconds or 200 000 iterations.

    Returns (result_dict, None) on success or (None, error_string) on
    failure.
    """
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
    h_start = haversine_km(start_coords[0], start_coords[1],
                           end_coords[0], end_coords[1])
    pq = [(h_start, 0.0, 0.0, 0, start_id, None)]
    best = {(start_id, None): (0.0, 0.0)}
    prev = {}
    best_found_real = float('inf')
    seq = 0

    start_time = time.monotonic()

    while pq:
        # Cooperative cancellation check
        if _check_cancelled():
            result = None, "Anulowano: wyszukiwanie przerwane"
            _cache_put_find(cache_key, result)
            return result

        est_total, pen_dist, real_dist, _, stop, route = heapq.heappop(pq)

        state = (stop, route)
        best_pen, _ = best.get(state, (float('inf'), 0))
        if pen_dist > best_pen:
            continue
        if est_total >= best_found_real:
            continue

        # Timeout / iteration limit check (every 1000 pops is cheap)
        iterations = len(best)
        if iterations % 1000 == 0:
            if time.monotonic() - start_time > _ASTAR_TIMEOUT_SECONDS:
                result = None, "Timeout: nie znaleziono trasy w wymaganym czasie"
                _cache_put_find(cache_key, result)
                return result

        if iterations >= _ASTAR_MAX_ITERATIONS:
            result = None, "Przekroczono limit iteracji A*"
            _cache_put_find(cache_key, result)
            return result

        if stop == end_id:
            result = reconstruct_path(prev, start_id, end_id, route, real_dist), None
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
                h = haversine_km(coords[0], coords[1],
                                 end_coords[0], end_coords[1])
                estimated = new_pen + h
                best[next_state] = (new_pen, new_real)
                prev[next_state] = (stop, route, edge)
                seq += 1
                heapq.heappush(pq, (estimated, new_pen, new_real, seq,
                                    next_stop, next_route))

    result = None, "Nie znaleziono trasy między tymi przystankami"
    _cache_put_find(cache_key, result)
    return result


# ============================================================
# Fare-based A* (cheapest route)
# ============================================================

# Ticket price changes at these segment distances (km):
# 0..3.5 -> base, then every 0.5 km up to the 9.00 zł cap at 8.5 km.
_PRICE_BOUNDS = [3.5 + 0.5 * k for k in range(11)]  # 3.5 .. 8.5
_cheap_search_count = 0
_cheap_timeout_count = 0
# Global CPU gate: the fare A* runs under the GIL on a single core, so
# parallel searches would thrash. At most 2 run at once (sync + viz jobs
# share this gate); the rest queue briefly.
_CHEAP_SEARCH_GATE = threading.Semaphore(2)

# Above 8.5 km a segment costs the max 9.00 zł — riding further is free,
# so all accumulated distances beyond the cap are equivalent.
_ACC_CAP = 8.5

# price lookup: index = int(km * 100), capped at 20 km (well beyond _ACC_CAP)
_PRICE_LOOKUP = tuple(
    calculate_cost(min(i / 100.0, 20.0))[0] for i in range(2001)
)


def _ticket_price(acc_km):
    """Price of the CURRENT segment accumulated to acc_km (regular fare)."""
    return _PRICE_LOOKUP[min(int(acc_km * 100 + 0.5), 2000)]


def _dominates(closed_a, acc_a, closed_b, acc_b):
    """Exact dominance on the (stop, route) frontier.

    The ticket price is non-decreasing in the segment distance, so a state
    with lower closed cost AND lower accumulated distance is never worse:
    for any continuation X of the current segment,
        closed_a + price(acc_a + X) <= closed_b + price(acc_b + X)
    holds whenever closed_a <= closed_b and acc_a <= acc_b.
    """
    return closed_a <= closed_b + 1e-9 and acc_a <= acc_b + 1e-9


def _cheap_heuristic(acc, remaining_km):
    """Admissible lower bound on the remaining fare.

    Remaining travel covers at least `remaining_km` (straight line). The
    cheapest way is to continue the current segment for x km and pay for the
    rest with new segments: price(acc+x) - price(acc) + price(remaining-x).
    Minimised over x at price breakpoints -> exact lower bound.
    """
    if remaining_km <= 0:
        return 0.0
    xs = {0.0, remaining_km}
    for b in _PRICE_BOUNDS:
        xs.add(b - acc)
        xs.add(remaining_km - b)
    xs = sorted(x for x in xs if x >= 0 and x <= remaining_km)
    best = float('inf')
    # step function: check jump points AND segment midpoints (min may lie
    # strictly between jumps)
    for i in range(len(xs) - 1):
        for x in (xs[i], (xs[i] + xs[i + 1]) / 2):
            cost = (_ticket_price(acc + x) - _ticket_price(acc)
                    + _ticket_price(remaining_km - x))
            best = min(best, cost)
    return best


def find_cheapest_path(start_ids, end_ids, upper_bound=float('inf')):
    """A* for the CHEAPEST (fare-minimising) path between stop sets.

    State: (stop, route, acc) where acc is the distance accumulated in the
    current ticket segment. Moving along the same route extends the segment;
    transfers / direct route changes close it (each segment is a separate
    ticket priced from zero). The (stop, route) frontier keeps a Pareto set
    of (closed_cost, acc) with exact dominance, so the search is optimal.

    start_ids / end_ids: lists of individual stop (platform) ids — the search
    runs from all starts at once and stops at the FIRST reachable end
    (multi-source A*, one search instead of one per platform pair).

    upper_bound: known fare of some path (e.g. the shortest route) — states
    whose partial cost already reaches it are pruned (optimality preserved:
    any completion would cost >= partial cost).

    Returns (result_dict, None) or (None, error_string); cached.
    """
    global _cheap_search_count, _cheap_timeout_count
    cache_key = ('cheap', tuple(start_ids), tuple(end_ids))
    cached = _cache_get_find(cache_key)
    if cached is not None:
        return cached
    _cheap_search_count += 1

    if not start_ids or not end_ids:
        result = None, "Nie podano przystanków"
        _cache_put_find(cache_key, result)
        return result

    # CPU gate: at most 2 fare searches run concurrently (GIL).
    _CHEAP_SEARCH_GATE.acquire()
    try:
        return _find_cheapest_path_gated(start_ids, end_ids, cache_key,
                                         upper_bound, max_seconds=6.0)
    finally:
        _CHEAP_SEARCH_GATE.release()


def _find_cheapest_path_gated(start_ids, end_ids, cache_key, upper_bound,
                              max_seconds=6.0):
    """Body of find_cheapest_path, executed under the CPU gate.

    max_seconds caps the wall time: beyond that the search gives up with a
    "not found" so the caller falls back to the short route. The full 30 s
    budget is only useful for pathological pairs; most fares are found in
    < 5 s and the cap keeps the UI responsive during bursts.
    """
    global _cheap_timeout_count
    end_set = set(end_ids)
    end_coords = stop_coords.get(end_ids[0], (0, 0))

    # pq entries: (f, closed, acc, seq, stop, route, parent_key)
    pq = []
    seq = 0
    for start_id in start_ids:
        if start_id not in adjacency:
            continue
        start_coords = stop_coords.get(start_id, (0, 0))
        h_start = _cheap_heuristic(
            0.0, haversine_km(start_coords[0], start_coords[1],
                              end_coords[0], end_coords[1]))
        heapq.heappush(pq, (h_start, 0.0, 0.0, seq, start_id, None, None))
        seq += 1
    if not pq:
        result = None, "Przystanek początkowy nie został znaleziony w grafie"
        _cache_put_find(cache_key, result)
        return result

    frontier = {}  # (stop, route) -> [(closed, acc)] — only SETTLED states
    prev = {}  # (stop, route, acc) -> (parent_stop, parent_route, parent_acc, edge)
    start_time = time.monotonic()
    iterations = 0

    while pq:
        # Cooperative cancellation check
        if _check_cancelled():
            result = None, "Anulowano: wyszukiwanie przerwane"
            _cache_put_find(cache_key, result)
            return result

        f_val, closed, acc, _, stop, route, parent = heapq.heappop(pq)

        # Prune states that already cost MORE than a known full route
        # (states with cost == bound may still be the optimum itself).
        if closed + _ticket_price(acc) > upper_bound + 1e-9:
            continue

        # Dominance: skip states dominated by an already-settled one at the
        # same node. (frontier holds settled states, so no self-comparison)
        states = frontier.get((stop, route))
        if states is not None:
            dominated = False
            for (c2, a2) in states:
                if _dominates(c2, a2, closed, acc):
                    dominated = True
                    break
            if dominated:
                continue

        # Settle: record the parent chain and add to the frontier,
        # Pareto-pruning entries dominated by the new one.
        state_key = (stop, route, acc)
        if parent is not None:
            prev[state_key] = parent
        if states is None:
            frontier[(stop, route)] = [(closed, acc)]
        else:
            pruned = [(c, a) for (c, a) in states
                      if not _dominates(closed, acc, c, a)]
            pruned.append((closed, acc))
            frontier[(stop, route)] = pruned

        iterations += 1

        # Timeout / iteration limits: hard wall-clock cap keeps the UI
        # responsive (caller falls back to the short route).
        if iterations % 1000 == 0:
            if time.monotonic() - start_time > max_seconds:
                _cheap_timeout_count += 1
                result = None, "Timeout: nie znaleziono trasy w wymaganym czasie"
                _cache_put_find(cache_key, result)
                return result
        if iterations >= _ASTAR_MAX_ITERATIONS:
            _cheap_timeout_count += 1
            result = None, "Przekroczono limit iteracji A*"
            _cache_put_find(cache_key, result)
            return result

        if stop in end_set:
            # Close the current segment: total = closed + price(acc).
            path_with_edges = []
            current = (stop, route, acc)
            while current in prev:
                prev_stop, prev_route, prev_acc, edge = prev[current]
                path_with_edges.append((current[0], current[1], edge))
                current = (prev_stop, prev_route, prev_acc)
            path_with_edges.append((current[0], None, None))
            path_with_edges.reverse()
            result = _build_route_result(path_with_edges), None
            _cache_put_find(cache_key, result)
            return result

        for edge in adjacency.get(stop, []):
            next_stop = edge['to']
            next_route = edge['route_id']
            dist = edge['distance']

            if next_route == 'transfer':
                # Walking transfer: close the current segment, open none.
                new_closed = closed + _ticket_price(acc)
                new_acc = 0.0
            elif route is None or route == 'transfer' or next_route != route:
                # Boarding / direct route change: new segment priced from 0.
                new_closed = closed + _ticket_price(acc)  # close old (0 if none)
                new_acc = dist
            else:
                # Continue the same ride: extend the segment.
                new_closed = closed
                new_acc = acc + dist

            new_acc = min(new_acc, _ACC_CAP)  # beyond cap riding is free
            new_acc = round(new_acc * 20) / 20  # 0.05 km grid (<< 0.5 km price step)
            g = new_closed + _ticket_price(new_acc)
            if g > upper_bound + 1e-9:
                continue

            # Pareto pruning at the destination node against settled states.
            states = frontier.get((next_stop, next_route))
            if states is not None:
                dominated = False
                for (c2, a2) in states:
                    if _dominates(c2, a2, new_closed, new_acc):
                        dominated = True
                        break
                if dominated:
                    continue

            coords = stop_coords.get(next_stop, (0, 0))
            h = _cheap_heuristic(
                new_acc, haversine_km(coords[0], coords[1],
                                      end_coords[0], end_coords[1]))
            parent_key = (stop, route, acc, edge)
            seq += 1
            heapq.heappush(pq, (g + h, new_closed, new_acc, seq,
                                next_stop, next_route, parent_key))

    # No strictly cheaper-than-bound path found -> let the caller fall back.
    result = None, "Nie znaleziono trasy między tymi przystankami"
    _cache_put_find(cache_key, result)
    return result


# ============================================================
# Path reconstruction
# ============================================================

def reconstruct_path(prev, start_id, end_id, end_route, total_distance):
    """Reconstruct the path from start to end.

    Walks the prev map, builds segments and transfers, merges
    consecutive segments on the same route, attaches alternative
    route info, shapes, distances and costs.
    """
    path_with_edges = []
    current_state = (end_id, end_route)
    while current_state in prev:
        prev_stop, prev_route, edge = prev[current_state]
        path_with_edges.append((current_state[0], current_state[1], edge))
        current_state = (prev_stop, prev_route)
    path_with_edges.append((start_id, None, None))
    path_with_edges.reverse()
    return _build_route_result(path_with_edges)


def _build_route_result(path_with_edges):
    """Build the full route result dict from a path of (stop_id, route_id, edge).

    Shared by the distance-based (short/convenient) and fare-based (cheap)
    search modes — segments, transfers, shapes, distances and costs are
    derived from the actual path edges.
    """
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
            transfer_from = (prev_route if prev_route != 'transfer'
                             else (path_with_edges[i - 2][1] if i >= 2 else None))
            transfer_to = None
            for j in range(i + 1, len(path_with_edges)):
                if path_with_edges[j][1] != 'transfer':
                    transfer_to = path_with_edges[j][1]
                    break
            if transfer_from and transfer_to and transfer_from != transfer_to:
                if (not transfers
                        or transfers[-1]['from_route'] != transfer_from
                        or transfers[-1]['to_route'] != transfer_to):
                    group_id = stop_to_group.get(stop_id, '')
                    group = stops_grouped.get(group_id, {})
                    stop_name = group.get(
                        'name', stops_by_id.get(stop_id, {}).get('name', ''))
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
                    'time': 0,
                    'stops': [prev_stop],
                    'end_stop': stop_id,
                }

            if current_segment['route_id'] != route_id:
                if (prev_route and prev_route != 'transfer'
                        and prev_route != route_id):
                    if (not transfers
                            or transfers[-1]['from_route'] != prev_route
                            or transfers[-1]['to_route'] != route_id):
                        group_id = stop_to_group.get(prev_stop, '')
                        group = stops_grouped.get(group_id, {})
                        stop_name = group.get(
                            'name',
                            stops_by_id.get(prev_stop, {}).get('name', ''))
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
                    'time': 0,
                    'stops': [prev_stop],
                    'end_stop': stop_id,
                }

            current_segment['end_stop'] = stop_id
            current_segment['stops'].append(stop_id)
            if edge and edge.get('time') is not None:
                current_segment['time'] += edge['time']
            if edge:
                # Real GTFS distance for this hop (shape_dist_traveled-based,
                # computed in process_gtfs.py; haversine only as fallback when
                # shape_dist is missing).
                current_segment['distance'] += edge['distance']

    if current_segment is not None:
        segments.append(current_segment)

    # Merge consecutive segments on same route
    merged_segments = []
    i = 0
    while i < len(segments):
        merged = segments[i]
        while (i + 1 < len(segments)
               and segments[i + 1]['route_id'] == merged['route_id']):
            i += 1
            next_seg = segments[i]
            if merged['stops'] and merged['stops'][-1] == next_seg['stops'][0]:
                merged['stops'].extend(next_seg['stops'][1:])
            else:
                merged['stops'].extend(next_seg['stops'])
            merged['distance'] += next_seg['distance']
            merged['time'] += next_seg.get('time', 0)
            merged['end_stop'] = next_seg['end_stop']
        merged_segments.append(merged)
        i += 1
    segments = merged_segments

    # Attach alternative routes, names, distances, costs and shapes
    for segment in segments:
        if segment['stops'] and len(segment['stops']) >= 2:
            first_group = stop_to_group.get(segment['stops'][0], '')
            second_group = stop_to_group.get(segment['stops'][1], '')
            first_pair_routes = set()
            for p1 in stops_grouped.get(first_group, {}).get('platforms', []):
                for p2 in stops_grouped.get(second_group, {}).get('platforms', []):
                    routes = stop_pair_routes.get((p1['id'], p2['id']), [])
                    first_pair_routes.update(routes)

            # Only keep routes that actually serve this segment's mode
            seg_mode = segment.get('mode')
            if seg_mode:
                filtered_routes = set()
                for rid in first_pair_routes:
                    r_info = routes_by_id.get(rid, {})
                    if r_info.get('mode') == seg_mode:
                        filtered_routes.add(rid)
                first_pair_routes = filtered_routes

            if first_pair_routes:
                segment['all_routes'] = sorted(
                    first_pair_routes,
                    key=lambda rid: routes_by_id.get(
                        rid, {}).get('short_name', rid))
            else:
                segment['all_routes'] = (
                    [segment['route_id']] if segment['route_id'] else [])
        else:
            segment['all_routes'] = (
                [segment['route_id']] if segment['route_id'] else [])

        first_stop_id = segment['stops'][0] if segment['stops'] else None
        last_stop_id = (segment['stops'][-1] if segment['stops'] else None)
        if first_stop_id:
            group_id = stop_to_group.get(first_stop_id, '')
            group = stops_grouped.get(group_id, {})
            segment['first_stop_name'] = group.get(
                'name',
                stops_by_id.get(first_stop_id, {}).get(
                    'name', first_stop_id))
        if last_stop_id:
            group_id = stop_to_group.get(last_stop_id, '')
            group = stops_grouped.get(group_id, {})
            segment['last_stop_name'] = group.get(
                'name',
                stops_by_id.get(last_stop_id, {}).get(
                    'name', last_stop_id))

    # Build path_stops: ONE entry per visited stop group, positioned at the
    # REAL platform used on the route (last occurrence in the group wins —
    # that is the peron you actually board/alight at). The boarding peron
    # (first stop) is always kept so the start marker is correct.
    path_stops = []
    path_idx_by_group = {}  # group_id -> index in path_stops
    for i, stop_id in enumerate(stop_ids):
        stop_info = stops_by_id.get(stop_id, {})
        route_id = path_with_edges[i][1] if i < len(path_with_edges) else None
        is_transfer = (route_id == 'transfer')
        group_id = stop_to_group.get(stop_id, '')
        group = stops_grouped.get(group_id, {})
        stop_name = group.get('name', stop_info.get('name', ''))

        if is_transfer:
            continue

        # Route display must use the REAL platform location, not the averaged
        # group position — otherwise stops straddling a junction appear in the
        # middle of the road. The averaged position is only for overview mode.
        lat = stop_info.get('lat', group.get('lat', 0))
        lon = stop_info.get('lon', group.get('lon', 0))

        entry = {
            'stop_id': stop_id,
            'group_id': group_id,
            'name': stop_name,
            'lat': lat,
            'lon': lon,
            'code': stop_info.get('code', ''),
            'mode': stop_info.get('mode', ''),
            'route_id': route_id,
            'is_transfer': is_transfer,
        }

        existing = path_idx_by_group.get(group_id)
        is_last_stop = (i == len(stop_ids) - 1)
        if i == 0:
            # Boarding peron: keep its own entry (start marker must be exact).
            path_idx_by_group[group_id] = len(path_stops)
            path_stops.append(entry)
        elif existing is None:
            path_idx_by_group[group_id] = len(path_stops)
            path_stops.append(entry)
        elif is_last_stop:
            # Alighting peron: last occurrence wins so the end marker is exact.
            path_stops[existing] = entry
        elif existing != 0:
            # Middle occurrence: last visited peron in the group wins.
            path_stops[existing] = entry

    # Compute segment costs and attach shapes.
    # segment['distance'] was accumulated from real GTFS edge distances
    # (shape_dist_traveled) while walking the path — round it for output.
    for segment in segments:
        segment['distance'] = round(segment['distance'], 4)

        seg_reg, seg_red = calculate_cost(segment.get('distance', 0.0))
        segment['cost_regular'] = seg_reg
        segment['cost_reduced'] = seg_red

        segment['shape'] = _extract_shape_segment(
            segment['route_id'], segment['stops'])

        # Exact peron coordinates for EVERY platform in this segment — the
        # path list is deduplicated per stop group, so segments carry their
        # own geometry lookup for lines and labels.
        segment['stop_positions'] = {}
        for sid in segment['stops']:
            s = stops_by_id.get(sid, {})
            segment['stop_positions'][sid] = [
                s.get('lat', 0), s.get('lon', 0),
            ]

    # Total distance: sum of real (GTFS) segment distances — NOT straight-line
    total_distance = sum(seg.get('distance', 0.0) for seg in segments)
    total_distance = round(total_distance, 4)

    cost_regular, cost_reduced = calculate_route_cost(segments)

    total_time = sum(seg.get('time', 0) for seg in segments)
    total_time += TRANSFER_TIME_SECONDS * len(transfers)

    return {
        'total_distance': total_distance,
        'total_time': total_time,
        'cost_regular': cost_regular,
        'cost_reduced': cost_reduced,
        'max_daily_cost_regular': MAX_DAILY_COST_REGULAR,
        'max_daily_cost_reduced': MAX_DAILY_COST_REDUCED,
        'path': path_stops,
        'segments': segments,
        'transfers': transfers,
    }


# ============================================================
# Shape extraction
# ============================================================

def _extract_shape_segment(route_id, stops):
    """Extract the portion of a route's shape between the first and last stop.

    Handles both forward and reverse-direction shapes by checking which
    end of the shape is nearer to the first stop and which to the last.
    Returns a list of [lat, lon] pairs, or [] if no shape is found.
    """
    shape_points = route_shapes.get(route_id)
    if not shape_points or len(stops) < 2:
        return []

    first_id = stops[0]
    last_id = stops[-1]
    first_stop = stops_by_id.get(first_id, {})
    last_stop = stops_by_id.get(last_id, {})
    if not first_stop or not last_stop:
        return []
    f_lat = first_stop.get('lat')
    f_lon = first_stop.get('lon')
    l_lat = last_stop.get('lat')
    l_lon = last_stop.get('lon')
    if f_lat is None or l_lat is None:
        return []

    n = len(shape_points)

    # Check both directions and pick the one that gives a coherent slice.
    # Forward:  i0 near first_stop, i1 near last_stop,  i0 < i1
    # Reverse:  i0 near last_stop,  i1 near first_stop, i0 < i1
    fwd_i0 = _nearest_shape_index(shape_points, f_lat, f_lon, 0, n)
    fwd_i1 = _nearest_shape_index(shape_points, l_lat, l_lon, 0, n)

    rev_i0 = _nearest_shape_index(shape_points, l_lat, l_lon, 0, n)
    rev_i1 = _nearest_shape_index(shape_points, f_lat, f_lon, 0, n)

    # For each candidate pair, ensure i0 < i1 (normalise)
    if fwd_i1 < fwd_i0:
        fwd_i0, fwd_i1 = fwd_i1, fwd_i0
    if rev_i1 < rev_i0:
        rev_i0, rev_i1 = rev_i1, rev_i0

    # Prefer the direction that gives the longer slice (more context)
    fwd_len = fwd_i1 - fwd_i0
    rev_len = rev_i1 - rev_i0

    if fwd_len >= rev_len:
        i0, i1 = fwd_i0, fwd_i1
    else:
        i0, i1 = rev_i0, rev_i1

    if i1 - i0 < 2:
        if n >= 2:
            return [list(p) for p in shape_points]
        return []

    return [list(p) for p in shape_points[i0:i1 + 1]]


# ============================================================
# Group-to-group route finding (dual-mode)
# ============================================================

_ROUTE_CACHE_MAX = 3000
_ROUTE_CACHE_MAX_BYTES = 24 * 1024 * 1024  # 24 MB total RAM budget
_route_cache: dict = {}
_route_cache_bytes = 0
_route_cache_lock = threading.Lock()
_feed_version = ''
# In-flight searches, keyed by (from_group_id, to_group_id): while a request
# is computing a pair, concurrent identical requests wait for its result
# instead of re-running the expensive A* (10× the same pair = 1 search).
_route_inflight = {}

# Disk persistence: on shutdown the route cache is written to
# processed/route_cache_<feed_version>.json so a restart starts warm.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'processed')


def _cache_file_path():
    ver = str(_feed_version) if _feed_version else 'unknown'
    return os.path.join(_CACHE_DIR, f'route_cache_{ver}.json')


def _save_route_cache():
    """Write the route cache to disk (best-effort, at exit)."""
    global _route_cache, _route_cache_bytes
    try:
        with _route_cache_lock:
            data = {f"{k[0]}|{k[1]}|{k[2]}": v[0]
                    for k, v in _route_cache.items()}
        with open(_cache_file_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _load_route_cache():
    """Load a previously saved route cache, if the feed version matches."""
    global _route_cache, _route_cache_bytes
    try:
        with open(_cache_file_path(), encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return 0
    with _route_cache_lock:
        for key, triple in data.items():
            from_id, to_id, mode = key.split('|')
            ck = (from_id, to_id, mode)
            # JSON deserialises tuples as lists — restore the dual tuple
            # structure used in memory: ((result, error), ...) x3.
            triple = tuple(tuple(pair) for pair in triple)
            size = len(json.dumps(triple, ensure_ascii=False))
            if size > _ROUTE_CACHE_MAX_BYTES:
                continue
            _route_cache[ck] = (triple, size)
            _route_cache_bytes += size
        while _route_cache_bytes > _ROUTE_CACHE_MAX_BYTES and _route_cache:
            oldest = next(iter(_route_cache))
            _route_cache_bytes -= _route_cache[oldest][1]
            del _route_cache[oldest]
    return len(data)

# Max platforms to try per group (prevents N^2 blowup for stops with
# many platforms)
_MAX_PLATFORMS_TO_TRY = 3


def find_route_between_groups(from_group_id, to_group_id, mode='both'):
    """Find the best route between two stop groups.

    Iterates over platform pairs once and tracks three results
    simultaneously:

    * ``'short'``        — minimum distance
    * ``'convenient'``   — fewest transfers
    * ``'cheap'``        — minimum ticket fare (each ride is a separate
      ticket priced from zero, so the fare is NOT proportional to distance)

    The *mode* parameter selects what is returned:

    * ``'short'`` / ``'convenient'`` / ``'cheap'`` — only that result
    * ``'both'`` (default) — a 3-tuple ``(short, convenient, cheap)``

    Each result is itself a ``(result_dict, error_string)`` pair.

    Results are cached per (from_group_id, to_group_id, mode, feed_version).
    """
    cache_key = (from_group_id, to_group_id, mode, _feed_version)
    with _route_cache_lock:
        cached = _route_cache.get(cache_key)
        if cached is not None:
            return _slice_route_cache(cached[0], mode)
        inflight = _route_inflight.get((from_group_id, to_group_id))

    # Another request is computing this exact pair — wait for its result
    # (keeps 10 concurrent identical queries down to a single A* run).
    if inflight is not None:
        with inflight['cond']:
            while not inflight['done']:
                inflight['cond'].wait(timeout=30.0)
        with _route_cache_lock:
            cached = _route_cache.get(cache_key)
        if cached is not None:
            return _slice_route_cache(cached[0], mode)
        # timed out or lost — fall through and compute ourselves

    # Register as the in-flight search for this pair (dedup for concurrent
    # identical requests), compute, then store + notify waiters.
    pair_key = (from_group_id, to_group_id)
    inflight = {'cond': threading.Condition(), 'done': False}
    with _route_cache_lock:
        _route_inflight[pair_key] = inflight
    try:
        return _compute_route_internal(cache_key, from_group_id, to_group_id,
                                       mode)
    finally:
        with _route_cache_lock:
            inflight['done'] = True
            _route_inflight.pop(pair_key, None)
        with inflight['cond']:
            inflight['cond'].notify_all()


def _compute_route_internal(cache_key, from_group_id, to_group_id, mode):
    """Compute the triple-mode result for a group pair (no cache/dedup)."""
    from_group = stops_grouped.get(from_group_id)
    to_group = stops_grouped.get(to_group_id)

    if not from_group:
        err = ((None, "Przystanek początkowy nie został znaleziony"),
               (None, "Przystanek początkowy nie został znaleziony"),
               (None, "Przystanek początkowy nie został znaleziony"))
        return _cache_route(cache_key, err, mode)
    if not to_group:
        err = ((None, "Przystanek końcowy nie został znaleziony"),
               (None, "Przystanek końcowy nie został znaleziony"),
               (None, "Przystanek końcowy nie został znaleziony"))
        return _cache_route(cache_key, err, mode)

    # Same group — zero-distance trip
    if from_group_id == to_group_id:
        cost_reg, cost_red = calculate_cost(0)
        result = ({
            'total_distance': 0,
            'total_time': 0,
            'cost_regular': cost_reg,
            'cost_reduced': cost_red,
            'max_daily_cost_regular': MAX_DAILY_COST_REGULAR,
            'max_daily_cost_reduced': MAX_DAILY_COST_REDUCED,
            'path': [{
                'stop_id': from_group['platforms'][0]['id'],
                'group_id': from_group_id,
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
        return _cache_route(cache_key, (result, result, result), mode)

    # Limit platforms to try per group
    from_platforms = from_group['platforms'][:_MAX_PLATFORMS_TO_TRY]
    to_platforms = to_group['platforms'][:_MAX_PLATFORMS_TO_TRY]

    best_short = None
    best_convenient = None
    best_cheap = None
    last_error = None

    for from_platform in from_platforms:
        for to_platform in to_platforms:
            # Distance-based search (feeds short + convenient)
            result, error = find_shortest_path(
                from_platform['id'], to_platform['id'])

            if result is not None:
                n_transfers = len(result.get('transfers', []))
                dist = result.get('total_distance', float('inf'))

                # Update short (minimum distance)
                if best_short is None or dist < best_short['total_distance']:
                    best_short = result

                # Update convenient (fewest transfers, then shortest distance)
                if best_convenient is None:
                    best_convenient = result
                else:
                    cur_t = len(best_convenient.get('transfers', []))
                    if (n_transfers < cur_t
                            or (n_transfers == cur_t
                                and dist < best_convenient['total_distance'])):
                        best_convenient = result
            elif last_error is None:
                last_error = error

    # Fare-based search (feeds cheap), one multi-source A* over all platform
    # pairs. Skip if the distance-based result already hits the minimum
    # possible fare: a single short segment costs exactly the base price, and
    # nothing can be cheaper than one base-priced ticket.
    base_fare = calculate_cost(0.5)[0]  # base ticket price
    if (best_short is not None
            and best_short.get('cost_regular', float('inf')) <= base_fare):
        best_cheap = best_short
    else:
        # Any found route's fare is an upper bound for the optimum; use the
        # UNCAPTED sum of segment fares — cost_regular is capped at the daily
        # limit (20 zł) and would not prune anything for long routes.
        def _raw_fare(r):
            if r is None:
                return float('inf')
            return sum(seg.get('cost_regular', 0.0)
                       for seg in r.get('segments', []))

        cheap_upper = min(_raw_fare(best_short), _raw_fare(best_convenient))
        cheap_result, cheap_error = find_cheapest_path(
            [p['id'] for p in from_platforms],
            [p['id'] for p in to_platforms],
            upper_bound=cheap_upper)

        if cheap_result is not None:
            best_cheap = cheap_result
        elif last_error is None:
            last_error = cheap_error
        # Fallback: the shortest route is always a valid (though possibly
        # not minimal) answer — never leave cheap empty when a route exists.
        if best_cheap is None and best_short is not None:
            best_cheap = best_short

    if best_short is None and best_convenient is None and best_cheap is None:
        err_msg = last_error or "Nie znaleziono trasy między tymi przystankami"
        err = ((None, err_msg), (None, err_msg), (None, err_msg))
        return _cache_route(cache_key, err, mode)

    # Build the triple-mode return
    short_pair = (best_short, None) if best_short else (None, last_error)
    convenient_pair = (best_convenient, None) if best_convenient else (None, last_error)
    cheap_pair = (best_cheap, None) if best_cheap else (None, last_error)

    triple = (short_pair, convenient_pair, cheap_pair)
    return _cache_route(cache_key, triple, mode)


def _cache_route(cache_key, triple_result, mode):
    """Store a triple-mode result in the route cache, then return
    the slice appropriate for *mode*.

    Memory guard: the cache is bounded BOTH by entry count and by total
    serialized bytes — a full route result (segments, shapes, positions)
    can reach ~100 KB, so a count-only cap could exceed the VPS RAM.
    """
    global _route_cache_bytes
    size = len(json.dumps(triple_result, ensure_ascii=False))
    if size > _ROUTE_CACHE_MAX_BYTES:
        return _slice_route_cache(triple_result, mode)

    if cache_key in _route_cache:
        _route_cache_bytes -= _route_cache[cache_key][1]
    _route_cache[cache_key] = (triple_result, size)
    _route_cache_bytes += size
    # evict oldest entries until under the byte budget
    while _route_cache_bytes > _ROUTE_CACHE_MAX_BYTES and _route_cache:
        oldest = next(iter(_route_cache))  # insertion-ordered dict
        _route_cache_bytes -= _route_cache[oldest][1]
        del _route_cache[oldest]

    return _slice_route_cache(triple_result, mode)


def _slice_route_cache(triple_result, mode):
    """Return the appropriate slice of a stored triple result."""
    if mode == 'convenient':
        return triple_result[1]
    elif mode == 'short':
        return triple_result[0]
    elif mode == 'cheap':
        return triple_result[2]
    else:
        # mode == 'both'
        return triple_result


# ============================================================
# Cache diagnostics
# ============================================================

def find_cache_info():
    """Return (count, bytes_used, max_bytes) for the A* find cache."""
    return len(_find_cache), _find_cache_bytes, _FIND_CACHE_MAX_BYTES


def cheap_search_info():
    """Return (searches, timeouts) for the fare-based A*."""
    return _cheap_search_count, _cheap_timeout_count


def route_cache_info():
    """Return (count, max_entries, bytes_used, max_bytes) for the
    group-to-group route cache."""
    return (len(_route_cache), _ROUTE_CACHE_MAX,
            _route_cache_bytes, _ROUTE_CACHE_MAX_BYTES)
