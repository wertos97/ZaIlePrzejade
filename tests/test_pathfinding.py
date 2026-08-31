"""Unit tests for server.pathfinding module."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.pathfinding import haversine_km


def _walk_reachable(adj, start, goal):
    """True when goal is reachable from start via transfer (walk) edges."""
    seen = {start}
    queue = [start]
    while queue:
        s = queue.pop()
        for e in adj.get(s, []):
            if e['route_id'] != 'transfer':
                continue
            t = e['to']
            if t == goal:
                return True
            if t not in seen:
                seen.add(t)
                queue.append(t)
    return False


class TestHaversineKm(unittest.TestCase):
    """Test haversine distance calculation."""

    def test_same_point(self):
        """Same point should have zero distance."""
        d = haversine_km(50.0, 19.9, 50.0, 19.9)
        self.assertAlmostEqual(d, 0.0, places=6)

    def test_known_distance(self):
        """Kraków Main Square to Wawel ~0.5 km."""
        # Kraków Main Square: 50.0614, 19.9366
        # Wawel Castle: 50.0539, 19.9346
        d = haversine_km(50.0614, 19.9366, 50.0539, 19.9346)
        self.assertGreater(d, 0.3)
        self.assertLess(d, 1.5)

    def test_symmetry(self):
        """Distance should be symmetric."""
        d1 = haversine_km(50.0, 19.9, 50.1, 20.0)
        d2 = haversine_km(50.1, 20.0, 50.0, 19.9)
        self.assertAlmostEqual(d1, d2, places=6)

    def test_positive_distance(self):
        """Distance should always be positive."""
        d = haversine_km(50.0, 19.9, 51.0, 21.0)
        self.assertGreater(d, 0)

    def test_large_distance(self):
        """Kraków to Warsaw ~250-300 km."""
        # Kraków: 50.06, 19.94
        # Warsaw: 52.23, 21.01
        d = haversine_km(50.06, 19.94, 52.23, 21.01)
        self.assertGreater(d, 200)
        self.assertLess(d, 400)

    def test_returns_float(self):
        """Should return a float."""
        d = haversine_km(50.0, 19.9, 50.1, 20.0)
        self.assertIsInstance(d, float)


class TestPathfindingIntegration(unittest.TestCase):
    """Integration tests using real data."""

    @classmethod
    def setUpClass(cls):
        """Load real data once for all tests."""
        from server.data import adjacency, stops_by_id, stops_grouped, stop_to_group, routes_by_id, route_shapes
        from server.pathfinding import init_pathfinding

        init_pathfinding(adjacency, stops_by_id, stops_grouped, stop_to_group, routes_by_id, route_shapes)
        # Store module references directly
        import server.pathfinding as pf
        cls.pf = pf
        cls.stops_grouped = stops_grouped
        group_ids = list(stops_grouped.keys())
        cls.test_from = group_ids[0]
        cls.test_to = group_ids[min(5, len(group_ids) - 1)]

    def test_find_shortest_path_returns_tuple(self):
        """Should return a (result, error) tuple."""
        from_group = self.stops_grouped[self.test_from]
        to_group = self.stops_grouped[self.test_to]
        fp = from_group['platforms'][0]['id']
        tp = to_group['platforms'][0]['id']
        result = self.pf.find_shortest_path(fp, tp)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_find_route_between_groups_returns_pair(self):
        """Should return (convenient_pair, cheap_pair) — the internal short
        route is not part of the result."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='both')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        convenient_pair, cheap_pair = result
        self.assertIsInstance(convenient_pair, tuple)
        self.assertIsInstance(cheap_pair, tuple)

    def test_find_route_short_mode(self):
        """mode='short' should return a single (result, error) pair."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='short')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_find_route_cheap_mode_slice(self):
        """mode='cheap' returns a single (result, error) pair."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='cheap')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_cheap_is_no_more_expensive_than_convenient(self):
        """Exactness invariant (reported pair Skrajna→Elektrociepłownia):
        when both exact searches complete, cheap fare <= convenient fare.
        A timed-out cheap mode is null + transient error, never approximated."""
        # Real reported pair: Skrajna (group_341) → Elektrociepłownia
        # (group_1503) — long trip where the exact searches are slowest.
        pair_ids = ('group_341', 'group_1503')
        if pair_ids[0] not in self.stops_grouped or pair_ids[1] not in self.stops_grouped:
            self.skipTest('reported pair not present in this dataset')

        def raw_fare(r):
            if r is None:
                return float('inf')
            return sum(s.get('cost_regular', 0.0)
                       for s in r.get('segments', []))

        pair = self.pf.find_route_between_groups(*pair_ids, mode='both')
        conv_pair, cheap_pair = pair
        conv_result, _ = conv_pair
        cheap_result, cheap_err = cheap_pair
        if cheap_result is None:
            # No fallbacks: timeout is a legitimate outcome; the error must
            # be transient so a retry can succeed.
            self.assertTrue(self.pf._is_transient_error(cheap_err), cheap_err)
        else:
            if conv_result is not None:
                self.assertLessEqual(
                    raw_fare(cheap_result), raw_fare(conv_result),
                    'cheap route costs more than the convenient route')

    def test_cheap_is_no_more_expensive_than_convenient_real_pair(self):
        """(kept) exactness invariant on the reported hard pair: when both
        exact searches complete, cheap fare <= convenient fare."""
        pair_ids = ('group_341', 'group_1503')
        if pair_ids[0] not in self.stops_grouped:
            self.skipTest('reported pair not present in this dataset')
        triple = self.pf.find_route_between_groups(*pair_ids, mode='both')
        conv_pair, cheap_pair = triple
        cheap_result, cheap_err = cheap_pair
        conv_result, _ = conv_pair
        if cheap_result is None:
            self.assertTrue(self.pf._is_transient_error(cheap_err), cheap_err)
            return
        if conv_result is not None:
            self.assertLessEqual(cheap_result['cost_regular'],
                                 conv_result['cost_regular'])

    def test_find_route_completes_within_30s_budget(self):
        """The whole dual-mode search must fit the 30s product promise
        (phase budgets 8s short + 8s convenient + 10s cheap)."""
        import time as _time
        pair = ('group_341', 'group_1503')
        if pair[0] not in self.stops_grouped:
            self.skipTest('reported pair not present in this dataset')
        t0 = _time.monotonic()
        self.pf.find_route_between_groups(*pair, mode='both')
        elapsed = _time.monotonic() - t0
        self.assertLess(elapsed, 30.0,
                        f'dual-mode search took {elapsed:.1f}s (budget 30s)')

    def test_same_group_returns_zero_distance(self):
        """Route from a group to itself should have zero distance."""
        conv_pair, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_from, mode='both')
        result, error = conv_pair
        self.assertIsNotNone(result)
        self.assertEqual(result['total_distance'], 0)
        self.assertEqual(result['cost_regular'], 0.0)

    def test_route_path_has_unique_groups(self):
        """Regression: path must contain exactly ONE entry per stop group
        (no duplicate dots for the same stop), positioned at a real peron."""
        conv_pair, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = conv_pair
        self.assertIsNotNone(result, error)
        path = result['path']
        self.assertGreater(len(path), 1)
        groups = [s['group_id'] for s in path]
        self.assertEqual(len(groups), len(set(groups)),
                         'duplicate stop group in path')
        for stop in path:
            real = self.pf.stops_by_id.get(stop['stop_id'])
            self.assertIsNotNone(real, stop['stop_id'])
            self.assertAlmostEqual(stop['lat'], real['lat'], places=6,
                                   msg=f"lat for {stop['stop_id']} should be the real platform")
            self.assertAlmostEqual(stop['lon'], real['lon'], places=6,
                                   msg=f"lon for {stop['stop_id']} should be the real platform")

    def test_segment_carries_stop_positions(self):
        """Every segment exposes exact peron coords for ALL its stops, so the
        map can draw geometry even though path is deduplicated per group."""
        conv_pair, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = conv_pair
        self.assertIsNotNone(result, error)
        for seg in result['segments']:
            self.assertIn('stop_positions', seg)
            for sid in seg['stops']:
                self.assertIn(sid, seg['stop_positions'],
                              f"stop {sid} missing from segment stop_positions")

    def test_segment_distance_follows_gtfs_edges(self):
        """Regression: segment distance must equal the sum of the real GTFS
        edge distances (shape_dist_traveled) along the path — NOT the
        straight-line distance between platform coordinates."""
        conv_pair, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = conv_pair
        self.assertIsNotNone(result, error)
        adj = self.pf.adjacency
        total_from_edges = 0.0
        for seg in result['segments']:
            seg_edges = 0.0
            stops = seg['stops']
            for j in range(len(stops) - 1):
                a, b = stops[j], stops[j + 1]
                # find the forward edge a->b used on this route; a segment
                # may span a walking transfer (same line re-boarded after a
                # walk — the walk-merge ticket semantics), so accept the
                # transfer edge as the hop too
                dist = None
                for e in adj.get(a, []):
                    if e['to'] == b and e['route_id'] in (seg['route_id'], 'transfer'):
                        # walk hops cost 0 ticket distance (walk-merge)
                        dist = 0.0 if e['route_id'] == 'transfer' else e['distance']
                        break
                if dist is None:
                    # reverse edge (bidirectional adjacency) — same distance
                    for e in adj.get(b, []):
                        if e['to'] == a and e['route_id'] in (seg['route_id'], 'transfer'):
                            dist = 0.0 if e['route_id'] == 'transfer' else e['distance']
                            break
                if dist is None:
                    # multi-hop walk bridge between platforms — 0 km ticket distance
                    if _walk_reachable(adj, a, b):
                        dist = 0.0
                self.assertIsNotNone(dist, f'edge {a}->{b} not found')
                seg_edges += dist
            # accumulated real distance (round to 4 like the server)
            self.assertAlmostEqual(seg['distance'], round(seg_edges, 4), places=4,
                                   msg=f"segment {seg['route_id']} distance should follow GTFS edges")
            total_from_edges += seg_edges
        self.assertAlmostEqual(result['total_distance'], round(total_from_edges, 4), places=4,
                               msg='total_distance should equal sum of real GTFS segment distances')

    def test_invalid_group_returns_error(self):
        """Invalid group ID should return an error."""
        result = self.pf.find_route_between_groups('group_999999', self.test_to, mode='both')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        convenient_pair, cheap_pair = result
        self.assertIsNone(convenient_pair[0])  # result is None
        self.assertIsNotNone(convenient_pair[1])  # error is set
        self.assertIsNone(cheap_pair[0])


