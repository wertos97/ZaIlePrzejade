"""Unit tests for server.pathfinding module."""

import unittest
import math
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
        from server.pathfinding import init_pathfinding, find_shortest_path, find_route_between_groups

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
        """The cheapest fare can never exceed the fare of the shortest route."""
        triple = self.pf.find_route_between_groups(self.test_from, self.test_to, mode='both')
        short_pair, _, cheap_pair = triple
        short_result, _ = short_pair
        cheap_result, _ = cheap_pair
        self.assertIsNotNone(short_result)
        self.assertIsNotNone(cheap_result)
        self.assertLessEqual(cheap_result['cost_regular'],
                             short_result['cost_regular'])

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


if __name__ == '__main__':
    unittest.main()
