"""HTTP endpoint tests for server.handler.

Boots the real ThreadedHTTPServer on an ephemeral port and exercises the
API endpoints. These tests exist because the two P0 regressions (broken
stops search, broken og-image) lived entirely in the handler layer and
were invisible to the earlier unit tests.

Run:  python3 -m unittest discover -s tests
"""

import gzip
import json
import re
import sys
import os
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.handler import MPKRequestHandler
from server.pathfinding import find_route_between_groups, init_pathfinding
from server import data

# populate the pathfinding module state (server.py normally does this)
init_pathfinding(
    data.adjacency, data.stops_by_id, data.stops_grouped,
    data.stop_to_group, data.routes_by_id, data.route_shapes,
)


class TestHandlerEndpoints(unittest.TestCase):
    """End-to-end HTTP tests against the real server."""

    @classmethod
    def setUpClass(cls):
        from http.server import HTTPServer
        from socketserver import ThreadingMixIn

        class TestServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        cls.server = TestServer(('127.0.0.1', 0), MPKRequestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.port}'

        # Pick two connected nearby groups for route tests
        group_ids = list(data.stops_grouped.keys())[:50]
        cls.from_id = group_ids[0]
        cls.to_id = group_ids[1]
        # Find a pair guaranteed to have a route: walk until one is found
        for gid in group_ids[2:]:
            short_pair, _, _ = find_route_between_groups(cls.from_id, gid, mode='both')
            if short_pair[0] is not None and short_pair[0]['path']:
                cls.to_id = gid
                break

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=30) as resp:
            self._last_headers = dict(resp.headers)
            return resp.status, self._last_headers, resp.read()

    # ------------------------------------------------------------
    # P0 regressions
    # ------------------------------------------------------------
    def test_stops_search_returns_array(self):
        """Regression: search previously raised AttributeError and returned
        a 200 JSON error body instead of results."""
        status, _, body = self._get('/api/stops/search?q=dworzec')
        self.assertEqual(status, 200)
        results = json.loads(body)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertIn('id', results[0])
        self.assertTrue(results[0]['id'].startswith('group_'))
        self.assertIn('name', results[0])

    def test_stops_search_short_query(self):
        """Queries shorter than 2 chars return an empty list."""
        status, _, body = self._get('/api/stops/search?q=d')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_stops_search_by_code(self):
        """Stop-code search (e.g. platform numbers) also works."""
        status, _, body = self._get('/api/stops/search?q=101')
        self.assertEqual(status, 200)
        results = json.loads(body)
        self.assertIsInstance(results, list)
        # Some codes are names — just verify the endpoint doesn't crash
        # and truncates to at most 50 entries.
        self.assertLessEqual(len(results), 50)

    def test_og_image_returns_svg(self):
        """Regression: og-image previously crashed on tuple unpacking."""
        status, headers, body = self._get(
            f'/api/og-image?from={self.from_id}&to={self.to_id}&mode=short')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'image/svg+xml')
        self.assertTrue(body.lstrip().startswith(b'<svg'))
        self.assertIn(b'zloty', body.lower().replace(b'z\xc5\x82', b'zloty'))

    def test_og_image_with_invalid_stops(self):
        """Invalid stop ids degrade gracefully (no crash, no route info)."""
        status, headers, body = self._get(
            '/api/og-image?from=group_999999&to=group_888888&mode=short')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'image/svg+xml')
        self.assertTrue(body.lstrip().startswith(b'<svg'))

    # ------------------------------------------------------------
    # Route API
    # ------------------------------------------------------------
    def test_find_route_returns_all_modes(self):
        status, _, body = self._get(
            f'/api/find-route?from={self.from_id}&to={self.to_id}')
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIn('short', result)
        self.assertIn('convenient', result)
        self.assertIn('cheap', result)
        self.assertIsNotNone(result['short'])
        self.assertIsNotNone(result['cheap'])

    def test_find_route_cheap_not_more_expensive(self):
        status, _, body = self._get(
            f'/api/find-route?from={self.from_id}&to={self.to_id}')
        result = json.loads(body)
        self.assertIsNotNone(result['short'])
        self.assertIsNotNone(result['cheap'])
        self.assertLessEqual(result['cheap']['cost_regular'],
                             result['short']['cost_regular'])

    def test_og_image_cheap_mode(self):
        """OG image accepts the cheap mode."""
        status, headers, body = self._get(
            f'/api/og-image?from={self.from_id}&to={self.to_id}&mode=cheap')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'image/svg+xml')
        self.assertTrue(body.lstrip().startswith(b'<svg'))

    def test_route_path_carries_group_id(self):
        """Regression: frontend dimming needs group_id on path stops."""
        status, _, body = self._get(
            f'/api/find-route?from={self.from_id}&to={self.to_id}')
        result = json.loads(body)
        path = result['short']['path']
        self.assertGreater(len(path), 0)
        self.assertIn('group_id', path[0])
        self.assertTrue(path[0]['group_id'].startswith('group_'))

    def test_find_route_invalid_group(self):
        status, _, body = self._get(
            '/api/find-route?from=group_999999&to=group_1')
        self.assertEqual(status, 200)
        result = json.loads(body)
        # Both modes fail -> single {error} response (frontend checks .error)
        self.assertIn('error', result)
        self.assertIsNone(result.get('short'))
        self.assertIsNone(result.get('convenient'))

    # ------------------------------------------------------------
    # Cost API
    # ------------------------------------------------------------
    def test_cost_api(self):
        status, _, body = self._get('/api/cost?distance=4')
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['cost_regular'], 4.5)
        self.assertEqual(result['cost_reduced'], 2.25)

    def test_cost_api_invalid(self):
        """Invalid input is rejected with HTTP 400 (not a silent 200)."""
        req = urllib.request.Request(self.base + '/api/cost?distance=abc')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body).get('error'), 'Invalid distance parameter')

    def test_unknown_endpoint_404(self):
        req = urllib.request.Request(self.base + '/api/nope')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 404)

    def test_index_html_nonces_all_script_tags(self):
        """Every external <script> in index.html carries the CSP nonce."""
        _, headers, body = self._get('/index.html')
        html_text = body.decode('utf-8')
        nonce_match = re.search(r'nonce-([A-Za-z0-9_-]+)',
                                headers.get('Content-Security-Policy', ''))
        self.assertIsNotNone(nonce_match, 'CSP header missing nonce')
        nonce = nonce_match.group(1)
        for m in re.finditer(r'<script\b[^>]*src=[^>]*>', html_text):
            self.assertIn(f'nonce="{nonce}"', m.group(0),
                          f'script tag without nonce: {m.group(0)}')

    # ------------------------------------------------------------
    # Compression and caching
    # ------------------------------------------------------------
    def test_gzip_with_vary(self):
        status, headers, body = self._get(
            '/api/stops', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(headers.get('Vary'), 'Accept-Encoding')
        stops = json.loads(gzip.decompress(body))
        self.assertIsInstance(stops, list)
        self.assertGreater(len(stops), 1000)

    # ------------------------------------------------------------
    # Rate limiting vs cheap endpoints (the "blank map" bug)
    # ------------------------------------------------------------
    def test_cheap_endpoints_not_rate_limited(self):
        """Regression: a burst on /api/stops used to 429 and the frontend
        silently rendered no stops. Cheap endpoints must not be limited."""
        statuses = []
        for _ in range(35):
            try:
                status, _, _ = self._get('/api/stops')
            except urllib.error.HTTPError as e:
                status = e.code
            statuses.append(status)
        self.assertTrue(all(s == 200 for s in statuses),
                        f'cheap endpoint was rate limited: {statuses}')

    def test_health(self):
        status, _, body = self._get('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'status': 'ok'})

    def test_status_reports_metrics(self):
        """/api/status exposes version, load, caches and counters."""
        status, _, body = self._get('/api/status')
        self.assertEqual(status, 200)
        s = json.loads(body)
        self.assertEqual(s['version'], data.APP_VERSION)
        self.assertIn('uptime_seconds', s)
        self.assertIn('load_avg', s)
        self.assertIn('active_requests', s)
        self.assertIn('rss_mb', s)
        self.assertIn('find_cache', s)
        self.assertIn('route_entries', s['find_cache'])
        self.assertIn('counters', s)
        self.assertIn('api_total', s['counters'])
        self.assertIn('cheap', s)
        self.assertIn('searches', s['cheap'])


if __name__ == '__main__':
    unittest.main()