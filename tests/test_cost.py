"""Unit tests for server.cost module."""

import unittest
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import cost


class TestCalculateCost(unittest.TestCase):
    """Test ticket cost calculation based on distance."""

    def test_zero_distance(self):
        """Zero distance should cost nothing."""
        reg, red = cost.calculate_cost(0)
        self.assertEqual(reg, 0.0)
        self.assertEqual(red, 0.0)

    def test_negative_distance(self):
        """Negative distance should cost nothing."""
        reg, red = cost.calculate_cost(-5)
        self.assertEqual(reg, 0.0)
        self.assertEqual(red, 0.0)

    def test_base_distance(self):
        """Distance <= 3.5 km should return base cost."""
        reg, red = cost.calculate_cost(3.5)
        self.assertEqual(reg, 4.0)
        self.assertEqual(red, 2.0)

    def test_below_base_distance(self):
        """Distance < 3.5 km should still return base cost."""
        reg, red = cost.calculate_cost(1.0)
        self.assertEqual(reg, 4.0)
        self.assertEqual(red, 2.0)

    def test_one_segment_beyond_base(self):
        """3.5 + 0.5 = 4.0 km → one additional segment."""
        reg, red = cost.calculate_cost(4.0)
        self.assertEqual(reg, 4.50)  # 4.00 + 0.50
        self.assertEqual(red, 2.25)  # 2.00 + 0.25

    def test_two_segments_beyond_base(self):
        """3.5 + 1.0 = 4.5 km → two additional segments."""
        reg, red = cost.calculate_cost(4.5)
        self.assertEqual(reg, 5.00)  # 4.00 + 2*0.50
        self.assertEqual(red, 2.50)  # 2.00 + 2*0.25

    def test_partial_segment_rounds_up(self):
        """Partial segment should round up (3.5 + 0.3 = 3.8 → 1 segment)."""
        reg, red = cost.calculate_cost(3.8)
        self.assertEqual(reg, 4.50)  # 4.00 + 1*0.50
        self.assertEqual(red, 2.25)

    def test_max_cost_cap_regular(self):
        """Regular cost should be capped at MAX_COST_REGULAR."""
        reg, red = cost.calculate_cost(100)  # Very long distance
        self.assertEqual(reg, 9.00)
        self.assertEqual(red, 4.50)

    def test_exact_max_cost_boundary(self):
        """Test the boundary where max cost kicks in."""
        # base=3.5, segment=0.5, segment_cost=0.50
        # 3.5 + 11*0.5 = 9.0 km → 11 segments → 4.00 + 11*0.50 = 9.50 → capped to 9.00
        reg, red = cost.calculate_cost(9.0)
        self.assertEqual(reg, 9.00)
        self.assertEqual(red, 4.50)

    def test_returns_floats(self):
        """Results should be float type."""
        reg, red = cost.calculate_cost(5.0)
        self.assertIsInstance(reg, float)
        self.assertIsInstance(red, float)


class TestCalculateRouteCost(unittest.TestCase):
    """Test route cost calculation (sum of segments with daily cap)."""

    def test_empty_segments(self):
        """Empty route should cost nothing."""
        reg, red = cost.calculate_route_cost([])
        self.assertEqual(reg, 0.0)
        self.assertEqual(red, 0.0)

    def test_single_short_segment(self):
        """Single short segment (base cost)."""
        segments = [{'distance': 2.0}]
        reg, red = cost.calculate_route_cost(segments)
        self.assertEqual(reg, 4.00)
        self.assertEqual(red, 2.00)

    def test_two_segments_sum(self):
        """Two segments should sum their costs."""
        segments = [
            {'distance': 2.0},  # 4.00 / 2.00 (within base)
            {'distance': 5.0},  # 5.50 / 2.75 (3.5 base + 3 segments of 0.5)
        ]
        reg, red = cost.calculate_route_cost(segments)
        self.assertEqual(reg, 9.50)
        self.assertEqual(red, 4.75)

    def test_daily_cap_regular(self):
        """Regular total should be capped at 20.00."""
        # 5 segments of 10km each → each costs ~7.00 → total 35.00 → capped to 20.00
        segments = [{'distance': 10.0}] * 5
        reg, red = cost.calculate_route_cost(segments)
        self.assertEqual(reg, 20.00)
        self.assertEqual(red, 10.00)

    def test_segment_without_distance(self):
        """Segment without distance should cost 0."""
        segments = [{'route_id': 'test'}]  # No distance key
        reg, red = cost.calculate_route_cost(segments)
        self.assertEqual(reg, 0.0)
        self.assertEqual(red, 0.0)

    def test_returns_rounded_floats(self):
        """Results should be rounded to 2 decimal places."""
        segments = [{'distance': 3.7}]  # 4.00 + 1*0.50 = 4.50
        reg, red = cost.calculate_route_cost(segments)
        self.assertEqual(reg, 4.50)
        self.assertEqual(red, 2.25)


if __name__ == '__main__':
    unittest.main()
