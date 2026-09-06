"""Security and cache-behaviour tests that do NOT need a live socket.

Covers the pieces most likely to regress silently:

* path traversal protection on static file serving,
* rate limiting (expensive endpoint 429) via the pure limiter function,
* failed searches are never cached (poisoned-pair regression),
* OG meta rewriting is regex-based and survives text edits in index.html,
* /api/status exposes no host internals.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from email.parser import Parser
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.handler import (  # noqa: E402
    MPKRequestHandler, _rate_limit_ok, _rewrite_og_meta,
)
from server import pathfinding  # noqa: E402


def _make_handler(path):
    """Build a request handler around BytesIO pipes (no network at all).

    Enough of the http.server contract is faked to drive do_GET() through
    the normal routing code paths.
    """
    raw = (f"GET {path} HTTP/1.1\r\nHost: localhost\r\n"
           f"Accept-Encoding: identity\r\nUser-Agent: test\r\n\r\n").encode()
    h = MPKRequestHandler.__new__(MPKRequestHandler)
    h.rfile = BytesIO(raw)
    h.wfile = BytesIO()
    h._headers_buffer = []
    h.client_address = ('127.0.0.1', 12345)
    h.command = 'GET'
    h.path = path
    h.request_version = 'HTTP/1.1'
    h.requestline = path
    h.headers = Parser().parsestr("Host: localhost\r\nUser-Agent: test\r\n")
    h.connection = None
    from server.handler import PUBLIC_DIR
    h.directory = PUBLIC_DIR
    return h


def _request(path):
    """Run do_GET() offline; return (status_code, body_bytes)."""
    h = _make_handler(path)
    try:
        h.do_GET()
    except Exception:
        # A handler crash is itself a failure worth reporting distinctly.
        raise
    out = h.wfile.getvalue()
    status = int(out.split(b'\r\n')[0].split()[1])
    body = out.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in out else b''
    return status, body


class TestPathTraversal(unittest.TestCase):
    """Static serving must never escape public/."""

    def test_traversal_attempts_blocked(self):
        for path in ('/../server.env', '/..%2fserver.env',
                     '/../../etc/passwd', '/....//server.env'):
            status, _ = _request(path)
            self.assertIn(status, (403, 404), f'{path} returned {status}')

    def test_dot_env_blocked(self):
        status, _ = _request('/.env')
        self.assertEqual(status, 404)

    def test_legit_file_still_served(self):
        status, body = _request('/logo.svg')
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'<'))

    def test_missing_file_404(self):
        status, _ = _request('/definitely-not-here.html')
        self.assertEqual(status, 404)


class TestRateLimiting(unittest.TestCase):
    """The expensive bucket must 429 after its limit is exceeded."""

    IP = 'test-rate-limit-ip'

    def test_expensive_limit_enforced(self):
        # Drain the expensive bucket (limit is small by design).
        allowed = 0
        for _ in range(100):
            if _rate_limit_ok(self.IP, expensive=True):
                allowed += 1
            else:
                break
        self.assertLess(allowed, 100, 'expensive bucket never exhausted')
        self.assertGreater(allowed, 0, 'first request must be allowed')

        # The normal bucket is independent and still allows requests.
        self.assertTrue(_rate_limit_ok(self.IP + '-other', expensive=False))


class TestFailureNotCached(unittest.TestCase):
    """A timed-out or failed search must not poison the pair's cache slot."""

    def test_cache_put_find_rejects_failures(self):
        key = ('test-failure', 'pair')
        try:
            pathfinding._cache_put_find(key, (None, 'Timeout'))
            self.assertIsNone(pathfinding._cache_get_find(key))
        finally:
            with pathfinding._find_cache_lock:
                pathfinding._find_cache.pop(key, None)

    def test_cache_put_find_stores_success(self):
        key = ('test-success', 'pair')
        value = ({'total_distance': 1.0}, None)
        try:
            pathfinding._cache_put_find(key, value)
            cached = pathfinding._cache_get_find(key)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[1], None)
        finally:
            with pathfinding._find_cache_lock:
                pathfinding._find_cache.pop(key, None)


class TestOgMetaRewriting(unittest.TestCase):
    """Rewriting must be keyed by meta name, not by exact default text."""

    DOC = ('<meta property="og:title" content="OLD">'
           '<meta name="twitter:image" content="https://old/x.svg">'
           '<meta property="og:url" content="https://old/">')

    def test_replaces_targeted_keys(self):
        out = _rewrite_og_meta(self.DOC, {'og:title': 'NEW', 'twitter:image': 'https://new/i.svg'})
        self.assertIn('content="NEW"', out)
        self.assertIn('https://new/i.svg', out)

    def test_leaves_other_tags_alone(self):
        out = _rewrite_og_meta(self.DOC, {})
        self.assertIn('content="OLD"', out)
        self.assertIn('https://old/', out)


class TestStatusPayload(unittest.TestCase):
    """/api/status is publicly reachable — no host internals allowed."""

    def test_no_host_internals(self):
        status, body = _request('/api/status')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for forbidden in ('load_avg', 'process_cpu_pct', 'cpus', 'counters'):
            self.assertNotIn(forbidden, payload)

    def test_has_version_and_uptime(self):
        status, body = _request('/api/status')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn('version', payload)
        self.assertIn('uptime_seconds', payload)