class TestRouteCacheSqlite(unittest.TestCase):
    """The route cache persists EVERY computed pair to sqlite (write-through)
    and the in-memory dict is only the hot set: evicted entries must still
    be readable from the database, and stored payloads are slimmed (no
    internal short route, no derivable stop_positions)."""

    @classmethod
    def setUpClass(cls):
        from server.data import adjacency, stops_by_id, stops_grouped, stop_to_group, routes_by_id, route_shapes
        from server.pathfinding import init_pathfinding

        init_pathfinding(adjacency, stops_by_id, stops_grouped, stop_to_group,
                         routes_by_id, route_shapes)
        import server.pathfinding as pf
        cls.pf = pf
        group_ids = list(stops_grouped.keys())
        cls.test_from = group_ids[0]
        cls.test_to = group_ids[min(5, len(group_ids) - 1)]

    def test_pair_roundtrip_through_db(self):
        """compute -> clear the memory hot set -> read again: the pair must
        come back from the sqlite tier with identical fares and hydrated
        stop_positions."""
        pf = self.pf
        pair = pf.find_route_between_groups(self.test_from, self.test_to,
                                            mode='both')
        conv, cheap = pair
        self.assertIsNotNone(conv[0])
        base_costs = (conv[0]['cost_regular'], cheap[0]['cost_regular'])

        # drop the memory hot set — the next read must hit sqlite
        with pf._route_cache_lock:
            pf._route_cache.clear()
        pair2 = pf.find_route_between_groups(self.test_from, self.test_to,
                                             mode='both')
        conv2, cheap2 = pair2
        self.assertIsNotNone(conv2[0])
        self.assertEqual(conv2[0]['cost_regular'], base_costs[0])
        self.assertEqual(cheap2[0]['cost_regular'], base_costs[1])
        # hydration rebuilt the stripped per-segment coordinates
        for route in (conv2[0], cheap2[0]):
            for seg in route['segments']:
                self.assertIn('stop_positions', seg)
                self.assertEqual(len(seg['stop_positions']),
                                 len(seg['stops']))

    def test_cached_payload_is_slimmed(self):
        """The stored pair must NOT contain the internal short route nor
        derivable stop_positions — that is what keeps entries small."""
        pf = self.pf
        pf.find_route_between_groups(self.test_from, self.test_to, mode='both')
        key = (self.test_from, self.test_to, 'both', pf._feed_version)
        with pf._route_cache_lock:
            pair = pf._route_cache[key][0]
        self.assertEqual(len(pair), 2)  # (convenient, cheap) — no short
        for one in pair:
            if one[0] is not None:
                for seg in one[0]['segments']:
                    self.assertNotIn('stop_positions', seg)

    def test_concurrent_db_writes(self):
        """Parallel route computations write through to sqlite without
        corrupting it (WAL + a write lock)."""
        import threading

        pf = self.pf
        ids = list(pf.stops_grouped.keys())[:12]
        errs = []

        def run(i):
            try:
                pf.find_route_between_groups(ids[i], ids[(i + 3) % 12],
                                             mode='both')
            except Exception as e:  # pragma: no cover
                errs.append(e)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
            self.assertFalse(t.is_alive(), 'search deadlocked')
        self.assertEqual(errs, [])
        # the db must still be readable and contain the pairs
        self.assertGreaterEqual(pf._db_count(), 8)


