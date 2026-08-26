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


if __name__ == '__main__':
    unittest.main()
