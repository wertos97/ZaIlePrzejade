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
import collections
import sqlite3
import heapq
import json
import logging
import math
import os
import resource
import threading
import time

from .config import (
    ASTAR_MAX_ITERATIONS as _ASTAR_MAX_ITERATIONS,
    ASTAR_TIMEOUT_SECONDS as _ASTAR_TIMEOUT_SECONDS,
    CHEAP_SEARCH_CONCURRENCY,
    CHEAP_SEARCH_MAX_SECONDS,
    CONVENIENT_BOARDING_PENALTY_ZL as _BOARDING_PENALTY,
    CONVENIENT_SEARCH_MAX_SECONDS,
    FIND_CACHE_MAX_BYTES as _FIND_CACHE_MAX_BYTES,
    MAX_PLATFORMS_TO_TRY_PER_GROUP as _MAX_PLATFORMS_TO_TRY,
    MEMORY_LIMIT_MB as _MEMORY_LIMIT_MB,
    ROUTE_CACHE_MAX_ENTRIES as _ROUTE_CACHE_MAX,
    ROUTE_CACHE_MAX_BYTES as _ROUTE_CACHE_MAX_BYTES,
    TRANSFER_TIME_SECONDS,
)
from .cost import (
    calculate_cost,
    calculate_route_cost,
    calculate_single_ticket_cost,
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
# Line-graph index (built in init_pathfinding): route edges by from-stop,
# routes serving each stop — used by the ride enumeration.
_route_edges: dict = {}
_stop_routes: dict = {}
_walk_edges: dict = {}
_walk_component: dict = {}
# LRU of line-sweep results (dist/parents per (line, platform)) shared by
# the ride enumerations of both modes and across requests. Bounded memory:
# entries store ride distances only.
_sweep_memo: collections.OrderedDict = collections.OrderedDict()
_SWEEP_MEMO_MAX = 2000


# ============================================================
# Initialisation
# ============================================================

def _rebuild_line_index():
    """(Re)build the line-graph index from the CURRENT adjacency global.

    Built once at init; the synthetic-graph tests call it after swapping
    adjacency so the enumeration always walks the live graph.
    """
    global _route_edges, _stop_routes, _walk_edges, _walk_component
    _route_edges = {}
    _stop_routes = {}
    _walk_edges = {}
    for stop_id, edges in adjacency.items():
        for edge in edges:
            rid = edge['route_id']
            if rid == 'transfer':
                _walk_edges.setdefault(stop_id, []).append(edge)
                continue
            _route_edges.setdefault(rid, {}).setdefault(stop_id, []).append(edge)
            _stop_routes.setdefault(stop_id, set()).add(rid)
    _walk_component = {}  # platform -> frozenset of walk-connected platforms
    for stop_id in _walk_edges:
        if stop_id in _walk_component:
            continue
        comp = {stop_id}
        queue = [stop_id]
        while queue:
            s = queue.pop()
            for wedge in _walk_edges.get(s, ()):
                t = wedge['to']
                if t not in comp:
                    comp.add(t)
                    queue.append(t)
        frozen = frozenset(comp)
        for s in comp:
            _walk_component[s] = frozen


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

    # Disk cache: a sqlite database holding EVERY computed route pair
    # (write-through, incremental). The in-memory dict is only the hot set —
    # on a memory miss the pair is read back from sqlite. The database
    # filename embeds the GTFS feed version and the cache algo version, so
    # a GTFS update (or a fare-semantics change) simply starts a new file.
    global _feed_version
    try:
        from . import data as _data
        _feed_version = _data.feed_metadata.get('version', '')
    except Exception:
        _feed_version = ''
    _open_cache_db()
    atexit.register(_close_cache_db)

    global _route_cache_db_entries
    _route_cache_db_entries = _db_count()

    logging.getLogger('mpk.pathfinding').info(
        'Route cache opened',
        extra={'db_routes': _route_cache_db_entries,
               'memory_entries': len(_route_cache),
               'memory_bytes_kb': _route_cache_bytes // 1024})

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

    # Line-graph index for the ride enumeration (see _enumerate_ride_bound)
    _rebuild_line_index()


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

    Thin wrapper over the multi-source :func:`find_shortest_path_multi`
    (a single A* from all start platforms to any end platform yields the
    shortest among all platform pairs, so calling it for one pair is
    identical to the old single-pair search).

    Uses penalty-weighted distance (penalises route changes) as the
    primary cost and haversine as the heuristic. Results are cached.
    Times out after 30 seconds or 200 000 iterations.

    Returns (result_dict, None) on success or (None, error_string) on
    failure.
    """
    return find_shortest_path_multi([start_id], [end_id])


def find_shortest_path_multi(start_ids, end_ids):
    """Multi-source / multi-target A* shortest path (penalty-weighted distance).

    Seeds the priority queue from *every* start platform at once and stops at
    the first end platform popped. Because the edge cost is identical for all
    starts, the first end reached is the globally shortest path among *all*
    (start, end) platform pairs — i.e. exactly what the old code computed by
    running ``find_shortest_path`` once per pair and taking the minimum, but in
    a single search instead of up to 4 sequential ones. This is what keeps the
    fast route path within its wall-clock budget for hard (long) trips.

    Results are cached per (frozenset(starts), frozenset(ends)).

    Returns (result_dict, None) on success or (None, error_string) on failure.
    """
    start_ids = list(start_ids)
    end_ids = list(end_ids)
    cache_key = ('multi', tuple(sorted(start_ids)), tuple(sorted(end_ids)))
    cached = _cache_get_find(cache_key)
    if cached is not None:
        return cached

    end_set = set(end_ids)
    if not start_ids or not end_set:
        return None, "Brak przystanków"
    if not any(s in adjacency for s in start_ids):
        return None, "Przystanek początkowy nie został znaleziony w grafie"
    if not any(e in adjacency for e in end_ids):
        return None, "Przystanek końcowy nie został znaleziony w grafie"

    # Precompute end coordinates for the (admissible) min-haversine heuristic.
    end_coords_list = [stop_coords.get(e, (0, 0)) for e in end_ids]

    def _h(stop):
        sc = stop_coords.get(stop, (0, 0))
        best = float('inf')
        for ec in end_coords_list:
            d = haversine_km(sc[0], sc[1], ec[0], ec[1])
            if d < best:
                best = d
        return best

    CHANGE_PENALTY = 0.3

    pq = []
    seq = 0
    best = {}
    prev = {}
    start_time = time.monotonic()
    pops = 0

    for sid in start_ids:
        if sid not in adjacency:
            continue
        h_start = _h(sid)
        heapq.heappush(pq, (h_start, 0.0, 0.0, seq, sid, None))
        best[(sid, None)] = (0.0, 0.0)
        seq += 1

    if not pq:
        return None, "Przystanek początkowy nie został znaleziony w grafie"

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

        # Timeout / iteration limit / memory check (every 1000 pops is cheap)
        if pops % 1000 == 0:
            if time.monotonic() - start_time > _ASTAR_TIMEOUT_SECONDS:
                # Never cache timeouts — the pair would be poisoned until eviction.
                return None, "Timeout: nie znaleziono trasy w wymaganym czasie"
            try:
                with open('/proc/self/statm') as f:
                    pages = int(f.read().split()[1])  # [1] = RSS, not [0] = VSZ
                mem_mb = pages * 4 // 1024  # pages * 4 KB / 1024 = MB
            except Exception:
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            if mem_mb > _MEMORY_LIMIT_MB:
                return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."

        if pops >= _ASTAR_MAX_ITERATIONS:
            return None, "Serwer jest przeciążony. Spróbuj krótszą trasę."

        if stop in end_set:
            # Reconstruct from the end state back through prev until we reach a
            # start platform (a state with no prev entry), then prepend it.
            path_with_edges = []
            cur = (stop, route)
            while cur in prev:
                ps, pr, edge = prev[cur]
                path_with_edges.append((cur[0], cur[1], edge))
                cur = (ps, pr)
            path_with_edges.append((cur[0], None, None))
            path_with_edges.reverse()
            result = _build_route_result(path_with_edges), None
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
                h = _h(next_stop)
                estimated = new_pen + h
                best[next_state] = (new_pen, new_real)
                prev[next_state] = (stop, route, edge)
                seq += 1
                heapq.heappush(pq, (estimated, new_pen, new_real, seq,
                                    next_stop, next_route))

    return None, "Nie znaleziono trasy między tymi przystankami"


# ============================================================
# Fare model — official straight-line distance
# ============================================================

_cheap_search_count = 0
_cheap_timeout_count = 0
_counter_lock = threading.Lock()
# CPU gate: the fare search is CPU-bound, so parallel searches would thrash.
# At most CHEAP_SEARCH_CONCURRENCY run at once; the rest queue briefly.
_CHEAP_SEARCH_GATE = threading.Semaphore(CHEAP_SEARCH_CONCURRENCY)

# price lookup: distance -> regular fare (exact, same function as display)
def _ticket_price(distance_km):
    """Regular fare for a single ride covering `distance_km`."""
    return calculate_cost(distance_km)[0]


# Base ticket price — the minimum cost of any real (non-zero) ride segment.
# Used by the exact searches for the boarding-count certification bounds.
_BASE_FARE = calculate_cost(0.5)[0]


def _tariff_point(stop_id):
    """Coordinates used for the official straight-line tariff distance.

    The app models a stop as a NAME GROUP (all platforms belong to the same
    tariff stop). Use the group's averaged position so the search and the
    displayed segment price always agree, regardless of which peron the
    vehicle uses.
    """
    group_id = stop_to_group.get(stop_id)
    if group_id is not None:
        group = stops_grouped.get(group_id)
        if group and group.get('lat') is not None \
                and group.get('lon') is not None:
            return (group['lat'], group['lon'])
    info = stops_by_id.get(stop_id)
    if info and info.get('lat') is not None and info.get('lon') is not None:
        return (info['lat'], info['lon'])
    return None


def _straight_line_km(from_stop, to_stop):
    """Official tariff distance: straight line between the two stop groups.

    Returns None when a stop has no coordinates; callers then fall back
    to the GTFS distance they already carry.
    """
    a = _tariff_point(from_stop)
    b = _tariff_point(to_stop)
    if a is None or b is None:
        return None
    return haversine_km(a[0], a[1], b[0], b[1])


def _ride_fare(from_stop, to_stop, fallback_km=0.0):
    """Fare of one ride, priced by the OFFICIAL straight-line distance."""
    km = _straight_line_km(from_stop, to_stop)
    if km is None:
        km = fallback_km
    return _ticket_price(km)


def find_cheapest_path(start_ids, end_ids, upper_bound=float('inf'),
                       bound_route=None):
    """Exact cheapest-route search between stop sets.

    Iterative deepening on the number of boardings (see
    :func:`_find_exact_fare_route`): the returned route is the PROVEN
    optimum of "total fare", or an error if the budget ran out before the
    optimum was certified. No approximate results are ever returned.

    A ride's fare is the OFFICIAL straight-line distance between its
    boarding and alighting stop groups; moving along the same line
    continues the current ride, transfers close it (each ride is a separate
    ticket priced from zero).

    upper_bound: scalar cost of a known complete route (bound_route's fare).
    bound_route: that route itself — returned as the certified optimum when
    the search proves nothing cheaper exists without needing to enumerate it.

    Returns (result_dict, None) or (None, error_string); successes cached.
    """
    return _find_exact_fare_route(start_ids, end_ids, upper_bound,
                                  boarding_penalty_zl=0.0,
                                  max_seconds=CHEAP_SEARCH_MAX_SECONDS,
                                  cache_prefix='cheap',
                                  bound_route=bound_route)


def find_most_convenient_path(start_ids, end_ids, upper_bound=float('inf'),
                              bound_route=None):
    """Exact most-convenient search: the minimum of
    "total fare + CONVENIENT_BOARDING_PENALTY_ZL per boarding".

    Same exact machinery as :func:`find_cheapest_path` with the boarding
    penalty folded into the closed cost. Dominance stays exact: the cost of
    closing a ride depends only on its boarding/alighting stops and FUTURE
    boardings — never on how the state was reached.

    Returns (result_dict, None) or (None, error_string); successes cached.
    """
    return _find_exact_fare_route(start_ids, end_ids, upper_bound,
                                  boarding_penalty_zl=_BOARDING_PENALTY,
                                  max_seconds=CONVENIENT_SEARCH_MAX_SECONDS,
                                  cache_prefix='convenient',
                                  bound_route=bound_route)


def _walk_platforms(stop_id):
    """Platforms reachable from stop_id by walking transfers — the whole
    transitive walk component (chained walks are free and legal), stop_id
    itself included."""
    comp = _walk_component.get(stop_id)
    if comp is None:
        return (stop_id,)
    return tuple(comp)


def _walk_fragment(from_stop, to_stop):
    """Walking-transfer hop(s) from from_stop to to_stop as a path
    fragment (BFS over the walk edges), or None when unreachable."""
    if from_stop == to_stop:
        return []
    parents = {from_stop: None}
    queue = collections.deque([from_stop])
    while queue:
        s = queue.popleft()
        for wedge in _walk_edges.get(s, ()):
            t = wedge['to']
            if t in parents:
                continue
            parents[t] = (s, wedge)
            if t == to_stop:
                fragment = []
                cur = t
                while parents[cur] is not None:
                    prev_s, edge = parents[cur]
                    fragment.append((cur, 'transfer', edge))
                    cur = prev_s
                fragment.reverse()
                return fragment
            queue.append(t)
    return None


def _ride_sweep(route_id, start_stop, want_parents=False):
    """Dijkstra over one line's edges from start_stop, PLUS the free
    walking transfers between platforms of the same stop cluster (a walk
    does not consume ticket distance — it just re-locates the passenger,
    who may continue riding the same line on a different platform).

    This makes the sweep COMPLETE for the "one ticket on this line" class:
    every stop reachable by riding line route_id with arbitrary same-cluster
    walks is found with its exact ticket distance.

    By default returns the minimum ride distance per stop, cached in the
    global LRU (shared across modes and requests). With want_parents=True
    returns (dist, parents) for path reconstruction (not cached).
    """
    if want_parents:
        return _ride_sweep_with_parents(route_id, start_stop)
    key = (route_id, start_stop)
    cached = _sweep_memo.get(key)
    if cached is not None:
        _sweep_memo.move_to_end(key)
        return cached
    redges = _route_edges.get(route_id)
    dist = None
    if redges and (start_stop in redges or _walk_edges.get(start_stop)
                   or adjacency.get(start_stop)):
        dist = {start_stop: 0.0}
        pq = [(0.0, start_stop)]
        while pq:
            d, s = heapq.heappop(pq)
            if d > dist.get(s, float('inf')) + 1e-12:
                continue
            for edge in redges.get(s, ()):
                t = edge['to']
                nd = d + edge['distance']
                if nd < dist.get(t, float('inf')) - 1e-12:
                    dist[t] = nd
                    heapq.heappush(pq, (nd, t))
            for wedge in _walk_edges.get(s, ()):
                t = wedge['to']
                if d < dist.get(t, float('inf')) - 1e-12:
                    dist[t] = d
                    heapq.heappush(pq, (d, t))
    _sweep_memo[key] = dist
    _sweep_memo.move_to_end(key)
    while len(_sweep_memo) > _SWEEP_MEMO_MAX:
        _sweep_memo.popitem(last=False)
    return dist


def _ride_sweep_with_parents(route_id, start_stop):
    """As _ride_sweep, but returns (dist, parents) for path reconstruction.
    Not cached (used rarely — only to rebuild the winning route)."""
    redges = _route_edges.get(route_id)
    if not redges or start_stop not in redges and not _walk_edges.get(start_stop):
        return None, None
    dist = {start_stop: 0.0}
    parents = {}
    pq = [(0.0, start_stop)]
    while pq:
        d, s = heapq.heappop(pq)
        if d > dist.get(s, float('inf')) + 1e-12:
            continue
        for edge in redges.get(s, ()):
            t = edge['to']
            nd = d + edge['distance']
            if nd < dist.get(t, float('inf')) - 1e-12:
                dist[t] = nd
                parents[t] = (s, edge)
                heapq.heappush(pq, (nd, t))
        for wedge in _walk_edges.get(s, ()):
            t = wedge['to']
            if d < dist.get(t, float('inf')) - 1e-12:
                dist[t] = d
                parents[t] = (s, wedge)
                heapq.heappush(pq, (d, t))
    return dist, parents


def _ride_fragment(parents, start, end):
    """Reconstruct a ride path as [(stop, route, edge), ...] fragment
    (WITHOUT the leading origin entry)."""
    fragment = []
    cur = end
    while cur != start:
        prev_stop, edge = parents[cur]
        fragment.append((cur, edge['route_id'], edge))
        cur = prev_stop
    fragment.reverse()
    return fragment


def _enumerate_ride_bound(from_platforms, end_platforms, boarding_penalty_zl,
                          deadline, upper_bound=float('inf')):
    """Exact-for-up-to-4-rides fare enumeration over the line graph (no A*).

    Every ride is priced by the OFFICIAL tariff distance: the straight line
    between its boarding and alighting platforms (not the GTFS path length).

    Composes WHOLE rides instead of expanding per-hop states:
      F1 — one sweep per (origin-group platform, its line): every 1-ride
           prefix, complete (sweeps include the free same-group walks, so
           walk-merged same-line continuations are priced correctly);
      F2 — per (stop, line) the cheapest 2-ride prefix (complete);
      B1 — per (platform, line) the cheapest single ride TO the destination
           (complete); per-platform top-2 supports the line exclusions;
      B2 — lazily per (platform, line) the cheapest 2-ride suffix to the
           destination (complete).

    Composing F1/F2 with B1/B2 enumerates ALL routes with up to 4 rides, so
    the best found route is CERTIFIABLY optimal: a 5th boarding costs at
    least base_fare + penalty, and displayed fares are capped at the daily
    limit, so no 5+-ride route can strictly beat the best <=4-ride route
    (the driver certifies this explicitly). The A* remains as the fallback
    for the degenerate case of the enumeration hitting its deadline.

    Returns (path_with_edges, scalar, completed) for the best route.
    `completed` is True only when every composition loop ran to exhaustion
    (no deadline break fired) — the driver uses it to certify "nothing
    better exists" outcomes without running the expensive A* deepening.
    On deadline the partial best-so-far is still returned (completed=False)
    purely to tighten the caller's upper bound; it never certifies anything.
    `upper_bound` (same scalar units: uncapped fare + penalty per boarding)
    optionally seeds best_scalar: a valid complete route costs at most that,
    so anything reaching it prunes immediately. Seeding never affects
    exactness — it only cuts work, and stage exits / certification below
    treat a seeded-but-empty best as "no route" (callers ignore None frags).
    """
    penalty = boarding_penalty_zl
    # Compositions may board/alight at ANY platform of the origin/destination
    # groups (walks are free) — expand the platform sets to whole groups.
    from_platforms = {p for o in from_platforms for p in _walk_platforms(o)}
    end_platforms = {p for e in end_platforms for p in _walk_platforms(e)}
    end_set = set(end_platforms)
    best_scalar = upper_bound
    best = None  # ride list [(line, from_platform, to_platform), ...]
    # False as soon as any composition loop bails out on the deadline.
    completed = True

    def consider(scalar, rides):
        nonlocal best_scalar, best
        # Strict improvement always records. Additionally, the FIRST route
        # at/below a seeded upper bound is recorded (ties with the seed):
        # otherwise a bound that EQUALS the optimum (e.g. a capped short
        # route) prunes the optimum itself away and the enumeration burns
        # the whole budget proving emptiness. Recording the tie restores
        # the stage exits; optimality is unaffected (best is always a real
        # route, ties are equally optimal, later improvements stay strict).
        if scalar < best_scalar - 1e-9 or (
                best is None and scalar <= best_scalar + 1e-9):
            best_scalar = scalar
            best = rides

    # ---- F1: first rides, complete per (arrival platform, line)
    f1 = {}  # (platform, line) -> (fare, origin_platform)
    for o in from_platforms:
        for route_id in _stop_routes.get(o, ()):
            reach = _ride_sweep(route_id, o)
            if reach is None:
                continue
            for t in reach:
                fare = _ride_fare(o, t)
                key = (t, route_id)
                cur = f1.get(key)
                if cur is None or fare < cur[0]:
                    f1[key] = (fare, o)
                    # 1-ride completion (sweeps include walks, so t covers
                    # every platform of its stop group)
                    if t in end_set:
                        consider(penalty + fare, [(route_id, o, t)])

    # ---- stage 1 exit: if the best 1-ride route can already be certified
    # (every route with 2+ boardings costs >= per_floor * 2), stop here.
    per_floor = _BASE_FARE + penalty
    if best is not None and best_scalar <= per_floor * 2 + 1e-9:
        return _materialize(best), best_scalar, True

    # ---- B1: cheapest single rides TO the destination.
    # b1[(platform, line)] = (fare, dest_platform); b1_top[platform] holds
    # the 2 smallest values over DISTINCT lines (supports exclusions).
    b1 = {}
    for e in end_platforms:
        for route_id in _stop_routes.get(e, ()):
            reach = _ride_sweep(route_id, e)
            if reach is None:
                continue
            for s in reach:
                if s in end_set:
                    continue  # degenerate: already at the destination
                fare = _ride_fare(s, e)
                key = (s, route_id)
                cur = b1.get(key)
                if cur is None or fare < cur[0]:
                    b1[key] = (fare, e)
    b1_top = {}
    for (s, line), (fare, e) in b1.items():
        lst = b1_top.setdefault(s, [])
        lst.append((fare, line, e))
        lst.sort()
        del lst[2:]

    def b1_best(platform, exclude_line):
        for val, line, e in b1_top.get(platform, ()):
            if line != exclude_line:
                return val, (line, platform, e)
        return None

    # ---- 2-ride composition: F1 arrival + B1
    for (t, line1), (fare1, o) in f1.items():
        if time.monotonic() > deadline:
            completed = False
            break
        for t2 in _walk_platforms(t):
            pair = b1_best(t2, line1)
            if pair is not None:
                val, b1_desc = pair
                consider(penalty + fare1 + val + penalty,
                         [(line1, o, t), b1_desc])

    # ---- stage 2 exit: <= 2-ride routes fully enumerated
    if best is not None and best_scalar <= per_floor * 3 + 1e-9:
        return _materialize(best), best_scalar, True

    # ---- F2: second rides — cheapest total per (arrival platform, line).
    # closed2 = penalty (board ride 1) + fare1 + penalty (board ride 2).
    # Boarding ride 2 from any WALK-REACHABLE platform (same walk component
    # as ride 1's arrival — the walk is free and the ticket stays open)
    # closes ride 1 at fare1; per component keep the 2 cheapest DISTINCT
    # lines (the exclusion pattern again).
    comp_f1 = {}  # walk component -> [(fare1, line, origin, arrival)]
    for (t, line), (fare, o) in f1.items():
        comp = _walk_component.get(t) or frozenset((t,))
        comp_f1.setdefault(comp, []).append((fare, line, o, t))
    comp_top2 = {}
    for comp, lst in comp_f1.items():
        lst.sort()
        top = []
        seen = set()
        for item in lst:
            if item[1] in seen:
                continue
            seen.add(item[1])
            top.append(item)
            if len(top) == 2:
                break
        comp_top2[comp] = top

    # f2[(u, line2)] = (total, fare2, line1, o, t, t2): the cheapest 2-ride
    # prefix arriving at u. Both the 3-ride and the 4-ride composition close
    # ride 2 at u, so the smallest total at the same (u, line2) dominates.
    f2 = {}
    for comp, top in comp_top2.items():
        if time.monotonic() > deadline:
            completed = False
            break
        for t2 in comp:
            for route2 in _stop_routes.get(t2, ()):
                cands = [x for x in top if x[1] != route2]
                if not cands:
                    continue
                reach2 = _ride_sweep(route2, t2)
                if reach2 is None:
                    continue
                for u in reach2:
                    fare2 = _ride_fare(t2, u)
                    for fare1, line1, o, t in cands:
                        total = fare1 + 2 * penalty + fare2
                        cur = f2.get((u, route2))
                        if cur is None or total < cur[0] - 1e-9:
                            f2[(u, route2)] = (
                                total, fare2, line1, o, t, t2)

    # ---- 3-ride composition: F2 arrival + B1
    for (u, line_m), entry in list(f2.items()):
        if time.monotonic() > deadline:
            completed = False
            break
        total2, _fare2, line1, o, t, t2 = entry
        for u2 in _walk_platforms(u):
            pair = b1_best(u2, line_m)
            if pair is None:
                continue
            val, b1_desc = pair
            consider(total2 + val + penalty,
                     [(line1, o, t), (line_m, t2, u), b1_desc])

    # ---- B2 (lazy): cheapest 2-ride suffix from a boarding platform.
    # Value = fare3 + penalty + fare4 + penalty. Cached per (platform, line).
    b2_cache = {}
    b2_floor = 2 * _BASE_FARE + 2 * penalty  # cheapest conceivable B2

    def b2_line(s2, line3):
        """Cheapest ride3(line3 from s2)+ride4 suffix to the destination."""
        key = (s2, line3)
        cached = b2_cache.get(key)
        if cached is not None:
            return cached if cached != 'none' else None
        reach = _ride_sweep(line3, s2)
        result = None
        if reach is not None:
            best_val = float('inf')
            best_rides = None
            for m in reach:
                fare3 = _ride_fare(s2, m)
                base3 = fare3 + penalty
                for m2 in _walk_platforms(m):
                    for line4 in _stop_routes.get(m2, ()):
                        if line4 == line3:
                            continue
                        entry = b1.get((m2, line4))
                        if entry is None:
                            continue
                        fare4, e = entry
                        cand_val = base3 + fare4 + penalty
                        if cand_val < best_val - 1e-9:
                            best_val = cand_val
                            best_rides = [(line3, s2, m), (line4, m2, e)]
            if best_rides is not None:
                result = (best_val, best_rides)
        b2_cache[key] = result if result is not None else 'none'
        return result

    # ---- 4-ride composition: F2 arrival + B2 (lazy, gated, sorted)
    f2_sorted = sorted(f2.items(), key=lambda kv: kv[1][0])
    for (u, line_m), entries in f2_sorted:
        total2 = entries[0]
        # sorted: once the cheapest conceivable 4-ride completion from this
        # entry cannot improve, no later entry can either
        if total2 + penalty + b2_floor >= best_scalar - 1e-9:
            break
        if time.monotonic() > deadline:
            completed = False
            break
        for u2 in _walk_platforms(u):
            if time.monotonic() > deadline:
                completed = False
                break
            for line3 in _stop_routes.get(u2, ()):
                if line3 == line_m:
                    continue
                b2 = b2_line(u2, line3)
                if b2 is None:
                    continue
                val, b2_rides = b2
                _total2, _fare2, line1, o, t, t2 = entries
                consider(total2 + val,
                         [(line1, o, t), (line_m, t2, u)]
                         + list(b2_rides))

    return _materialize(best), best_scalar, completed


def _min_ride_route(start_ids, end_ids, max_rides=20):
    """Minimum-ride-count route between two platform sets (BFS).

    Used only when the <=4-ride enumeration PROVED that no route with at
    most 4 rides exists. Every route then has >=5 rides: each real ride
    costs at least the 4 zł base fare, so the daily cap (20 zł) is reached
    and the cheap objective ties on every route; the convenient objective
    additionally pays the boarding penalty per ride, so the minimum number
    of rides is optimal for both.

    Returns a path_with_edges list, or None when the destination is
    unreachable.
    """
    end_set = set(end_ids)
    end_comps = set()
    for e in end_ids:
        comp = _walk_component.get(e)
        end_comps.add(comp if comp is not None else frozenset((e,)))

    def _is_goal(stop):
        if stop in end_set:
            return True
        comp = _walk_component.get(stop)
        return comp is not None and comp in end_comps

    parent = {}
    frontier = []
    for s in start_ids:
        if s not in adjacency or s in parent:
            continue
        parent[s] = None
        frontier.append(s)
        if _is_goal(s):
            return [(s, None, None)]
    if not frontier:
        return None

    visited = set(frontier)
    for _level in range(max_rides):
        if _check_cancelled():
            return None
        next_frontier = []
        for arrival in frontier:
            if _check_cancelled():
                return None
            # Walks inside the stop group are free: every platform of the
            # component is a valid boarding point for the next ride.
            for board in _walk_platforms(arrival):
                for line in _stop_routes.get(board, ()):
                    reach = _ride_sweep(line, board)
                    if not reach:
                        continue
                    for stop in reach:
                        if stop in visited:
                            continue
                        visited.add(stop)
                        parent[stop] = (arrival, line, board)
                        if _is_goal(stop):
                            return _rebuild_min_ride(parent, stop)
                        next_frontier.append(stop)
        frontier = next_frontier
        if not frontier:
            break
    return None


def _rebuild_min_ride(parent, goal_stop):
    """Rebuild a path from the BFS parent map (one ride per parent hop)."""
    rides = []
    cur = goal_stop
    while parent.get(cur) is not None:
        prev, line, board = parent[cur]
        rides.append((prev, line, board, cur))
        cur = prev
    rides.reverse()

    path = [(cur, None, None)]
    for _prev, line, board, stop in rides:
        if cur != board:
            walk = _walk_fragment(cur, board)
            if walk is None:
                return None
            path += walk
        dist, parents = _ride_sweep_with_parents(line, board)
        if dist is None or stop not in dist:
            return None
        path += _ride_fragment(parents, board, stop)
        cur = stop
    return path


def _materialize(best):
    """Rebuild path_with_edges from a ride list [(line, frm, to), ...] by
    re-sweeping each winning ride once (walks between rides are added from
    the adjacency transfer edges)."""
    if not best:
        return None
    path = [(best[0][1], None, None)]
    cur = best[0][1]
    for line, frm, to in best:
        if frm != cur:
            walk = _walk_fragment(cur, frm)
            if walk is None:
                return None
            path += walk
        dist, parents = _ride_sweep_with_parents(line, frm)
        if dist is None or to not in dist:
            return None
        path += _ride_fragment(parents, frm, to)
        cur = to
    return path


def _find_exact_fare_route(start_ids, end_ids, upper_bound, boarding_penalty_zl,
                           max_seconds, cache_prefix, bound_route=None):
    """Exact fare search driver under the CPU gate, with caching.

    Iterative deepening on the number of boardings — this is what keeps the
    search EXACT yet fast. Every ride costs at least the base fare (the
    official straight-line fare floor), so a route with j boardings costs
    at least
        PER_SEGMENT_FLOOR * j   (4 zł cheap, 6 zł convenient incl. penalty).

    The search runs with a cap of k = 1, 2, 3... boardings; each cap bounds
    the combinatorial explosion of transfer chains, so low-k searches are
    cheap. After a cap-k search finds a best route with scalar cost f*:
      * if PER_SEGMENT_FLOOR * (k+1) >= f*, every route with more boardings
        costs more than f* -> f* is the CERTIFIED global optimum; done.
      * else if PER_SEGMENT_FLOOR * (k+1) >= upper_bound (a real route's
        cost) and nothing with <= k boardings beat it -> bound_route is the
        certified optimum; return it.
      * else escalate to k+1.

    On budget exhaustion mid-deepening the result is NOT certified, so an
    error is returned (exact-only product — no approximate results).
    """
    global _cheap_search_count, _cheap_timeout_count
    cache_key = (cache_prefix, tuple(start_ids), tuple(end_ids))
    cached = _cache_get_find(cache_key)
    if cached is not None:
        return cached
    with _counter_lock:
        _cheap_search_count += 1

    if not start_ids or not end_ids:
        return None, "Nie podano przystanków"

    # CPU gate: at most 2 exact fare searches run concurrently (GIL).
    _CHEAP_SEARCH_GATE.acquire()
    try:
        deadline = time.monotonic() + max_seconds
        # Minimum scalar cost of any route with j boardings.
        per_segment_floor = _BASE_FARE + boarding_penalty_zl

        # --- fast bound enumeration (<= 4 rides, whole-ride composition).
        # For most pairs this either CERTIFIES the optimum outright (every
        # route with 5+ rides costs >= per_segment_floor * 5) or provides a
        # tight upper bound that shrinks the search space by an order of
        # magnitude. It never affects exactness: its route is a real route,
        # and the floor argument holds regardless of completeness.
        enum_frag, enum_scalar, enum_completed = _enumerate_ride_bound(
            start_ids, end_ids, boarding_penalty_zl, deadline,
            upper_bound=upper_bound)
        if enum_frag is None and enum_completed and bound_route is not None:
            # Complete <=4-ride coverage with nothing under the upper bound:
            # certify the bound route directly when no unseen route can
            # display less. Every <=4-ride route has scalar >= upper (else
            # it would have been recorded), so those display >= F4 below;
            # every zero-free 5+-ride route has uncapped fare >= 5*base
            # (reaches the daily cap) hence displays >= F5; routes WITH
            # zero-distance rides are weakly dominated (dropping such a
            # ride keeps connectivity at <= fare and <= penalty).
            # The bound attains the upper, so if its displayed scalar
            # beats both floors, it is the certified global optimum —
            # no A* deepening needed (this is what saves 5+-ride pairs
            # like Mydlniki->Kepna, whose short route already caps at 20).
            bound_fare = sum(seg.get('cost_regular', 0.0)
                             for seg in bound_route.get('segments', []))
            bound_nr = len(bound_route.get('segments', []))
            bound_displayed = min(bound_fare, MAX_DAILY_COST_REGULAR) + \
                boarding_penalty_zl * bound_nr
            floor4 = min(upper_bound, MAX_DAILY_COST_REGULAR +
                         boarding_penalty_zl)
            floor5 = MAX_DAILY_COST_REGULAR + 5 * boarding_penalty_zl
            if _route_scalar(bound_route, boarding_penalty_zl) \
                    <= upper_bound + 1e-9 and \
                    bound_displayed <= min(floor4, floor5) + 1e-9:
                result = bound_route, None
                _cache_put_find(cache_key, result)
                return result
        if enum_frag is not None:
            enum_result = _build_route_result(enum_frag)
            # Certify on the DISPLAYED scalar (daily-capped fare + penalty
            # per ride): the enumeration covers ALL routes with <= 4 rides,
            # and every 5-ride route displays at least per_segment_floor * 5
            # (its uncapped fare alone reaches the daily cap), so the check
            # below always holds once the <= 4-ride enumeration completed.
            enum_scalar = (enum_result['cost_regular']
                           + boarding_penalty_zl
                           * len(enum_result.get('segments', [])))
            if per_segment_floor * 5 >= enum_scalar - 1e-9:
                result = enum_result, None
                _cache_put_find(cache_key, result)
                return result
            if enum_scalar < upper_bound:
                upper_bound = enum_scalar
                bound_route = enum_result

        # No route with <=4 rides exists (the enumeration completed empty):
        # every route then has >=5 rides, each real ride costs at least the
        # 4 zł base fare, so the 20 zł daily cap applies to every route and
        # the minimum-ride route is optimal for BOTH objectives.
        if enum_frag is None and enum_completed:
            min_frag = _min_ride_route(start_ids, end_ids)
            if min_frag is not None:
                result = _build_route_result(min_frag), None
                _cache_put_find(cache_key, result)
                return result

        best = None          # (result_dict, scalar) of the best route so far
        best_scalar = float('inf')
        k = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with _counter_lock:
                    _cheap_timeout_count += 1
                return None, "Timeout: nie znaleziono trasy w wymaganym czasie"
            k += 1
            res, err = _find_exact_fare_route_capped(
                start_ids, end_ids, upper_bound, boarding_penalty_zl, k,
                max_seconds=remaining)
            if res is not None:
                scalar = _route_scalar(res, boarding_penalty_zl)
                if scalar < best_scalar - 1e-9:
                    best, best_scalar = res, scalar
            if best is not None and per_segment_floor * (k + 1) >= best_scalar - 1e-9:
                # Certified: routes with > k boardings cannot beat best.
                result = best, None
                _cache_put_find(cache_key, result)
                return result
            if per_segment_floor * (k + 1) > upper_bound + 1e-9:
                # Every route with more boardings is STRICTLY worse than the
                # bound route, and none with <= k beat it (the cap-k search
                # is exact under the bound) -> bound_route IS the certified
                # optimum. (With >=, tying routes would be cut off before a
                # cap-k search could find them.)
                if bound_route is not None:
                    result = bound_route, None
                    _cache_put_find(cache_key, result)
                    return result
                return None, "Nie znaleziono trasy między tymi przystankami"
            # else: a cheaper route with more boardings may exist — escalate.
    finally:
        _CHEAP_SEARCH_GATE.release()


def _route_scalar(result, boarding_penalty_zl):
    """Scalar objective of a built route result (uncapped fare + penalty)."""
    fare = sum(seg.get('cost_regular', 0.0)
               for seg in result.get('segments', []))
    return fare + boarding_penalty_zl * len(result.get('segments', []))


def _find_exact_fare_route_capped(start_ids, end_ids, upper_bound,
                                  boarding_penalty_zl, max_boardings,
                                  max_seconds):
    """One exact fare search limited to routes with <= max_boardings rides.

    Dijkstra over (stop, route, board_stop, alight_stop, boardings):
    `closed` is the sum of COMPLETED ride fares plus boarding penalties; the
    currently open ride is priced only when it closes (or at the goal), by
    the OFFICIAL straight-line distance between its boarding and alighting
    stop GROUPS. Every transition adds a non-negative amount, and a lower
    `closed` at the same state always dominates, so settled labels are final.

    Returns (result_dict, None) or (None, error_string); NOT cached (the
    driver caches only the certified optimum).
    """
    end_set = set(end_ids)
    # Walk components containing a destination platform: reaching one of
    # these on foot means no further fare is needed.
    end_comps = set()
    for e in end_ids:
        comp = _walk_component.get(e)
        end_comps.add(comp if comp is not None else frozenset((e,)))

    best_total = upper_bound
    best_result = None
    best_closed = {}   # state -> lowest closed cost
    prev = {}          # state -> (parent_state, edge)
    pq = []
    seq = 0

    def _is_goal(stop):
        if stop in end_set:
            return True
        comp = _walk_component.get(stop)
        return comp is not None and comp in end_comps

    def _h(stop):
        # Any further non-walk move costs at least the base fare (the open
        # ride is not part of `closed`), unless the destination is in the
        # current walk component.
        return 0.0 if _is_goal(stop) else _BASE_FARE

    def _push(closed, stop, route, board, alight, boardings, parent):
        nonlocal seq
        key = (stop, route, board, alight, boardings)
        if closed >= best_closed.get(key, float('inf')) - 1e-9:
            return
        best_closed[key] = closed
        prev[key] = parent
        heapq.heappush(pq, (closed + _h(stop), closed, seq,
                            stop, route, board, alight, boardings, key))
        seq += 1

    for start_id in start_ids:
        if start_id not in adjacency:
            continue
        root = (start_id, None, None, None, 0)
        if 0.0 >= best_closed.get(root, float('inf')) - 1e-9:
            continue
        best_closed[root] = 0.0
        heapq.heappush(pq, (_h(start_id), 0.0, seq,
                            start_id, None, None, None, 0, root))
        seq += 1

    if not pq:
        return None, "Przystanek początkowy nie został znaleziony w grafie"

    start_time = time.monotonic()
    iterations = 0

    while pq:
        if _check_cancelled():
            return None, "Anulowano: wyszukiwanie przerwane"

        f_val, closed, _, stop, route, board, alight, boardings, key = \
            heapq.heappop(pq)

        # Stale label (a better one was found after this was queued).
        if closed > best_closed.get(key, float('inf')) + 1e-9:
            continue
        # Lower bound on any completion from this state.
        if best_result is not None and f_val > best_total + 1e-9:
            break

        iterations += 1
        if iterations % 1000 == 0:
            if time.monotonic() - start_time > max_seconds:
                return None, "Timeout: nie znaleziono trasy w wymaganym czasie"
            try:
                with open('/proc/self/statm') as f:
                    pages = int(f.read().split()[1])  # [1] = RSS, not [0]
                mem_mb = pages * 4 // 1024
            except Exception:
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            if mem_mb > _MEMORY_LIMIT_MB:
                return None, "Serwer jest przeciążony. Spróbuj ponownie za chwilę."
        if iterations >= _ASTAR_MAX_ITERATIONS:
            return None, "Serwer jest przeciążony. Spróbuj ponownie za chwilę."

        # Goal: close the open ride at this stop (the stop may be another
        # platform of the destination group, reachable on foot).
        if _is_goal(stop):
            total = closed
            if route is not None:
                total += _ride_fare(board, alight)
            if total > upper_bound + 1e-9:
                continue
            if best_result is None or total < best_total - 1e-9:
                best_total = total
                path_with_edges = []
                cur = key
                while cur in prev:
                    parent_key, edge = prev[cur]
                    path_with_edges.append((cur[0], cur[1], edge))
                    cur = parent_key
                path_with_edges.append((cur[0], None, None))
                path_with_edges.reverse()
                best_result = _build_route_result(path_with_edges)

        for edge in adjacency.get(stop, ()):
            next_stop = edge['to']
            next_route = edge['route_id']

            if next_route == 'transfer':
                # Free platform walk: the ticket stays open.
                new_closed = closed
                new_route = route
                new_board = board
                new_alight = alight
                new_boardings = boardings
            elif route is not None and next_route == route:
                # Continue the current ride.
                new_closed = closed
                new_route = route
                new_board = board
                new_alight = next_stop
                new_boardings = boardings
            else:
                # Close the open ride and board a different line.
                new_closed = closed + boarding_penalty_zl
                if route is not None:
                    new_closed += _ride_fare(board, alight)
                new_route = next_route
                new_board = stop
                new_alight = next_stop
                new_boardings = boardings + 1
                if new_boardings > max_boardings:
                    continue

            # `closed` is a lower bound on the final displayed fare.
            if new_closed > upper_bound + 1e-9:
                continue
            _push(new_closed, next_stop, new_route, new_board, new_alight,
                  new_boardings, (key, edge))

    if best_result is not None:
        return best_result, None
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
            transfer_from = None
            j = i - 1
            while j >= 0:
                if path_with_edges[j][1] != 'transfer':
                    transfer_from = path_with_edges[j][1]
                    break
                j -= 1
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
    # The FARE, however, uses the official tariff distance: the straight
    # line between the ride's boarding and alighting platforms.
    raw_billed = []
    for segment in segments:
        segment['distance'] = round(segment['distance'], 4)

        first_stop_id = (segment['stops'][0] if segment['stops'] else None)
        last_stop_id = (segment.get('end_stop')
                        or (segment['stops'][-1] if segment['stops'] else None))
        billed_km = _straight_line_km(first_stop_id, last_stop_id)
        if billed_km is None:
            billed_km = segment['distance']
        raw_billed.append(billed_km)
        segment['billed_distance'] = round(billed_km, 4)

        seg_reg, seg_red = calculate_cost(billed_km)
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

    # Total distance: sum of real (GTFS) route distances — this is what the
    # map draws. The tariff distance is the sum of per-ride straight lines.
    total_distance = sum(seg.get('distance', 0.0) for seg in segments)
    total_distance = round(total_distance, 4)
    total_billed_distance = round(sum(raw_billed), 4)

    cost_regular, cost_reduced = calculate_route_cost(segments)
    # Interpretation B for the same route: whole journey as one ticket.
    # Its distance is the straight line from the first boarding platform to
    # the last alighting platform (hypothetical merged-transfer tariff).
    journey_distance = 0.0
    if segments:
        journey_km = _straight_line_km(segments[0]['stops'][0],
                                       segments[-1].get('end_stop'))
        if journey_km is None:
            journey_km = total_billed_distance
        journey_distance = round(journey_km, 4)
    cost_b_regular, cost_b_reduced = calculate_single_ticket_cost(
        journey_distance)

    total_time = sum(seg.get('time', 0) for seg in segments)
    total_time += TRANSFER_TIME_SECONDS * len(transfers)

    return {
        'total_distance': total_distance,
        'total_billed_distance': total_billed_distance,
        'journey_distance': journey_distance,
        'total_time': total_time,
        'cost_regular': cost_regular,
        'cost_reduced': cost_reduced,
        'cost_b_regular': cost_b_regular,
        'cost_b_reduced': cost_b_reduced,
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
# Number of routes available in the sqlite disk cache (metrics only).
_route_cache_db_entries = 0
# Entries computed fresh (not loaded from disk) during this server run.
# Cumulative — eviction may later remove some of them from the cache.
_route_cache_computed = 0
# In-flight searches, keyed by (from_group_id, to_group_id): while a request
# is computing a pair, concurrent identical requests wait for its result
# instead of re-running the expensive A* (10× the same pair = 1 search).
_route_inflight = {}
# How long an in-flight waiter waits for the producer before recomputing.
_INFLIGHT_MAX_WAIT_SECONDS = 60.0

# Disk persistence: every computed pair is written through to a sqlite
# database, processed/route_cache_<feed>_<algo>.sqlite. The in-memory dict
# is only the hot set; evicted entries live on in the database, and the
# whole cache survives restarts. The database filename embeds the GTFS feed
# version and the cache algo version — a GTFS update starts a fresh file.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'processed')
# Bump when the computed result format/semantics changes — old disk caches
# become unreadable under a new name and stale entries never leak back in
# (e.g. v4: exact-only search — convenient is the exact fare+penalty
#  optimum, no greedy/heuristic results, no fallbacks;
#  v5: cache stores the two user-facing routes only (no internal short),
#  strips derivable stop_positions, and moves persistence to sqlite;
#  v6: results carry Interpretation B fares (cost_b_regular/reduced);
#  v7: official straight-line tariff distance/pricing, billed_distance
#  fields, corrected 4-ride enumeration;
#  v8: ride fares resolved on stop-group centroids (search/display agree),
#  min-ride fallback for 5+-ride pairs;
#  v9: single transfer entry after multi-hop walks, exact billed total).
_CACHE_ALGO_VERSION = 'v9'

_sqlite_conn = None
_SQLITE_LOCK = threading.Lock()


def _cache_db_path():
    ver = str(_feed_version) if _feed_version else 'unknown'
    return os.path.join(_CACHE_DIR,
                        f'route_cache_{ver}_{_CACHE_ALGO_VERSION}.sqlite')


def _open_cache_db():
    """Open (and create) the sqlite route cache. WAL + NORMAL synchronous:
    durable across process crashes without per-write fsync storms on the
    1-core VPS."""
    global _sqlite_conn
    try:
        conn = sqlite3.connect(_cache_db_path(), check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('CREATE TABLE IF NOT EXISTS routes ('
                     'k TEXT PRIMARY KEY, v TEXT NOT NULL, size INTEGER NOT NULL)')
        conn.commit()
        _sqlite_conn = conn
        _cleanup_stale_cache_files()
    except Exception:
        _sqlite_conn = None  # cache is best-effort; searches must not fail
    return _sqlite_conn


def _close_cache_db():
    global _sqlite_conn
    if _sqlite_conn is not None:
        try:
            _sqlite_conn.close()
        except Exception:
            pass
        _sqlite_conn = None


def _cache_db_key(from_id, to_id):
    return f'{from_id}|{to_id}|{_feed_version}|{_CACHE_ALGO_VERSION}'


def _db_put(key, payload_json, size):
    """Write-through one pair (autocommit — durable across restarts)."""
    conn = _sqlite_conn
    if conn is None:
        return
    try:
        with _SQLITE_LOCK:
            conn.execute('INSERT OR REPLACE INTO routes(k, v, size) '
                         'VALUES (?, ?, ?)', (key, payload_json, size))
            conn.commit()
    except Exception:
        pass  # best-effort — the memory cache still serves this process


def _db_get(key):
    conn = _sqlite_conn
    if conn is None:
        return None
    try:
        with _SQLITE_LOCK:
            row = conn.execute('SELECT v FROM routes WHERE k = ?',
                               (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _db_count():
    conn = _sqlite_conn
    if conn is None:
        return 0
    try:
        with _SQLITE_LOCK:
            return conn.execute('SELECT COUNT(*) FROM routes').fetchone()[0]
    except Exception:
        return 0


def _cleanup_stale_cache_files():
    """Remove route-cache files from older feed / algo versions."""
    try:
        current = os.path.basename(_cache_db_path())
        for name in os.listdir(_CACHE_DIR):
            if (name.startswith('route_cache_')
                    and (name.endswith('.json') or name.endswith('.sqlite'))
                    and name != current):
                os.remove(os.path.join(_CACHE_DIR, name))
    except OSError:
        pass  # best-effort — stale files are harmless


def find_route_between_groups(from_group_id, to_group_id, mode='both'):
    """Find the routes between two stop groups.

    Computes two user-facing results, each EXACT (no heuristic, no
    fallbacks — a mode whose exact search cannot finish in its budget is
    returned as an error):

    * ``'convenient'``   — exact minimum of "fare + boarding penalty per
      ride" (few transfers without ignoring overpriced detours)
    * ``'cheap'``        — exact minimum ticket fare (each ride is a separate
      ticket priced from zero on its OFFICIAL straight-line distance)

    The distance-based ``'short'`` search also runs internally (exact) to
    provide upper bounds that speed up the exact fare searches; it is not a
    user-facing mode.

    Returns a pair ``(convenient_pair, cheap_pair)``; each is a
    ``(result_dict, error_string)`` pair.

    Results are cached per (from_group_id, to_group_id, feed_version) —
    in memory (hot set) and in the sqlite disk tier (everything, surviving
    restarts and evictions). Transient (timeout) failures are not cached.
    """
    cache_key = (from_group_id, to_group_id, 'both', _feed_version)

    pair, _from_db = _read_cached_pair(cache_key, mode,
                                       from_group_id, to_group_id)
    if pair is not None:
        return pair

    # Another request computing this exact pair — wait for its result
    # (keeps 10 concurrent identical queries down to a single search run).
    inflight = _route_inflight.get((from_group_id, to_group_id))
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
        pair, _from_db = _read_cached_pair(cache_key, mode,
                                           from_group_id, to_group_id)
        if pair is not None:
            return pair
        # Producer lost without storing a result — fall through and compute

    # Register as the in-flight search for this pair (dedup for concurrent
    # identical requests), compute, then store + notify waiters.
    pair_key = (from_group_id, to_group_id)
    inflight = {'cond': threading.Condition(), 'done': False}
    with _route_cache_lock:
        _route_inflight[pair_key] = inflight
    try:
        return _compute_route_internal(cache_key, from_group_id, to_group_id)
    finally:
        with _route_cache_lock:
            inflight['done'] = True
            _route_inflight.pop(pair_key, None)
        with inflight['cond']:
            inflight['cond'].notify_all()


# ============================================================
# Group-to-group computation (dual-mode, exact)
# ============================================================

def _compute_route_internal(cache_key, from_group_id, to_group_id):
    """Compute the dual-mode result for a group pair (no cache/dedup).

    Every user-facing mode is an EXACT result of its own proven-optimal
    search, or an error when that search could not finish within its budget:

      * convenient — exact minimum of "fare + CONVENIENT_BOARDING_PENALTY_ZL
        per boarding" (few rides, no overpriced detours),
      * cheap      — exact minimum fare.

    There are NO fallbacks: a timed-out mode is returned as (None, error).
    The distance-based short search runs only to provide upper bounds that
    speed up the exact fare searches (it is not user-facing).
    """
    from_group = stops_grouped.get(from_group_id)
    to_group = stops_grouped.get(to_group_id)

    if not from_group:
        err = ((None, "Przystanek początkowy nie został znaleziony"),
               (None, "Przystanek początkowy nie został znaleziony"))
        _cache_store(cache_key, err)
        return err
    if not to_group:
        err = ((None, "Przystanek końcowy nie został znaleziony"),
               (None, "Przystanek końcowy nie został znaleziony"))
        _cache_store(cache_key, err)
        return err

    # Same group — zero-distance trip
    if from_group_id == to_group_id:
        cost_reg, cost_red = calculate_cost(0)
        cost_b_reg, cost_b_red = calculate_single_ticket_cost(0)
        result = ({
            'total_distance': 0,
            'total_billed_distance': 0,
            'journey_distance': 0,
            'total_time': 0,
            'cost_regular': cost_reg,
            'cost_reduced': cost_red,
            'cost_b_regular': cost_b_reg,
            'cost_b_reduced': cost_b_red,
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
        return (result, result)

    from_platforms = from_group['platforms'][:_MAX_PLATFORMS_TO_TRY]
    to_platforms = to_group['platforms'][:_MAX_PLATFORMS_TO_TRY]
    from_ids = [p['id'] for p in from_platforms]
    to_ids = [p['id'] for p in to_platforms]

    last_error = None

    def _raw_fare(r):
        # UNCAPPED sum of segment fares — cost_regular is capped at the daily
        # limit (20 zł) and would not bound anything for long routes.
        if r is None:
            return float('inf')
        return sum(seg.get('cost_regular', 0.0)
                   for seg in r.get('segments', []))

    # --- short (exact, distance-based): internal upper-bound provider only.
    # NOTE: Do NOT use a nested ThreadPoolExecutor here — on timeout,
    # run_pathfinding_with_timeout sets a cancel event and the outer
    # executor frees the worker.  A nested pool's shutdown(wait=True)
    # would block the worker from returning, permanently draining the
    # executor pool until a server restart.
    best_short = None
    if not _check_cancelled():
        result, error = find_shortest_path_multi(from_ids, to_ids)
        if result is not None:
            best_short = result
        elif last_error is None:
            last_error = error

    # If we were cancelled mid-short-search, skip remaining phases.
    if _check_cancelled():
        err = last_error or "Anulowano wyszukiwanie"
        return ((None, err), (None, err))

    # --- convenient (EXACT: fare + penalty per boarding). The short route's
    # scalar cost is a valid complete-route cost — seeding the upper bound
    # with it prunes the search but never affects optimality, and lets the
    # iterative deepening certify the short route itself as the optimum.
    conv_upper = float('inf')
    if best_short is not None:
        conv_upper = (_raw_fare(best_short)
                      + _BOARDING_PENALTY * len(best_short.get('segments', [])))
    best_convenient, convenient_error = find_most_convenient_path(
        from_ids, to_ids, upper_bound=conv_upper,
        bound_route=best_short)

    # --- cheap (EXACT: fare). Upper bound = cheapest known complete route
    # (any real route's fare bounds the optimum from above).
    cheap_upper = min(_raw_fare(best_short), _raw_fare(best_convenient))
    cheap_bound_route = (best_short
                         if _raw_fare(best_short) <= _raw_fare(best_convenient)
                         else best_convenient)
    best_cheap = None
    cheap_error = None
    if cheap_upper <= _BASE_FARE + 1e-9:
        # A route costing the base fare already exists and nothing can be
        # cheaper than a single base-priced ticket — that route IS the exact
        # optimum (mathematical shortcut, not a fallback).
        best_cheap = cheap_bound_route
    else:
        best_cheap, cheap_error = find_cheapest_path(
            from_ids, to_ids, upper_bound=cheap_upper,
            bound_route=cheap_bound_route)

    if best_convenient is None and best_cheap is None:
        err_msg = (convenient_error or cheap_error or last_error
                   or "Nie znaleziono trasy między tymi przystankami")
        # Do NOT cache all-error results — a transient error (memory,
        # timeout) would poison the cache until eviction.
        return ((None, err_msg), (None, err_msg))

    # Build the dual-mode return. The internal short route is not part of
    # the result — only the two user-facing modes are served.
    convenient_pair = ((best_convenient, None) if best_convenient
                       else (None, convenient_error or last_error))
    cheap_pair = ((best_cheap, None) if best_cheap
                  else (None, cheap_error or last_error))

    pair = (convenient_pair, cheap_pair)

    # Never cache results whose user-facing search failed transiently
    # (timeout / cancel / memory) — a retry must recompute instead of being
    # served the stored error for the cache's lifetime.
    if (_has_transient_error(convenient_pair)
            or _has_transient_error(cheap_pair)):
        return pair

    # Write-through to the sqlite disk tier + keep in the memory hot set.
    # The stored payload is slimmed (no internal short route, no derivable
    # stop_positions) — hydrate_slice rebuilds it on read.
    payload = _pair_payload(pair)
    _db_put(_cache_db_key(from_group_id, to_group_id),
            json.dumps(payload), _estimate_bytes(payload))
    # Keep the SLIMMED payload in the hot set too: _read_cached_pair()
    # hydrates fresh copies, and duplicate stop_positions are the bulk of
    # an entry. (Storing the full pair here made the memory tier ~2x the
    # disk tier and broke the "stored payload is slimmed" invariant.)
    _cache_store(cache_key, payload)
    return pair


def _hydrate_result(result):
    """Rebuild the per-segment fields the cache strips to save memory
    (stop_positions = exact per-stop coordinates, derivable from
    stops_by_id). Returns a FRESH dict — the cached original is shared
    with every concurrent reader and must never be mutated."""
    if result is None:
        return None
    positions = {}
    res = dict(result)
    segs = []
    for seg in result.get('segments', []):
        seg = dict(seg)
        stops = []
        pos = {}
        for sid in seg.get('stops', ()):
            stops.append(sid)
            info = positions.get(sid)
            if info is None:
                info = stops_by_id.get(sid, {})
                info = [info.get('lat', 0), info.get('lon', 0)]
                positions[sid] = info
            pos[sid] = info
        seg['stops'] = stops
        seg['stop_positions'] = pos
        segs.append(seg)
    res['segments'] = segs
    return res


def _hydrate_slice(pair, mode):
    """Hydrate the pair slice for *mode* (fresh copies, cache untouched)."""
    if mode == 'convenient':
        one = pair[0]
    elif mode == 'cheap':
        one = pair[1]
    else:
        return ((_hydrate_result(pair[0][0]), pair[0][1]),
                (_hydrate_result(pair[1][0]), pair[1][1]))
    return _hydrate_result(one[0]), one[1]


def _pair_payload(pair):
    """Slim the pair for caching: the internal short route is not stored
    (the API never serves it) and stop_positions are dropped (rehydrated
    from stops_by_id on read). Cuts entry size by ~45%."""
    def slim(pair_one):
        result, err = pair_one
        if result is None:
            return [None, err]
        res = dict(result)
        res['segments'] = [
            {k: v for k, v in seg.items() if k != 'stop_positions'}
            for seg in result.get('segments', [])]
        return [res, err]
    return [slim(pair[0]), slim(pair[1])]


def _cache_from_payload(payload):
    """Rebuild the pair from its stored payload (JSON round-trip safe)."""
    return (payload[0], payload[1])


def _cache_store(cache_key, pair):
    """Insert into the in-memory hot set with byte-budget eviction. The
    sqlite copy (written through at compute time) is unaffected by eviction
    — evicted entries can always be read back."""
    global _route_cache_bytes, _route_cache_computed
    size = _estimate_bytes(pair)
    if size > _ROUTE_CACHE_MAX_BYTES:
        return
    with _route_cache_lock:
        if cache_key in _route_cache:
            _route_cache_bytes -= _route_cache[cache_key][1]
        else:
            _route_cache_computed += 1  # brand-new pair this run
        _route_cache[cache_key] = (pair, size)
        _route_cache_bytes += size
        while _route_cache_bytes > _ROUTE_CACHE_MAX_BYTES and _route_cache:
            oldest = next(iter(_route_cache))  # insertion-ordered dict
            _route_cache_bytes -= _route_cache[oldest][1]
            del _route_cache[oldest]


def _read_cached_pair(cache_key, mode, from_id, to_id):
    """Memory hot set -> sqlite disk -> None. Returns (pair, from_db);
    both hit paths return HYDRATED fresh copies (cache stays immutable)."""
    with _route_cache_lock:
        cached = _route_cache.get(cache_key)
    if cached is not None:
        return _hydrate_slice(cached[0], mode), False
    payload = _db_get(_cache_db_key(from_id, to_id))
    if payload is None:
        return None, False
    pair = _cache_from_payload(payload)
    _cache_store(cache_key, pair)
    return _hydrate_slice(pair, mode), True


# Search failures a retry could overcome. Definitive results ("no route
# exists between these stops") are NOT transient and may be cached.
_TRANSIENT_ERROR_PREFIXES = ("Timeout:", "Anulowano", "Serwer jest przeciążony")


def _is_transient_error(error):
    """True for search failures a retry could overcome (timeout, cancel,
    memory pressure)."""
    if not error:
        return False
    return any(error.startswith(p) for p in _TRANSIENT_ERROR_PREFIXES)


def _has_transient_error(pair):
    """True when a (result, error) pair carries a transient failure."""
    return pair[0] is None and _is_transient_error(pair[1])


# ============================================================
# Cache diagnostics
# ============================================================

def find_cache_info():
    """Return (count, bytes_used, max_bytes) for the A* find cache."""
    return len(_find_cache), _find_cache_bytes, _FIND_CACHE_MAX_BYTES


def cheap_search_info():
    """Return (searches, timeouts) for the exact fare searches
    (cheap + convenient)."""
    with _counter_lock:
        return _cheap_search_count, _cheap_timeout_count


def route_cache_info():
    """Return (count, max_entries, bytes_used, max_bytes) for the
    group-to-group route cache."""
    return (len(_route_cache), _ROUTE_CACHE_MAX,
            _route_cache_bytes, _ROUTE_CACHE_MAX_BYTES)


def cache_db_size():
    """Size of the sqlite route-cache file on disk, in bytes (0 if missing)."""
    try:
        return os.path.getsize(_cache_db_path())
    except OSError:
        return 0


def sweep_cache_info():
    """Number of line sweeps currently in the shared LRU."""
    return len(_sweep_memo)


def route_cache_origin_info():
    """Return (db_routes, computed_this_run) for the route cache.

    db_routes — pairs available in the sqlite disk tier (survive restarts),
    computed_this_run — new pairs computed from real requests this run
    (cumulative; eviction may have removed some from memory since).
    """
    return max(_route_cache_db_entries, _db_count()), _route_cache_computed


# ============================================================
# Cached route access (OG images — read-only, never computes)
# ============================================================

def get_cached_route_result(from_group_id, to_group_id, mode):
    """Return the cached (result, error) pair for a pair+mode, or (None, None).

    READ-ONLY by contract: OG image generation must never trigger a search —
    it may only reuse a route the user has already had computed (memory hot
    set or sqlite disk tier). Anything else yields no cost preview.
    """
    cache_key = (from_group_id, to_group_id, 'both', _feed_version)
    with _route_cache_lock:
        cached = _route_cache.get(cache_key)
    if cached is not None:
        pair = cached[0]
    else:
        payload = _db_get(_cache_db_key(from_group_id, to_group_id))
        if payload is None:
            return None, None
        pair = _cache_from_payload(payload)
    if mode == 'convenient':
        one = pair[0]
    elif mode == 'cheap':
        one = pair[1]
    else:
        one = pair[0]
    return _hydrate_result(one[0]), one[1]


