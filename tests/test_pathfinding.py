"""Unit tests for server.pathfinding module."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.pathfinding import haversine_km


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

    def test_find_route_between_groups_returns_triple(self):
        """Should return (short_pair, convenient_pair, cheap_pair)."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='both')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        short_pair, convenient_pair, cheap_pair = result
        self.assertIsInstance(short_pair, tuple)
        self.assertIsInstance(convenient_pair, tuple)
        self.assertIsInstance(cheap_pair, tuple)

    def test_find_route_short_mode(self):
        """mode='short' should return a single (result, error) pair."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='short')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_find_route_cheap_mode(self):
        """mode='cheap' should return a single (result, error) pair."""
        result = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='cheap')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_cheap_is_no_more_expensive_than_short(self):
        """Exactness invariant: when the cheap search completes, its fare can
        never exceed the fare of the shortest route."""
        triple = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='both')
        short_pair, _, cheap_pair = triple
        short_result, _ = short_pair
        cheap_result, cheap_err = cheap_pair
        self.assertIsNotNone(short_result)
        if cheap_result is None:
            # Exact search timed out — legitimate; the error must be transient.
            self.assertTrue(self.pf._is_transient_error(cheap_err), cheap_err)
            return
        self.assertLessEqual(cheap_result['cost_regular'],
                             short_result['cost_regular'])

    def test_cheap_is_no_more_expensive_than_convenient(self):
        """Exactness invariant (reported pair Skrajna→Elektrociepłownia):
        when both exact searches complete, cheap fare <= convenient fare.
        A timed-out cheap mode is null + transient error, never approximated."""
        # Real reported pair: Skrajna (group_341) → Elektrociepłownia
        # (group_1503) — long trip where the exact searches are slowest.
        pair = ('group_341', 'group_1503')
        if pair[0] not in self.stops_grouped or pair[1] not in self.stops_grouped:
            self.skipTest('reported pair not present in this dataset')

        def raw_fare(r):
            if r is None:
                return float('inf')
            return sum(s.get('cost_regular', 0.0)
                       for s in r.get('segments', []))

        triple = self.pf.find_route_between_groups(*pair, mode='both')
        short_pair, conv_pair, cheap_pair = triple
        short_result, _ = short_pair
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
            if short_result is not None:
                self.assertLessEqual(
                    raw_fare(cheap_result), raw_fare(short_result),
                    'cheap route costs more than the short route')

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
        short_pair, _, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_from, mode='both')
        result, error = short_pair
        self.assertIsNotNone(result)
        self.assertEqual(result['total_distance'], 0)
        self.assertEqual(result['cost_regular'], 0.0)

    def test_route_path_has_unique_groups(self):
        """Regression: path must contain exactly ONE entry per stop group
        (no duplicate dots for the same stop), positioned at a real peron."""
        short_pair, _, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = short_pair
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
        short_pair, _, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = short_pair
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
        short_pair, _, _ = self.pf.find_route_between_groups(
            self.test_from, self.test_to, mode='both')
        result, error = short_pair
        self.assertIsNotNone(result, error)
        adj = self.pf.adjacency
        total_from_edges = 0.0
        for seg in result['segments']:
            seg_edges = 0.0
            stops = seg['stops']
            for j in range(len(stops) - 1):
                a, b = stops[j], stops[j + 1]
                # find the forward edge a->b used on this route
                dist = None
                for e in adj.get(a, []):
                    if e['to'] == b and e['route_id'] == seg['route_id']:
                        dist = e['distance']
                        break
                if dist is None:
                    # reverse edge (bidirectional adjacency) — same distance
                    for e in adj.get(b, []):
                        if e['to'] == a and e['route_id'] == seg['route_id']:
                            dist = e['distance']
                            break
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
        self.assertEqual(len(result), 3)
        short_pair, convenient_pair, cheap_pair = result
        self.assertIsNone(short_pair[0])  # result is None
        self.assertIsNotNone(short_pair[1])  # error is set
        self.assertIsNone(convenient_pair[0])
        self.assertIsNone(cheap_pair[0])


class TestRouteCacheFlushNoDeadlock(unittest.TestCase):
    """Regression: the cache flusher must never save while holding
    _route_cache_lock.

    _save_route_cache() acquires the non-reentrant _route_cache_lock itself.
    The old _flush_loop called it while already holding the lock, which
    self-deadlocked the flusher thread while HOLDING the lock — wedging the
    pathfinding worker (it blocks on the lock forever) so that every route
    search timed out ~2 minutes after the first uncached search.
    """

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

    def test_flush_cycle_does_not_deadlock(self):
        """The flusher's decision+save cycle must complete while route
        searches may run — never self-deadlock on _route_cache_lock.

        Before the fix, the flush loop called _save_route_cache() while
        holding _route_cache_lock; the non-reentrant Lock deadlocked the
        flusher thread holding the lock, so every route search timed out
        ~2 minutes after the first uncached computation.
        """
        import threading

        pf = self.pf
        # Seed a cache entry so the flusher decides to save (sig != last).
        cache_key = (self.test_from, self.test_to, 'flush-test', pf._feed_version)
        with pf._route_cache_lock:
            pf._route_cache[cache_key] = ((None, 'x'),) * 3, 100

        # Run flush cycles while concurrent "searches" hammer the lock.
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                with pf._route_cache_lock:
                    pass

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
        for r in readers:
            r.start()
        try:
            last_sig, last_count = None, 0
            for _ in range(3):
                def cycle(s=last_sig, c=last_count):
                    return pf._flush_route_cache_once(s, c)

                t = threading.Thread(target=cycle, daemon=True)
                t.start()
                t.join(timeout=10)
                self.assertFalse(t.is_alive(),
                                 'flush cycle deadlocked on _route_cache_lock')
                last_sig, last_count = cycle()
        finally:
            stop.set()
            for r in readers:
                r.join(timeout=5)

    def test_flush_releases_cache_lock_for_pathfinding(self):
        """After a flush, _route_cache_lock must be free so find_route_…
        never blocks on the background flusher."""
        import threading

        pf = self.pf
        pf._flush_route_cache_once(None, 0)
        self.assertTrue(pf._route_cache_lock.acquire(timeout=5),
                        '_route_cache_lock held after flush cycle')
        pf._route_cache_lock.release()

    def test_concurrent_saves_do_not_corrupt_file(self):
        """Two threads saving at once (flusher + atexit) must not corrupt the
        cache file — writes are serialised by _save_route_cache_lock."""
        import json
        import threading

        pf = self.pf
        threads = [threading.Thread(target=pf._save_route_cache)
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), 'concurrent _save_route_cache() hung')
        # The written file must still be valid JSON.
        with open(pf._cache_file_path(), encoding='utf-8') as f:
            json.load(f)


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
        import server.pathfinding as pf
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
        pf._find_cache.clear()
        pf._find_cache_bytes = 0

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.pf, name, value)

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
        """Score a path exactly like the search does (same ticket rules)."""
        pf = self.pf
        closed, acc, boardings, cur = 0.0, 0.0, 0, None
        for _u, _v, e in path:
            rid, dist = e['route_id'], e['distance']
            if rid == 'transfer':
                closed += pf._ticket_price(acc)
                acc = 0.0
                cur = 'transfer'
            elif cur is None or cur == 'transfer' or rid != cur:
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
        """Pruning by a bound must never lose the optimum: with a bound just
        below the optimum the search must report 'no route', with the exact
        optimum as the bound it must find it."""
        result, err = self.pf.find_cheapest_path(['s1'], ['s3'],
                                                 upper_bound=7.9)
        self.assertIsNone(result)
        self.assertIn('Nie znaleziono', err)
        result, err = self.pf.find_cheapest_path(['s1'], ['s3'],
                                                 upper_bound=8.0)
        self.assertIsNotNone(result, err)
        self.assertAlmostEqual(self._raw_fare(result), 8.0, places=6)

        conv, cerr = self.pf.find_most_convenient_path(['s1'], ['s3'],
                                                       upper_bound=10.5)
        self.assertIsNone(conv)
        self.assertIn('Nie znaleziono', cerr)
        conv, cerr = self.pf.find_most_convenient_path(['s1'], ['s3'],
                                                       upper_bound=11.0)
        self.assertIsNotNone(conv, cerr)
        self.assertAlmostEqual(self._raw_fare(conv), 9.0, places=6)


if __name__ == '__main__':
    unittest.main()