class TestTileProxy(unittest.TestCase):
    """Map tiles are proxied from our own origin (privacy): coordinate
    validation must reject garbage without ever touching upstream."""

    def test_invalid_tile_paths_400(self):
        for path in ('/api/tiles/v2/abc/1/2.png',
                      '/api/tiles/v2/20/1/1.png',   # z > 19
                      '/api/tiles/v2/2/99/1.png',   # x out of range
                      '/api/tiles/v2/0/1/0.png',    # z=0 admits only 0/0
                      '/api/tiles/v2/10/1/1.jpg',
                      '/api/tiles/10/550/343.png'):  # unversioned: 404
            status, _ = _request(path)
            self.assertIn(status, (400, 404), f'{path} returned {status}')

    def test_cached_tile_served_without_upstream(self):
        from server.handler import _tile_cache_path
        cache_path = _tile_cache_path(10, 550, 343, '')
        fake_png = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 200)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'wb') as f:
            f.write(fake_png)
        try:
            status, body = _request('/api/tiles/v2/10/550/343.png')
            self.assertEqual(status, 200)
            self.assertEqual(body, fake_png)
        finally:
            if os.path.isfile(cache_path):
                os.remove(cache_path)

    def test_retina_tile_path_accepted(self):
        from server.handler import _TILE_RE
        m = _TILE_RE.match('/api/tiles/v2/13/4412/2808@2x.png')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(4), '@2x')

    def test_missing_api_key_refuses_without_caching(self):
        """Without TILE_API_KEY upstream would watermark tiles — the
        proxy must 502 instead of caching garbage."""
        from server import handler as handler_mod
        from server.handler import _tile_cache_path
        old_key, handler_mod.TILE_API_KEY = handler_mod.TILE_API_KEY, ''
        cache_path = _tile_cache_path(19, 524287, 524287, '')
        try:
            if os.path.isfile(cache_path):
                os.remove(cache_path)
            status, _ = _request('/api/tiles/v2/19/524287/524287.png')
            self.assertEqual(status, 502)
            self.assertFalse(os.path.isfile(cache_path),
                             'refused tile must not be cached')
        finally:
            handler_mod.TILE_API_KEY = old_key
            if os.path.isfile(cache_path):
                os.remove(cache_path)


class TestStatsRetention(unittest.TestCase):
    """Raw events expire, but per-day aggregates keep the full history."""

    OLD_TS = __import__('time').time() - 200 * 86400  # predates the app

    def _old_day(self):
        from server import admin_stats
        return admin_stats._day_key(self.OLD_TS, admin_stats.WARSAW)

    def _cleanup(self):
        from server import admin_stats
        with admin_stats._lock:
            admin_stats._conn.execute(
                "DELETE FROM events WHERE kind IN ('request', 'visit') "
                "AND (iph LIKE 'test-%' OR outcome IN ('test-old', 'test-fresh'))")
            admin_stats._conn.execute(
                "DELETE FROM daily_rollup WHERE day=?", (self._old_day(),))
            admin_stats._conn.commit()

    def test_old_events_pruned_fresh_kept(self):
        import time
        from server import admin_stats
        if admin_stats._conn is None:
            self.skipTest('stats DB unavailable')
        with admin_stats._lock:
            admin_stats._conn.execute(
                "INSERT INTO events(ts, kind, outcome, iph) "
                "VALUES (?, 'request', 'test-old', 'test-old-hash')",
                (self.OLD_TS,))
            admin_stats._conn.execute(
                "INSERT INTO events(ts, kind, outcome, iph) "
                "VALUES (?, 'request', 'test-fresh', 'test-fresh-hash')",
                (time.time(),))
            admin_stats._conn.commit()
        admin_stats._last_prune = 0.0  # force the daily prune to run now
        try:
            admin_stats.prune_old_events()
            with admin_stats._lock:
                rows = admin_stats._conn.execute(
                    "SELECT outcome FROM events "
                    "WHERE outcome IN ('test-old', 'test-fresh')").fetchall()
            outcomes = sorted(r[0] for r in rows)
            self.assertEqual(outcomes, ['test-fresh'])
        finally:
            self._cleanup()

    def test_rollup_preserves_full_history(self):
        import time
        from server import admin_stats
        if admin_stats._conn is None:
            self.skipTest('stats DB unavailable')
        old_day = self._old_day()
        with admin_stats._lock:
            for iph in ('test-h1', 'test-h2', 'test-h1'):
                admin_stats._conn.execute(
                    "INSERT INTO events(ts, kind, outcome, iph) "
                    "VALUES (?, 'request', 'ok', ?)", (self.OLD_TS, iph))
            admin_stats._conn.commit()
        admin_stats._last_prune = 0.0
        try:
            admin_stats.prune_old_events()
            with admin_stats._lock:
                raw = admin_stats._conn.execute(
                    "SELECT COUNT(*) FROM events WHERE iph LIKE 'test-h%'"
                ).fetchone()[0]
                roll = admin_stats._conn.execute(
                    "SELECT requests, ok, visitors FROM daily_rollup "
                    "WHERE day=?", (old_day,)).fetchone()
            self.assertEqual(raw, 0, 'old raw events must be pruned')
            self.assertIsNotNone(roll, 'old day must survive in rollup')
            self.assertEqual(tuple(roll), (3, 3, 2))
            # Merged series spans the whole period, no double counting.
            from_ts = self.OLD_TS - 86400
            daily, unique_total, meta = admin_stats.daily_series(
                from_ts=from_ts, to_ts=time.time())
            self.assertIn(old_day, daily)
            self.assertEqual(daily[old_day]['requests'], 3)
            self.assertEqual(daily[old_day]['visitors'], 2)
            self.assertFalse(meta['unique_exact'])
            self.assertIsNotNone(meta['unique_since'])
        finally:
            self._cleanup()


if __name__ == '__main__':
    unittest.main()
