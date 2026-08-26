"""A* pathfinding for MPK Kraków route planner.

Provides shortest-path search between individual stops and between
stop groups (platforms sharing a name/location). Results are cached
with bounded memory usage.

Key differences from the original server.py implementation:
  - find_route_between_groups returns BOTH short and convenient results
    in a single pass through platform pairs (dual-mode).
  - A* has a 30-second timeout (time check + max iterations).
"""

import atexit
import heapq
import itertools
import json
import logging
import math
import os
import resource
import threading
import time

from .config import (
    ACC_CAP_KM as _ACC_CAP,
    ASTAR_MAX_ITERATIONS as _ASTAR_MAX_ITERATIONS,
    ASTAR_TIMEOUT_SECONDS as _ASTAR_TIMEOUT_SECONDS,
    CHEAP_HEURISTIC_CACHE_MAX,
    CHEAP_SEARCH_CONCURRENCY,
    CHEAP_SEARCH_MAX_SECONDS,
    CONVENIENT_BOARDING_PENALTY_ZL as _BOARDING_PENALTY,
    FIND_CACHE_MAX_BYTES as _FIND_CACHE_MAX_BYTES,
    MAX_PLATFORMS_TO_TRY_PER_GROUP as _MAX_PLATFORMS_TO_TRY,
    PRICE_LOOKUP_MAX_KM,
    ROUTE_CACHE_MAX_ENTRIES as _ROUTE_CACHE_MAX,
    ROUTE_CACHE_MAX_BYTES as _ROUTE_CACHE_MAX_BYTES,
    TRANSFER_TIME_SECONDS,
)
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
    # route cache periodically + at exit. A background flusher (every 2 min,
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

    global _route_cache_disk_loaded
    with _route_cache_lock:
        _route_cache_disk_loaded = len(_route_cache)

    def _flush_loop():
        last_sig = None
        last_count = 0
        while True:
            time.sleep(120)
            with _route_cache_lock:
                count = len(_route_cache)
                sig = (count, _route_cache_bytes)
                # flush: every 2 min or when 30+ entries were added since last
                if count - last_count >= 30:
                    last_count = count
                    _save_route_cache()
                    last_sig = sig
                elif sig != last_sig and count > 0:
                    last_sig = sig
                    last_count = count
                    _save_route_cache()

    threading.Thread(target=_flush_loop, daemon=True,
                     name='route-cache-flush').start()

    logging.getLogger('mpk.pathfinding').info(
        'Route cache restored',
        extra={'entries': len(_route_cache), 'loaded_from_disk': loaded,
               'bytes_kb': _route_cache_bytes // 1024})

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


# ============================================================
# Pathfinding cache (bounded by size)
# ============================================================

def _estimate_bytes(obj):
    """Rough memory footprint of a JSON-like structure, in bytes.

    Much cheaper than json.dumps() — used on every cache store to keep
    the byte budgets meaningful without serializing the whole result.
    """
    t = type(obj)
    if t is str:
        return len(obj) + 49
    if t is dict:
        return 184 + sum(_estimate_bytes(k) + _estimate_bytes(v)
                         for k, v in obj.items())
    if t is list or t is tuple:
        return 56 + sum(_estimate_bytes(x) for x in obj)
    if t is float:
        return 24
    if t is int:
        return 28
    if obj is None or t is bool:
        return 16
    return len(repr(obj)) + 49


_find_cache: dict = {}
_find_cache_bytes: int = 0
# The find cache is mutated from concurrent request threads — all reads and
# writes of the dict AND the byte counter must hold this lock.
_find_cache_lock = threading.Lock()


def _cache_put_find(key, value):
    """Store a value in the find cache, evicting oldest entries if over size limit."""
    global _find_cache_bytes
    if value[0] is None:
        return  # Never cache failures — the pair would be poisoned until eviction.
    size = _estimate_bytes(value)
    if size > _FIND_CACHE_MAX_BYTES:
        return  # Don't cache very large results
    with _find_cache_lock:
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
    with _find_cache_lock:
        entry = _find_cache.get(key)
        if entry is not None:
            return entry[0]
    return None


# ============================================================
# A* shortest path
# ============================================================

# Per-thread cancellation: every search runs in its own worker thread with a
# PRIVATE cancel event, so timing out one request never cancels another
# concurrent search (a shared global flag would do exactly that).
_search_ctx = threading.local()


def _check_cancelled():
    """Check if the calling thread's search was cancelled."""
    ev = getattr(_search_ctx, 'cancel_event', None)
    return ev is not None and ev.is_set()


def set_thread_cancel_event(ev):
    """Bind a cancellation event to the calling thread's current search."""
    _search_ctx.cancel_event = ev


def clear_thread_cancel_event():
    """Unbind the cancellation event after a search finishes."""
    _search_ctx.cancel_event = None


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
    pops = 0

    start_time = time.monotonic()

    while pq:
        pops += 1

        # Cooperative cancellation check
        if _check_cancelled():
            # Do NOT cache cancellations — the pair would be poisoned with a
            # permanent error for every future request until eviction.
            return None, "Anulowano: wyszukiwanie przerwane"

        est_total, pen_dist, real_dist, _, stop, route = heapq.heappop(pq)

        state = (stop, route)
        best_pen, _ = best.get(state, (float('inf'), 0))
        if pen_dist > best_pen:
            continue
        if est_total >= best_found_real:
            continue

        # Timeout / iteration limit / memory check (every 1000 pops is cheap)
        if pops % 1000 == 0:
            if time.monotonic() - start_time > _ASTAR_TIMEOUT_SECONDS:
                # Never cache timeouts — the pair would be poisoned until eviction.
                return None, "Timeout: nie znaleziono trasy w wymaganym czasie"
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            if mem_mb > 180:
                return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."

        if pops >= _ASTAR_MAX_ITERATIONS:
            return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."

        if stop == end_id:
            result = reconstruct_path(prev, start_id, end_id, route), None
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

    return None, "Nie znaleziono trasy między tymi przystankami"


# ============================================================
# Fare-based A* (cheapest route)
# ============================================================

# Ticket price changes at these segment distances (km):
# 0..3.5 -> base, then every 0.5 km up to the 9.00 zł cap at 8.5 km.
_PRICE_BOUNDS = [3.5 + 0.5 * k for k in range(11)]  # 3.5 .. 8.5
_cheap_search_count = 0
_cheap_timeout_count = 0
_counter_lock = threading.Lock()
# CPU gate: the fare A* is CPU-bound, so parallel searches would thrash.
# At most CHEAP_SEARCH_CONCURRENCY run at once; the rest queue briefly.
_CHEAP_SEARCH_GATE = threading.Semaphore(CHEAP_SEARCH_CONCURRENCY)

# price lookup: index = int(km * 100), capped well beyond _ACC_CAP
_PRICE_LOOKUP_MAX_INDEX = int(PRICE_LOOKUP_MAX_KM * 100)
_PRICE_LOOKUP = tuple(
    calculate_cost(min(i / 100.0, PRICE_LOOKUP_MAX_KM))[0]
    for i in range(_PRICE_LOOKUP_MAX_INDEX + 1)
)


def _ticket_price(acc_km):
    """Price of the CURRENT segment accumulated to acc_km (regular fare)."""
    return _PRICE_LOOKUP[min(int(acc_km * 100 + 0.5), _PRICE_LOOKUP_MAX_INDEX)]


def _dominates(closed_a, acc_a, closed_b, acc_b):
    """Exact dominance on the (stop, route) frontier.

    The ticket price is non-decreasing in the segment distance, so a state
    with lower closed cost AND lower accumulated distance is never worse:
    for any continuation X of the current segment,
        closed_a + price(acc_a + X) <= closed_b + price(acc_b + X)
    holds whenever closed_a <= closed_b and acc_a <= acc_b.
    """
    return closed_a <= closed_b + 1e-9 and acc_a <= acc_b + 1e-9


_cheap_heuristic_cache: dict = {}


def _cheap_heuristic(acc, remaining_km):
    """Admissible lower bound on the remaining fare.

    Remaining travel covers at least `remaining_km` (straight line). The
    cheapest way is to continue the current segment for x km and pay for the
    rest with new segments: price(acc+x) - price(acc) + price(remaining-x).
    Minimised over x at price breakpoints -> exact lower bound.

    Memoised: the search calls this on every edge relaxation. `remaining_km`
    is floored to 0.25 km buckets — flooring keeps the bound admissible
    (computed on a shorter distance), bucketing keeps the cache small.
    """
    if remaining_km <= 0:
        return 0.0
    key = (round(acc, 2), math.floor(remaining_km * 4) / 4)
    cached = _cheap_heuristic_cache.get(key)
    if cached is not None:
        return cached

    remaining = key[1]
    xs = {0.0, remaining}
    for b in _PRICE_BOUNDS:
        xs.add(b - acc)
        xs.add(remaining - b)
    xs = sorted(x for x in xs if 0 <= x <= remaining)
    best = float('inf')
    # step function: check jump points AND segment midpoints (min may lie
    # strictly between jumps)
    for i in range(len(xs) - 1):
        for x in (xs[i], (xs[i] + xs[i + 1]) / 2):
            cost = (_ticket_price(acc + x) - _ticket_price(acc)
                    + _ticket_price(remaining - x))
            best = min(best, cost)
    if len(_cheap_heuristic_cache) >= CHEAP_HEURISTIC_CACHE_MAX:
        _cheap_heuristic_cache.clear()
    _cheap_heuristic_cache[key] = best
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
    global _cheap_search_count
    cache_key = ('cheap', tuple(start_ids), tuple(end_ids))
    cached = _cache_get_find(cache_key)
    if cached is not None:
        return cached
    with _counter_lock:
        _cheap_search_count += 1

    if not start_ids or not end_ids:
        return None, "Nie podano przystanków"

    # CPU gate: at most 2 fare searches run concurrently (GIL).
    _CHEAP_SEARCH_GATE.acquire()
    try:
        return _find_cheapest_path_gated(start_ids, end_ids, cache_key,
                                         upper_bound, max_seconds=CHEAP_SEARCH_MAX_SECONDS)
    finally:
        _CHEAP_SEARCH_GATE.release()


def _find_greedy_route(start_ids, end_ids, boarding_penalty_zl=0.0,
                       max_seconds=2.0):
    """Fast approximate route search: plain A* over (stop, route) states —
    no Pareto dominance sets, so it runs in milliseconds even on long
    multi-segment trips.

    Cost = fare (closed + current ticket) + boarding_penalty_zl per boarding.
    With penalty 0 this is the cheapest-route helper; with penalty > 0 it
    balances fare against the number of rides ("convenient" route).

    Returns (result_dict, fare) or (None, None).
    """
    end_set = set(end_ids)
    end_coords = stop_coords.get(end_ids[0], (0, 0))

    pq = []
    seq = 0
    for start_id in start_ids:
        if start_id not in adjacency:
            continue
        sc = stop_coords.get(start_id, (0, 0))
        h = _cheap_heuristic(
            0.0, haversine_km(sc[0], sc[1], end_coords[0], end_coords[1]))
        heapq.heappush(pq, (h, 0.0, 0.0, 0, seq, start_id, None))
        seq += 1
    if not pq:
        return None, None

    best = {}
    prev = {}
    start_time = time.monotonic()
    pops = 0

    while pq:
        pops += 1
        if _check_cancelled():
            return None, None
        # The greedy pass must stay fast — it's a helper, not the product.
        if pops % 512 == 0 and time.monotonic() - start_time > max_seconds:
            return None, None

        _, closed, acc, boardings, _, stop, route = heapq.heappop(pq)
        state = (stop, route)
        if closed > best.get(state, float('inf')) + 1e-9:
            continue

        if stop in end_set:
            path_with_edges = []
            current = state
            while current in prev:
                prev_stop, prev_route, edge = prev[current]
                path_with_edges.append((current[0], current[1], edge))
                current = (prev_stop, prev_route)
            path_with_edges.append((current[0], None, None))
            path_with_edges.reverse()
            return _build_route_result(path_with_edges), closed + _ticket_price(acc)

        for edge in adjacency.get(stop, []):
            next_stop = edge['to']
            next_route = edge['route_id']
            dist = edge['distance']

            if next_route == 'transfer':
                # Walking transfer: close the ticket, no boarding.
                new_closed = closed + _ticket_price(acc)
                new_acc = 0.0
                new_boardings = boardings
            elif route is None or route == 'transfer' or next_route != route:
                # Boarding a vehicle (first ride or route change).
                new_closed = closed + _ticket_price(acc)
                new_acc = dist
                new_boardings = boardings + 1
            else:
                new_closed = closed
                new_acc = acc + dist
                new_boardings = boardings

            new_acc = min(new_acc, _ACC_CAP)
            new_acc = round(new_acc * 20) / 20
            g = (new_closed + _ticket_price(new_acc)
                 + boarding_penalty_zl * new_boardings)
            nxt = (next_stop, next_route)
            if g < best.get(nxt, float('inf')) - 1e-9:
                best[nxt] = g
                prev[nxt] = (stop, route, edge)
                coords = stop_coords.get(next_stop, (0, 0))
                # Fare-only heuristic is admissible for the penalised cost:
                # it lower-bounds the fare component, boardings add on top.
                h = _cheap_heuristic(
                    new_acc, haversine_km(coords[0], coords[1],
                                          end_coords[0], end_coords[1]))
                seq += 1
                heapq.heappush(pq, (g + h, new_closed, new_acc, new_boardings,
                                    seq, next_stop, next_route))

    return None, None


def _find_cheapest_path_gated(start_ids, end_ids, cache_key, upper_bound,
                              max_seconds=CHEAP_SEARCH_MAX_SECONDS):
    """Body of find_cheapest_path, executed under the CPU gate.

    Two phases:
      1. greedy pass (_find_greedy_route, penalty=0) — near-optimal fare in
         milliseconds; tightens upper_bound and acts as the fallback,
      2. exact Pareto A* with the tightened bound.

    max_seconds caps the wall time of the exact phase: beyond that the
    greedy result (if any) is returned, so the user still gets a cheap
    route. Only without any greedy result does the caller fall back to
    the short route.
    """
    global _cheap_timeout_count
    end_set = set(end_ids)
    end_coords = stop_coords.get(end_ids[0], (0, 0))

    # Phase 1: greedy — bound tightening + timeout fallback
    greedy, greedy_fare = _find_greedy_route(start_ids, end_ids)
    if greedy_fare is not None:
        upper_bound = min(upper_bound, greedy_fare)

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
        return None, "Przystanek początkowy nie został znaleziony w grafie"

    frontier = {}  # (stop, route) -> [(closed, acc)] — only SETTLED states
    prev = {}  # (stop, route, acc) -> (parent_stop, parent_route, parent_acc, edge)
    start_time = time.monotonic()
    iterations = 0

    while pq:
        # Cooperative cancellation check
        if _check_cancelled():
            # Do NOT cache cancellations — the pair would be poisoned with a
            # permanent error for every future request until eviction.
            return None, "Anulowano: wyszukiwanie przerwane"

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

        # Timeout / iteration limits / memory: hard wall-clock cap keeps the UI
        # responsive; the greedy route (if found) is served instead.
        if iterations % 1000 == 0:
            if time.monotonic() - start_time > max_seconds:
                with _counter_lock:
                    _cheap_timeout_count += 1
                if greedy is not None:
                    result = greedy, None
                    _cache_put_find(cache_key, result)
                    return result
                return None, "Timeout: nie znaleziono trasy w wymaganym czasie"
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            if mem_mb > 180:
                with _counter_lock:
                    _cheap_timeout_count += 1
                if greedy is not None:
                    result = greedy, None
                    _cache_put_find(cache_key, result)
                    return result
                return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."
        if iterations >= _ASTAR_MAX_ITERATIONS:
            with _counter_lock:
                _cheap_timeout_count += 1
            if greedy is not None:
                result = greedy, None
                _cache_put_find(cache_key, result)
                return result
            return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."

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
    return None, "Nie znaleziono trasy między tymi przystankami"


# ============================================================
# Path reconstruction
# ============================================================

def reconstruct_path(prev, start_id, end_id, end_route):
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
    """Extract the portion of a route's shape that the segment travels.

    Walks the shape monotonically through ALL stops of the segment. Matching
    only the endpoints independently breaks on loop lines (e.g. 240): the
    shape passes the same area twice, so a stop can latch onto the wrong pass
    and the slice ends up covering the entire loop.

    Both shape directions are tried (GTFS shapes may run opposite to travel)
    and the tighter slice wins. Returns [[lat, lon], ...] or [].
    """
    shape_points = route_shapes.get(route_id)
    if not shape_points or len(stops) < 2:
        return []

    coords = []
    for sid in stops:
        s = stops_by_id.get(sid, {})
        lat, lon = s.get('lat'), s.get('lon')
        if lat is None or lon is None:
            return []
        coords.append((lat, lon))

    n = len(shape_points)

    def walk(index_seq):
        """Match each stop to a shape index, moving monotonically forward
        through index_seq (never backwards — that's what prevents loop lines
        from collapsing the slice onto the wrong pass). Returns
        (matched_indices, total_match_distance_km)."""
        seq = list(index_seq)
        matched = []
        total_d = 0.0
        pos = 0
        for lat, lon in coords:
            best_j, best_d = None, float('inf')
            for j in range(pos, len(seq)):
                p = shape_points[seq[j]]
                d = haversine_km(p[0], p[1], lat, lon)
                if d < best_d:
                    best_d, best_j = d, j
            if best_j is None:
                best_j = pos  # sequence exhausted — pin to last position
                best_d = haversine_km(shape_points[seq[pos]][0],
                                      shape_points[seq[pos]][1], lat, lon)
            matched.append(seq[best_j])
            total_d += best_d
            pos = best_j
        return matched, total_d

    forward, fwd_d = walk(range(n))
    backward, bwd_d = walk(range(n - 1, -1, -1))

    # The correct direction matches every stop closely; the wrong one ends up
    # pinning later stops to a distant shape point (large match distance).
    seq = forward if fwd_d <= bwd_d else backward
    i0, i1 = min(seq[0], seq[-1]), max(seq[0], seq[-1])
    if i1 <= i0:
        # Both ends matched the same shape point — too short to draw;
        # the frontend falls back to a straight line between the stops.
        return []

    return [list(p) for p in shape_points[i0:i1 + 1]]


# ============================================================
# Group-to-group route finding (dual-mode)
# ============================================================

_route_cache: dict = {}
_route_cache_bytes = 0
_route_cache_lock = threading.Lock()
_feed_version = ''
# Number of entries restored from the disk cache at startup (metrics only).
_route_cache_disk_loaded = 0
# Entries computed fresh (not loaded from disk) during this server run.
# Cumulative — eviction may later remove some of them from the cache.
_route_cache_computed = 0
# In-flight searches, keyed by (from_group_id, to_group_id): while a request
# is computing a pair, concurrent identical requests wait for its result
# instead of re-running the expensive A* (10× the same pair = 1 search).
_route_inflight = {}
# How long an in-flight waiter waits for the producer before recomputing.
_INFLIGHT_MAX_WAIT_SECONDS = 60.0

# Disk persistence: on shutdown the route cache is written to
# processed/route_cache_<feed_version>.json so a restart starts warm.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'processed')
# Bump when the computed result format/geometry changes — old disk caches
# become unreadable under a new name and stale entries never leak back in
# (e.g. v2: fixed loop-line shape slicing in _extract_shape_segment;
#  v3: convenient = dedicated fare+transfers balance search).
_CACHE_ALGO_VERSION = 'v3'


def _cache_file_path():
    ver = str(_feed_version) if _feed_version else 'unknown'
    return os.path.join(_CACHE_DIR, f'route_cache_{ver}_{_CACHE_ALGO_VERSION}.json')


def _save_route_cache():
    """Write the route cache to disk (best-effort, at exit)."""
    global _route_cache, _route_cache_bytes
    try:
        with _route_cache_lock:
            data = {f"{k[0]}|{k[1]}|{k[2]}": v[0]
                    for k, v in _route_cache.items()}
        with open(_cache_file_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        _cleanup_stale_cache_files()
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
            size = _estimate_bytes(triple)
            if size > _ROUTE_CACHE_MAX_BYTES:
                continue
            _route_cache[ck] = (triple, size)
            _route_cache_bytes += size
        while _route_cache_bytes > _ROUTE_CACHE_MAX_BYTES and _route_cache:
            oldest = next(iter(_route_cache))
            _route_cache_bytes -= _route_cache[oldest][1]
            del _route_cache[oldest]
    _cleanup_stale_cache_files()
    return len(data)


def _cleanup_stale_cache_files():
    """Remove route-cache files from older feed versions / algo versions."""
    try:
        current = os.path.basename(_cache_file_path())
        for name in os.listdir(_CACHE_DIR):
            if name.startswith('route_cache_') and name.endswith('.json') \
                    and name != current:
                os.remove(os.path.join(_CACHE_DIR, name))
    except OSError:
        pass  # best-effort — stale files are harmless

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
        # Generous deadline: the producer's finally block always notifies,
        # so waiting is safe; the cap only guards against a lost producer.
        deadline = time.monotonic() + _INFLIGHT_MAX_WAIT_SECONDS
        with inflight['cond']:
            while not inflight['done']:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                inflight['cond'].wait(remaining)
        with _route_cache_lock:
            cached = _route_cache.get(cache_key)
        if cached is not None:
            return _slice_route_cache(cached[0], mode)
        # Producer lost without storing a result — fall through and compute

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
            # Distance-based search (feeds short)
            result, error = find_shortest_path(
                from_platform['id'], to_platform['id'])

            if result is not None:
                dist = result.get('total_distance', float('inf'))

                # Update short (minimum distance)
                if best_short is None or dist < best_short['total_distance']:
                    best_short = result
            elif last_error is None:
                last_error = error

    # Convenient: dedicated fare+transfers balance search (multi-source over
    # all platform pairs). Cost = fare + penalty per boarding, so it prefers
    # few rides but won't ignore a much pricier detour — a route people
    # would actually take. The distance-based candidates above are the
    # fallback when this search finds nothing.
    best_convenient, _conv_fare = _find_greedy_route(
        [p['id'] for p in from_platforms],
        [p['id'] for p in to_platforms],
        boarding_penalty_zl=_BOARDING_PENALTY)
    if best_convenient is None:
        # Fallback: fewest-transfer distance-based candidate
        for from_platform in from_platforms:
            for to_platform in to_platforms:
                result, _ = find_shortest_path(
                    from_platform['id'], to_platform['id'])
                if result is None:
                    continue
                n_transfers = len(result.get('transfers', []))
                dist = result.get('total_distance', float('inf'))
                if best_convenient is None:
                    best_convenient = result
                else:
                    cur_t = len(best_convenient.get('transfers', []))
                    if (n_transfers < cur_t
                            or (n_transfers == cur_t
                                and dist < best_convenient['total_distance'])):
                        best_convenient = result

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
    estimated bytes — a full route result (segments, shapes, positions)
    can reach ~100 KB, so a count-only cap could exhaust host memory.
    Cached values are immutable by contract — never mutate a returned
    result, it is shared with every concurrent reader of this entry.
    """
    global _route_cache_bytes, _route_cache_computed
    size = _estimate_bytes(triple_result)
    if size > _ROUTE_CACHE_MAX_BYTES:
        return _slice_route_cache(triple_result, mode)

    with _route_cache_lock:
        if cache_key in _route_cache:
            _route_cache_bytes -= _route_cache[cache_key][1]
        else:
            _route_cache_computed += 1  # brand-new pair computed this run
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
    with _counter_lock:
        return _cheap_search_count, _cheap_timeout_count


def route_cache_info():
    """Return (count, max_entries, bytes_used, max_bytes) for the
    group-to-group route cache."""
    return (len(_route_cache), _ROUTE_CACHE_MAX,
            _route_cache_bytes, _ROUTE_CACHE_MAX_BYTES)


def route_cache_origin_info():
    """Return (disk_loaded, computed_this_run) for the route cache.

    disk_loaded — entries restored from the disk cache at startup,
    computed_this_run — new pairs computed from real requests this run
    (cumulative; eviction may have removed some since).
    """
    return _route_cache_disk_loaded, _route_cache_computed