class TestExactSearchExactness(unittest.TestCase):
    """Exact-only product: the Pareto searches must return the TRUE optimum.

    Runs find_cheapest_path / find_most_convenient_path on a tiny synthetic
    graph and compares against brute-force enumeration of all simple paths,
    scored with the same ticket pricing rules (the oracle).

    Graph (edge labels = route, km):
        s1 --A 8.5--> s3                fare 9.00, 1 boarding
        s1 --B 3.5--> s2 --C 3.5--> s3  fare 8.00, 2 boardings
        s1 --B 3.5--> s4 -t0.1-> s2 --C 3.5--> s3   fare 8.00, 2 boardings

    So: cheap optimum = 8.00 (two hops), convenient optimum (fare + 2 zł per
    boarding) = 11.00 (direct A) — the two modes intentionally diverge.
    """

    @classmethod
    def setUpClass(cls):
        from server.data import (adjacency, stops_by_id, stops_grouped,
                                 stop_to_group, routes_by_id, route_shapes)
        from server.pathfinding import init_pathfinding
        import server.pathfinding as pf
        init_pathfinding(adjacency, stops_by_id, stops_grouped, stop_to_group,
                         routes_by_id, route_shapes)
        cls.pf = pf

    def setUp(self):
        pf = self.pf
        names = ('adjacency', 'stops_by_id', 'stops_grouped', 'stop_to_group',
                 'routes_by_id', 'route_shapes', 'stop_coords',
                 'stop_pair_routes')
        self._saved = {n: getattr(pf, n) for n in names}

        coords = {
            's1': (50.000, 19.900),
            's2': (50.027, 19.920),
            's3': (50.054, 19.900),
            's4': (50.027, 19.905),
        }

        def edge(to, route, dist):
            return {'to': to, 'route_id': route, 'distance': dist,
                    'time': int(dist * 120), 'mode': 'bus',
                    'headsign': 'Test'}

        adjacency = {
            's1': [edge('s3', 'A', 8.5), edge('s2', 'B', 3.5),
                   edge('s4', 'B', 3.5)],
            's2': [edge('s1', 'B', 3.5), edge('s3', 'C', 3.5),
                   edge('s4', 'transfer', 0.1)],
            's3': [edge('s1', 'A', 8.5), edge('s2', 'C', 3.5)],
            's4': [edge('s1', 'B', 3.5), edge('s2', 'transfer', 0.1)],
        }

        pf.adjacency = adjacency
        pf.route_shapes = {}
        pf.routes_by_id = {}
        pf.stop_pair_routes = {}
        pf.stop_coords = dict(coords)
        pf.stops_by_id = {
            sid: {'id': sid, 'name': sid, 'lat': lat, 'lon': lon,
                  'code': '', 'mode': 'bus'}
            for sid, (lat, lon) in coords.items()
        }
        pf.stops_grouped = {
            sid: {'id': sid, 'name': sid, 'lat': lat, 'lon': lon,
                  'modes': ['bus'],
                  'platforms': [{'id': sid, 'code': '', 'lat': lat,
                                 'lon': lon, 'mode': 'bus'}]}
            for sid, (lat, lon) in coords.items()
        }
        pf.stop_to_group = {sid: sid for sid in coords}
        # Fresh find cache per test — cached successes ignore upper_bound,
        # which would defeat the bound-pruning test below.
        pf._rebuild_line_index()
        pf._find_cache.clear()
        pf._find_cache_bytes = 0

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.pf, name, value)
        self.pf._rebuild_line_index()

    # ------------------------------------------------------------
    # Oracle: brute-force every simple path, score with the same rules
    # ------------------------------------------------------------
    def _all_simple_paths(self, start, end):
        paths = []

        def dfs(stop, visited, path):
            if stop == end:
                paths.append(list(path))
                return
            for e in self.pf.adjacency.get(stop, []):
                nxt = e['to']
                if nxt in visited:
                    continue
                visited.add(nxt)
                path.append((stop, nxt, e))
                dfs(nxt, visited, path)
                path.pop()
                visited.remove(nxt)

        dfs(start, {start}, [])
        return paths

    def _score(self, path, penalty):
        """Score a path exactly like the search does (same ticket rules).

        Ticket rules (matching the displayed-fare merge in
        _build_route_result): a walk between platforms does NOT close the
        ticket — re-boarding the SAME line continues the ride; boarding a
        different line closes it and opens a new ticket.
        """
        pf = self.pf
        closed, acc, boardings, cur = 0.0, 0.0, 0, None
        for _u, _v, e in path:
            rid, dist = e['route_id'], e['distance']
            if rid == 'transfer':
                continue  # ticket stays open, route/acc unchanged
            elif cur is None or rid != cur:
                closed += pf._ticket_price(acc)
                acc = dist
                boardings += 1
                cur = rid
            else:
                acc += dist
            acc = min(acc, 8.5)
            acc = round(acc * 10) / 10
        return closed + pf._ticket_price(acc) + penalty * boardings

    def _raw_fare(self, result):
        return sum(s.get('cost_regular', 0.0)
                   for s in result.get('segments', []))

    # ------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------
    def test_cheap_finds_true_minimum_fare(self):
        oracle = min(self._score(p, 0.0)
                     for p in self._all_simple_paths('s1', 's3'))
        self.assertEqual(oracle, 8.0)
        result, err = self.pf.find_cheapest_path(['s1'], ['s3'])
        self.assertIsNotNone(result, err)
        self.assertAlmostEqual(self._raw_fare(result), oracle, places=6)

    def test_convenient_finds_true_penalty_optimum(self):
        penalty = self.pf._BOARDING_PENALTY
        oracle = min(self._score(p, penalty)
                     for p in self._all_simple_paths('s1', 's3'))
        self.assertEqual(oracle, 11.0)  # direct A: 9.00 fare + 2.00 boarding
        result, err = self.pf.find_most_convenient_path(['s1'], ['s3'])
        self.assertIsNotNone(result, err)
        scalar = (self._raw_fare(result)
                  + penalty * len(result.get('segments', [])))
        self.assertAlmostEqual(scalar, oracle, places=6)

    def test_modes_diverge_on_this_graph(self):
        """Sanity: the two exact objectives pick DIFFERENT routes here —
        cheap takes two hops (8 zł), convenient goes direct (9 zł + 2)."""
        cheap, err1 = self.pf.find_cheapest_path(['s1'], ['s3'])
        conv, err2 = self.pf.find_most_convenient_path(['s1'], ['s3'])
        self.assertIsNotNone(cheap, err1)
        self.assertIsNotNone(conv, err2)
        self.assertEqual(len(cheap['segments']), 2)
        self.assertEqual(len(conv['segments']), 1)

    def test_upper_bound_pruning_stays_exact(self):
        """Pruning by a bound must never lose the optimum (tested directly
        on the capped A*: with a bound well below the optimum it must report
        'no route', with the exact optimum as the bound it must find it).
        The top-level driver treats the bound as a hint only — the ride
        enumeration returns the proven optimum regardless."""
        result, err = self.pf._find_exact_fare_route_capped(
            ['s1'], ['s3'], upper_bound=7.0, boarding_penalty_zl=0.0,
            max_boardings=2, max_seconds=5)
        self.assertIsNone(result)
        self.assertIn('Nie znaleziono', err)
        result, err = self.pf._find_exact_fare_route_capped(
            ['s1'], ['s3'], upper_bound=8.0, boarding_penalty_zl=0.0,
            max_boardings=2, max_seconds=5)
        self.assertIsNotNone(result, err)
        self.assertAlmostEqual(self._raw_fare(result), 8.0, places=6)

        conv, cerr = self.pf._find_exact_fare_route_capped(
            ['s1'], ['s3'], upper_bound=10.5, boarding_penalty_zl=2.0,
            max_boardings=1, max_seconds=5)
        self.assertIsNone(conv)
        self.assertIn('Nie znaleziono', cerr)
        conv, cerr = self.pf._find_exact_fare_route_capped(
            ['s1'], ['s3'], upper_bound=11.0, boarding_penalty_zl=2.0,
            max_boardings=1, max_seconds=5)
        self.assertIsNotNone(conv, cerr)
        self.assertAlmostEqual(self._raw_fare(conv), 9.0, places=6)

    def test_same_line_across_platform_walk_is_one_ticket(self):
        """Regression for ticket semantics: walking between platforms of the
        same stop and re-boarding the SAME line must price as ONE ticket
        (the display merges such segments), not two.

        Graph: m1 -L1 3.5-> m2 -walk 0.1-> m3 -L1 3.5-> m4 costs
        price(7.0) = 7.50 with 1 boarding, beating m1 -X-> m5 -Y-> m4
        (8.00, 2 boardings)."""
        pf = self.pf

        def edge(to, route, dist):
            return {'to': to, 'route_id': route, 'distance': dist,
                    'time': int(dist * 120), 'mode': 'bus', 'headsign': 'T'}

        pf.adjacency = {
            'm1': [edge('m2', 'L1', 3.5), edge('m5', 'X', 3.5)],
            'm2': [edge('m1', 'L1', 3.5), edge('m3', 'transfer', 0.1)],
            'm3': [edge('m2', 'transfer', 0.1), edge('m4', 'L1', 3.5)],
            'm4': [edge('m3', 'L1', 3.5), edge('m5', 'Y', 3.5)],
            'm5': [edge('m1', 'X', 3.5), edge('m4', 'Y', 3.5)],
        }
        coords = {
            'm1': (50.000, 19.900), 'm2': (50.032, 19.900),
            'm3': (50.033, 19.901), 'm4': (50.065, 19.901),
            'm5': (50.000, 19.935),
        }
        pf.stop_coords = dict(coords)
        pf.stops_by_id = {
            sid: {'id': sid, 'name': sid, 'lat': lat, 'lon': lon,
                  'code': '', 'mode': 'bus'}
            for sid, (lat, lon) in coords.items()}
        pf.stops_grouped = {
            sid: {'id': sid, 'name': sid, 'lat': lat, 'lon': lon,
                  'modes': ['bus'],
                  'platforms': [{'id': sid, 'code': '', 'lat': lat,
                                 'lon': lon, 'mode': 'bus'}]}
            for sid, (lat, lon) in coords.items()}
        pf.stop_to_group = {sid: sid for sid in coords}
        pf._rebuild_line_index()
        pf._find_cache.clear()
        pf._find_cache_bytes = 0

        result, err = pf.find_cheapest_path(['m1'], ['m4'])
        self.assertIsNotNone(result, err)
        self.assertAlmostEqual(self._raw_fare(result), 7.5, places=6)
        # One ticket: L1 + walk + L1 merged into a single segment.
        self.assertEqual(len(result['segments']), 1)

        conv, cerr = pf.find_most_convenient_path(['m1'], ['m4'])
        self.assertIsNotNone(conv, cerr)
        # convenient: 7.50 + 1 boarding (2.00) = 9.50 beats X+Y (8+4=12).
        self.assertAlmostEqual(
            self._raw_fare(conv) + 2.0 * len(conv['segments']), 9.5, places=6)


if __name__ == '__main__':
    unittest.main()
