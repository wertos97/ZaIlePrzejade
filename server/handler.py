"""
HTTP request handler for MPK Kraków ticket calculator.
Serves static files and provides API endpoints.
"""

import gzip
import html
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from http.server import SimpleHTTPRequestHandler

from .config import (
    APP_VERSION,
    BLOCKED_PATH_PREFIXES as _BLOCKED_PREFIXES,
    BLOCKED_PATHS as _BLOCKED_PATHS,
    BOT_USER_AGENTS as _BOT_LIST,
    CSP_NONCE_BYTES,
    FAVICON_CACHE_MAX_AGE,
    GROUP_ID_PATTERN as _GROUP_ID_PATTERN_SRC,
    MAX_DISTANCE_PARAM_LENGTH,
    MAX_GROUP_ID_LENGTH as _MAX_GROUP_ID_LEN,
    MAX_CONCURRENT_REQUESTS,
    MAX_ROUTE_ID_LENGTH,
    MAX_STOP_ID_LENGTH,
    OG_IMAGE_CACHE_MAX_AGE,
    PATHFINDING_EXECUTOR_WORKERS,
    PUBLIC_BASE_URL,
    RATE_LIMIT_CLEANUP_INTERVAL_SECONDS,
    RATE_LIMIT_EXPENSIVE_MAX_REQUESTS as _RATE_LIMIT_EXPENSIVE_MAX,
    RATE_LIMIT_MAX_REQUESTS as _RATE_LIMIT_MAX,
    RATE_LIMIT_STALE_CUTOFF_MULTIPLIER,
    RATE_LIMIT_WINDOW_SECONDS as _RATE_LIMIT_WINDOW,
    SEARCH_MAX_QUERY_LENGTH,
    SEARCH_MAX_RESULTS,
    SEARCH_MIN_QUERY_LENGTH,
    SEARCH_PREFIX_MAX_LENGTH,
    STATIC_CACHE_MAX_AGE_UNVERSIONED,
    STATIC_CACHE_MAX_AGE_VERSIONED,
    TRUST_PROXY_HEADERS,
)
from . import data
from . import cost
from . import pathfinding
from .logging_config import get_logger

_GROUP_ID_PATTERN = re.compile(_GROUP_ID_PATTERN_SRC)


def generate_nonce() -> str:
    """Generate a cryptographically secure nonce for CSP."""
    return secrets.token_urlsafe(CSP_NONCE_BYTES)


def sanitize_svg(content: str) -> str:
    """Sanitize SVG content for safe embedding.

    Removes script elements, event handlers, javascript: URLs, and other
    potentially dangerous content. Keeps only safe presentation elements.
    """
    if not content:
        return ''

    # Remove script elements entirely
    content = re.sub(r'<script\b[^>]*>.*?</script>', '', content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r'<script\b[^>]*/?>', '', content, flags=re.IGNORECASE)

    # Remove event handlers (onclick, onload, etc.)
    content = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\s+on\w+\s*=\s*\S+', '', content, flags=re.IGNORECASE)

    # Remove javascript: URLs
    content = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', 'href="#"', content, flags=re.IGNORECASE)
    content = re.sub(r'xlink:href\s*=\s*["\']javascript:[^"\']*["\']', 'xlink:href="#"', content, flags=re.IGNORECASE)

    # Remove style attributes that could contain expression() or behavior
    content = re.sub(r'style\s*=\s*["\'][^"\']*expression\s*\([^"\']*["\']', 'style=""', content, flags=re.IGNORECASE)
    content = re.sub(r'style\s*=\s*["\'][^"\']*behavior\s*\([^"\']*["\']', 'style=""', content, flags=re.IGNORECASE)

    # Remove foreignObject (can embed HTML)
    content = re.sub(r'<foreignObject\b[^>]*>.*?</foreignObject>', '', content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r'<foreignObject\b[^>]*/?>', '', content, flags=re.IGNORECASE)

    return content


# Thread pool for pathfinding with timeout support
_pathfinding_executor = ThreadPoolExecutor(
    max_workers=PATHFINDING_EXECUTOR_WORKERS, thread_name_prefix='pathfind')


def run_pathfinding_with_timeout(func, *args, timeout=25.0):
    """Run a pathfinding function with a hard timeout.

    Uses a thread pool executor to enforce wall-clock timeout. On timeout,
    a PRIVATE cancellation event is signalled so only this request's search
    stops — concurrent searches are unaffected.
    """
    cancel_event = threading.Event()

    def _runner():
        pathfinding.set_thread_cancel_event(cancel_event)
        try:
            return func(*args)
        finally:
            pathfinding.clear_thread_cancel_event()

    future = _pathfinding_executor.submit(_runner)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        # The worker keeps running until its next checkpoint; the private
        # event makes it exit cooperatively without touching other searches.
        cancel_event.set()
        return None, f"Timeout: wyszukiwanie trwało zbyt długo (>{timeout}s)"
    except Exception as e:
        return None, f"Błąd wyszukiwania: {e}"


# ============================================================
# Constants
# ============================================================
PUBLIC_DIR = os.path.realpath(data.PUBLIC_DIR)
_PUBLIC_HOST = PUBLIC_BASE_URL.split('//', 1)[-1]

BLOCKED_PREFIXES = _BLOCKED_PREFIXES
BLOCKED_PATHS = _BLOCKED_PATHS


# ============================================================
# Input validation
# ============================================================


def validate_group_id(group_id: str) -> tuple[bool, str | None]:
    """Validate a stop group ID.

    Returns (is_valid, error_message). If valid, error_message is None.
    """
    if not group_id:
        return False, 'Missing group ID'
    if len(group_id) > _MAX_GROUP_ID_LEN:
        return False, 'Group ID too long'
    if not _GROUP_ID_PATTERN.match(group_id):
        return False, 'Invalid group ID format (expected group_<number>)'
    return True, None


def validate_distance(distance_str: str) -> tuple[bool, float | None, str | None]:
    """Validate and parse a distance parameter.

    Returns (is_valid, distance_value, error_message).
    """
    if not distance_str:
        return False, None, 'Missing distance parameter'
    if len(distance_str) > MAX_DISTANCE_PARAM_LENGTH:
        return False, None, 'Distance parameter too long'
    try:
        distance = float(distance_str)
    except (ValueError, TypeError):
        return False, None, 'Invalid distance parameter'
    if not math.isfinite(distance):
        return False, None, 'Invalid distance parameter'
    if distance < 0:
        distance = 0.0
    return True, distance, None


def validate_mode(mode: str) -> str:
    """Validate and normalize a route mode parameter."""
    if mode not in ('short', 'convenient', 'cheap'):
        return 'short'
    return mode


# ============================================================
# Rate limiting (token bucket per IP, O(1) amortized)
# Limits come from config.py — the single source of truth.
# ============================================================
_rate_limits = {}
_rate_limits_lock = threading.RLock()
_Rate_limit_cleanup_thread = None
_Rate_limit_cleanup_stop = threading.Event()


def _start_rate_limit_cleanup():
    """Start the background thread for rate limit cleanup."""
    global _Rate_limit_cleanup_thread
    if _Rate_limit_cleanup_thread is not None:
        return
    _Rate_limit_cleanup_stop.clear()
    _Rate_limit_cleanup_thread = threading.Thread(
        target=_rate_limit_cleanup_loop,
        daemon=True,
        name='rate-limit-cleanup'
    )
    _Rate_limit_cleanup_thread.start()


def _rate_limit_cleanup_loop():
    """Background loop to clean up stale rate limit entries."""
    while not _Rate_limit_cleanup_stop.wait(RATE_LIMIT_CLEANUP_INTERVAL_SECONDS):
        try:
            _cleanup_stale_rate_limits()
        except Exception:
            pass  # Best effort


def _cleanup_stale_rate_limits():
    """Remove stale rate limit entries. Called from background thread."""
    now = time.time()
    stale_cutoff = now - _RATE_LIMIT_WINDOW * RATE_LIMIT_STALE_CUTOFF_MULTIPLIER
    with _rate_limits_lock:
        stale_ips = [
            k for k, v in _rate_limits.items()
            if (not v['normal'] or v['normal'][-1] < stale_cutoff)
            and (not v['expensive'] or v['expensive'][-1] < stale_cutoff)
        ]
        for k in stale_ips:
            del _rate_limits[k]


def _get_client_ip(handler):
    """Get real client IP.

    When TRUST_PROXY_HEADERS is enabled, X-Real-IP is trusted — it must be
    set by the reverse proxy from $remote_addr and cannot be influenced by
    the client; X-Forwarded-For may contain client-supplied values, so it is
    only used as a fallback. With TRUST_PROXY_HEADERS=false (default, direct
    exposure) the socket address is always used, so a spoofed header cannot
    rotate rate-limit buckets.
    """
    if TRUST_PROXY_HEADERS:
        real = handler.headers.get('X-Real-IP', '')
        if real:
            return real.strip()
        forwarded = handler.headers.get('X-Forwarded-For', '')
        if forwarded:
            first = forwarded.split(',')[0].strip()
            if first:
                return first
    return handler.client_address[0] if handler.client_address else 'unknown'


def _rate_limit_ok(ip, expensive=False):
    """Check rate limit. O(1) amortized — cleanup runs in background thread."""
    now = time.time()

    with _rate_limits_lock:
        buckets = _rate_limits.get(ip)
        if buckets is None:
            buckets = {'normal': deque(), 'expensive': deque()}
            _rate_limits[ip] = buckets

        key = 'expensive' if expensive else 'normal'
        timestamps = buckets[key]

        # Remove expired timestamps (O(1) amortized — each timestamp removed once)
        while timestamps and timestamps[0] < now - _RATE_LIMIT_WINDOW:
            timestamps.popleft()

        limit = _RATE_LIMIT_EXPENSIVE_MAX if expensive else _RATE_LIMIT_MAX
        if len(timestamps) >= limit:
            return False

        timestamps.append(now)

        return True


# ============================================================
# Pre-computed static JSON responses (cached at startup)
# ============================================================
_server_start_time = time.time()
_req_counters = {  # lightweight server telemetry (GIL-protected ints)
    'api_total': 0,
    'find_route': 0,
    'og_image': 0,
    'rate_limited': 0,
    'static_total': 0,
    'blocked': 0,
}
_max_from_prev_reqs = 0
_req_counters_lock = threading.Lock()


def _bump_counter(key, n=1):
    with _req_counters_lock:
        _req_counters[key] += n


_cached_stops_json = None
_cached_routes_json = None
_cached_stops_json_gz = None
_cached_routes_json_gz = None


def _build_stops_json():
    """Build cached stops JSON response."""
    global _cached_stops_json, _cached_stops_json_gz
    result = []
    for g in data.stops_grouped.values():
        result.append({
            'id': g['id'],
            'name': g['name'],
            'lat': round(g['lat'], 6),
            'lon': round(g['lon'], 6),
            'modes': g['modes'],
            'platform_count': len(g['platforms']),
        })
    _cached_stops_json = json.dumps(result, ensure_ascii=False).encode('utf-8')
    _cached_stops_json_gz = gzip.compress(_cached_stops_json, compresslevel=6)


def _build_routes_json():
    """Build cached routes JSON response."""
    global _cached_routes_json, _cached_routes_json_gz
    _cached_routes_json = json.dumps(data.routes_list, ensure_ascii=False).encode('utf-8')
    _cached_routes_json_gz = gzip.compress(_cached_routes_json, compresslevel=6)


def html_escape(s):
    """Escape a string for safe insertion into HTML."""
    return html.escape(str(s), quote=True)


# ============================================================
# HTTP Request Handler
# ============================================================

_OG_META_RE = re.compile(
    r'<meta\s+(property|name)="(og:title|og:description|og:url|og:image|'
    r'twitter:title|twitter:description|twitter:image)"\s+content="[^"]*"\s*/?>')


def _rewrite_og_meta(page_html, values):
    """Rewrite OG/Twitter meta tag contents by key.

    Regex-based (not exact-string replace), so editing the default texts
    in index.html cannot silently break crawler rewriting.
    """
    def _sub(m):
        attr, key = m.group(1), m.group(2)
        value = values.get(key)
        if value is None:
            return m.group(0)
        return f'<meta {attr}="{key}" content="{value}">'
    return _OG_META_RE.sub(_sub, page_html)


class MPKRequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler for MPK Kraków app."""

    server_version = ''
    sys_version = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC_DIR, **kwargs)

    def do_HEAD(self):
        self._head_only = True
        self.do_GET()

    def do_POST(self):
        self.send_error_page(405)

    def do_PUT(self):
        self.send_error_page(405)

    def do_DELETE(self):
        self.send_error_page(405)

    def do_PATCH(self):
        self.send_error_page(405)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, HEAD, OPTIONS')
        self.end_headers()

    def _send_body(self, body):
        """Write the response body, unless this is a HEAD request."""
        if not getattr(self, '_head_only', False):
            self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith('/api/'):
            self.handle_api(path, query)
            return

        # Rate limit static file requests
        client_ip = _get_client_ip(self)
        if not _rate_limit_ok(client_ip):
            self.send_error_page(429)
            return

        if path in BLOCKED_PATHS or any(path.startswith(p) for p in BLOCKED_PREFIXES):
            self.send_error_page(404)
            return

        # Serve favicon.svg
        if path == '/favicon.svg':
            favicon_path = os.path.join(PUBLIC_DIR, 'favicon.svg')
            if os.path.isfile(favicon_path):
                with open(favicon_path, 'r', encoding='utf-8') as f:
                    body = f.read().encode('utf-8')
            else:
                body = data.logo_svg_content.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', f'public, max-age={FAVICON_CACHE_MAX_AGE}')
            self.end_headers()
            self._send_body(body)
            return

        if path == '/' or path == '':
            path = '/index.html'

        file_path = os.path.realpath(os.path.normpath(
            os.path.join(PUBLIC_DIR, path.lstrip('/'))))
        if os.path.commonpath([PUBLIC_DIR, file_path]) != PUBLIC_DIR:
            self.send_error_page(403)
            return

        if not os.path.isfile(file_path):
            self.send_error_page(404)
            return

        # Crawler detection for OG meta rewriting
        user_agent = self.headers.get('User-Agent', '').lower()
        is_crawler = any(bot in user_agent for bot in _BOT_LIST)

        from_stop = query.get('from', [''])[0]
        to_stop = query.get('to', [''])[0]
        mode = query.get('mode', ['short'])[0]

        if is_crawler and from_stop and to_stop and path in ('/', '', '/index.html'):
            self.serve_modified_html(from_stop, to_stop, mode)
            return

        # Static assets: serve directly with long-lived caching. Versioned
        # URLs (?v=...) are immutable (cache for a year); unversioned assets
        # get 1 hour — enough to cut repeat page-load traffic.
        if path.endswith(('.js', '.css', '.svg', '.png', '.ico')):
            try:
                with open(file_path, 'rb') as f:
                    body = f.read()
            except OSError:
                self.send_error_page(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', self.guess_type(file_path))
            self.send_header('Content-Length', str(len(body)))
            if 'v' in query:
                self.send_header(
                    'Cache-Control',
                    f'public, max-age={STATIC_CACHE_MAX_AGE_VERSIONED}, immutable')
            else:
                self.send_header(
                    'Cache-Control',
                    f'public, max-age={STATIC_CACHE_MAX_AGE_UNVERSIONED}')
            self.end_headers()
            self._send_body(body)
            return

        # Serve index.html with CSP nonce injection.
        # Generic: every external <script> gets the nonce, so bumping asset
        # versions or adding scripts in index.html cannot silently break CSP.
        if path == '/index.html':
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    page_html = f.read()
                # Generate the nonce up front — _security_headers() reuses it
                # when the headers are actually sent below.
                nonce = getattr(self, '_csp_nonce', None) or generate_nonce()
                self._csp_nonce = nonce

                def _add_nonce(m):
                    attrs = m.group(1)
                    if 'nonce=' in attrs or 'application/ld+json' in attrs:
                        return m.group(0)
                    return f'<script{attrs} nonce="{nonce}">'

                page_html = re.sub(r'<script\b([^>]*)>', _add_nonce, page_html)
                body = page_html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self._send_body(body)
                return
            except Exception:
                pass  # Fall through to default handler

        super().do_GET()

    def serve_modified_html(self, from_stop, to_stop, mode):
        """Serve HTML with modified OG tags for crawlers."""
        try:
            file_path = os.path.join(PUBLIC_DIR, 'index.html')
            with open(file_path, 'r', encoding='utf-8') as f:
                page_html = f.read()

            if mode not in ('short', 'convenient', 'cheap'):
                mode = 'short'

            from_name = "Przystanek początkowy"
            to_name = "Przystanek końcowy"

            from_group = data.stops_grouped.get(from_stop)
            to_group = data.stops_grouped.get(to_stop)

            if from_group:
                from_name = from_group['name']
            if to_group:
                to_name = to_group['name']

            from_name_esc = html_escape(from_name)
            to_name_esc = html_escape(to_name)
            # Use proper URL encoding for query parameters
            from_stop_url = urllib.parse.quote(from_stop, safe='')
            to_stop_url = urllib.parse.quote(to_stop, safe='')
            mode_url = urllib.parse.quote(mode, safe='')

            og_image_url = f"{PUBLIC_BASE_URL}/api/og-image?from={from_stop_url}&to={to_stop_url}&mode={mode_url}"
            current_url = f"{PUBLIC_BASE_URL}/?from={from_stop_url}&to={to_stop_url}&mode={mode_url}"
            title = f"Za Ile Przejadę? {from_name_esc} → {to_name_esc}"
            description = f"Oblicz koszt przejazdu z {from_name_esc} do {to_name_esc} w nowym systemie biletów komunikacji miejskiej w Krakowie."

            page_html = _rewrite_og_meta(page_html, {
                'og:title': html_escape(title),
                'og:description': html_escape(description),
                'og:url': current_url,
                'og:image': og_image_url,
                'twitter:title': html_escape(title),
                'twitter:description': html_escape(description),
                'twitter:image': og_image_url,
            })

            body = page_html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self._send_body(body)

        except Exception:
            super().do_GET()

    # ------------------------------------------------------------
    # API routing
    # ------------------------------------------------------------
    def handle_api(self, path, query):
        """Handle API requests."""
        try:
            _bump_counter('api_total')
            # Only truly expensive endpoints (route search, OG image) are
            # rate-limited. Cheap pre-computed/lookup endpoints must never
            # 429 a page load — a burst on a shared IP would blank the map.
            expensive = path in ('/api/find-route', '/api/og-image')
            if expensive:
                client_ip = _get_client_ip(self)
                if not _rate_limit_ok(client_ip, expensive=True):
                    _bump_counter('rate_limited')
                    self.serve_json({'error': 'Zbyt wiele zapytań. Spróbuj ponownie za chwilę.'}, status=429)
                    return

            if path == '/api/stops':
                self.serve_json_cached(path)

            elif path == '/api/stops/search':
                self._handle_stops_search(query)

            elif path == '/api/stop-platforms':
                self._handle_stop_platforms(query)

            elif path == '/api/find-route':
                self._handle_find_route(query)

            elif path == '/api/cost':
                self._handle_cost(query)

            elif path == '/api/pricing':
                self.serve_json({
                    'base_distance_km': cost.BASE_DISTANCE,
                    'base_cost_regular': cost.BASE_COST_REGULAR,
                    'base_cost_reduced': cost.BASE_COST_REDUCED,
                    'segment_distance_km': cost.SEGMENT_DISTANCE,
                    'segment_cost_regular': cost.SEGMENT_COST_REGULAR,
                    'segment_cost_reduced': cost.SEGMENT_COST_REDUCED,
                    'max_cost_regular': cost.MAX_COST_REGULAR,
                    'max_cost_reduced': cost.MAX_COST_REDUCED,
                    'max_daily_cost_regular': cost.MAX_DAILY_COST_REGULAR,
                    'max_daily_cost_reduced': cost.MAX_DAILY_COST_REDUCED,
                })

            elif path == '/api/shapes':
                self._handle_shapes(query)

            elif path == '/api/health':
                # Trigger deferred cache warmup on first successful health
                # check. The entry point (server.py) registers the callback —
                # the handler must not import the entry script directly.
                on_health = getattr(type(self), 'on_health_check', None)
                if on_health:
                    try:
                        on_health()
                    except Exception:
                        pass
                # Verify data integrity
                if not data.stops_grouped or not data.adjacency:
                    self.serve_json({'status': 'degraded', 'error': 'Data not loaded'}, status=503)
                    return
                self.serve_json({'status': 'ok'})

            elif path == '/api/status':
                self._handle_status()

            elif path == '/api/version':
                self.serve_json({'version': APP_VERSION})

            elif path == '/api/data-info':
                meta = data.feed_metadata
                self.serve_json({
                    'version': meta.get('version', ''),
                    'start_date': meta.get('start_date', ''),
                    'end_date': meta.get('end_date', ''),
                    'publisher': meta.get('publisher', ''),
                    'url': meta.get('url', ''),
                })

            elif path == '/api/routes':
                self.serve_json_cached(path)

            elif path == '/api/stop':
                self._handle_stop_info(query)

            elif path == '/api/og-image':
                self._handle_og_image(query)

            else:
                self.serve_json({'error': 'Unknown API endpoint'}, status=404)

        except Exception:
            traceback.print_exc(file=sys.stderr)
            # If a response already went out (partially), starting another
            # one would only raise again — give up quietly.
            if getattr(self, '_headers_sent', False):
                return
            try:
                # Suppress the default HTML error page so the response stays JSON
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(b'{"error": "Internal server error"}')))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self._send_body(b'{"error": "Internal server error"}')
            except OSError:
                pass

    # ------------------------------------------------------------
    # Individual API handlers
    # ------------------------------------------------------------
    def _handle_stops_search(self, query):
        q = query.get('q', [''])[0].lower().strip()
        if len(q) > SEARCH_MAX_QUERY_LENGTH:
            q = q[:SEARCH_MAX_QUERY_LENGTH]
        if len(q) < SEARCH_MIN_QUERY_LENGTH:
            self.serve_json([])
            return
        results = []
        seen = set()
        # Prefix-based search (fast)
        for prefix_len in range(min(SEARCH_PREFIX_MAX_LENGTH, len(q)), 1, -1):
            prefix = q[:prefix_len]
            if prefix in data.stop_search_index:
                for name_lower, group_id in data.stop_search_index[prefix]:
                    if group_id not in seen and q in name_lower:
                        seen.add(group_id)
                        g = data.stops_grouped[group_id]
                        results.append({
                            'id': g['id'],
                            'name': g['name'],
                            'lat': round(g['lat'], 6),
                            'lon': round(g['lon'], 6),
                            'modes': g['modes'],
                            'platform_count': len(g['platforms']),
                        })
                if results:
                    break
        # Fallback: full scan (for queries that don't match any prefix)
        if not results:
            for name_lower, group_ids in data.stops_by_name_grouped.items():
                if q in name_lower:
                    for group_id in group_ids:
                        if group_id not in seen:
                            seen.add(group_id)
                            g = data.stops_grouped[group_id]
                            results.append({
                                'id': g['id'],
                                'name': g['name'],
                                'lat': round(g['lat'], 6),
                                'lon': round(g['lon'], 6),
                                'modes': g['modes'],
                                'platform_count': len(g['platforms']),
                            })
        self.serve_json(results[:SEARCH_MAX_RESULTS])

    def _handle_stop_platforms(self, query):
        group_id = query.get('id', [''])[0]
        valid, error = validate_group_id(group_id)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return
        group = data.stops_grouped.get(group_id)
        if group:
            self.serve_json({
                'id': group['id'],
                'name': group['name'],
                'lat': round(group['lat'], 6),
                'lon': round(group['lon'], 6),
                'modes': group['modes'],
                'platforms': group['platforms'],
            })
        else:
            self.serve_json({'error': 'Stop group not found'}, status=404)

    def _handle_find_route(self, query):
        from_stop = query.get('from', [''])[0]
        to_stop = query.get('to', [''])[0]

        valid, error = validate_group_id(from_stop)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return
        valid, error = validate_group_id(to_stop)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return

        # Always compute all modes in one call, with timeout protection
        _bump_counter('find_route')
        result = run_pathfinding_with_timeout(
            pathfinding.find_route_between_groups,
            from_stop, to_stop, 'both',
            timeout=25.0
        )

        # run_pathfinding_with_timeout returns the triple on success,
        # or (None, error_message) on timeout/exception
        if isinstance(result, tuple) and len(result) == 3:
            short_pair, convenient_pair, cheap_pair = result
        else:
            # Timeout or error
            error_msg = result[1] if isinstance(result, tuple) and len(result) == 2 else "Nie znaleziono trasy między tymi przystankami"
            self.serve_json({'error': error_msg})
            return

        short_result, short_error = short_pair
        convenient_result, convenient_error = convenient_pair
        cheap_result, cheap_error = cheap_pair

        if (short_result is None and convenient_result is None
                and cheap_result is None):
            self.serve_json({'error': short_error or convenient_error or cheap_error
                             or "Nie znaleziono trasy między tymi przystankami"})
        else:
            self.serve_json({
                'short': short_result,
                'convenient': convenient_result,
                'cheap': cheap_result,
            })

    def _handle_cost(self, query):
        distance_str = query.get('distance', ['0'])[0]
        valid, distance, error = validate_distance(distance_str)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return
        cost_reg, cost_red = cost.calculate_cost(distance)
        self.serve_json({
            'distance': distance,
            'cost_regular': cost_reg,
            'cost_reduced': cost_red,
        })

    def _handle_shapes(self, query):
        route_id = query.get('route_id', [''])[0]
        if len(route_id) > MAX_ROUTE_ID_LENGTH:
            route_id = route_id[:MAX_ROUTE_ID_LENGTH]
        if not route_id:
            self.serve_json({'error': 'Missing route_id parameter'}, status=400)
            return
        shape = data.route_shapes.get(route_id, [])
        self.serve_json({'route_id': route_id, 'shape': shape})

    def _handle_status(self):
        """Application-level status: version, uptime, cache usage.

        Deliberately excludes host internals (load average, RSS, per-process
        CPU, request counters) — this endpoint is publicly reachable.
        """
        fc_count, fc_bytes, fc_max = pathfinding.find_cache_info()
        rc_count, rc_max, rc_bytes, rc_max_bytes = pathfinding.route_cache_info()
        cheap_searches, cheap_timeouts = pathfinding.cheap_search_info()
        active = 0
        server = getattr(self, 'server', None)
        if server is not None:
            with server._active_lock:
                active = server._active_requests
        self.serve_json({
            'version': APP_VERSION,
            'uptime_seconds': int(time.time() - _server_start_time),
            'active_requests': active,
            'max_concurrent': MAX_CONCURRENT_REQUESTS,
            'find_cache': {
                'entries': rc_count + fc_count,
                'find_entries': fc_count,
                'find_bytes': fc_bytes,
                'find_max_bytes': fc_max,
                'route_entries': rc_count,
                'route_max': rc_max,
                'route_bytes': rc_bytes,
                'route_max_bytes': rc_max_bytes,
            },
            'cheap': {
                'searches': cheap_searches,
                'timeouts': cheap_timeouts,
            },
        })

    def _handle_stop_info(self, query):
        stop_id = query.get('id', [''])[0]
        if len(stop_id) > MAX_STOP_ID_LENGTH:
            stop_id = stop_id[:MAX_STOP_ID_LENGTH]
        if not stop_id:
            self.serve_json({'error': 'Missing id parameter'}, status=400)
            return
        stop = data.stops_by_id.get(stop_id)
        if stop:
            self.serve_json(stop)
        else:
            self.serve_json({'error': 'Stop not found'}, status=404)

    def _handle_og_image(self, query):
        _bump_counter('og_image')
        from_stop = query.get('from', [''])[0]
        to_stop = query.get('to', [''])[0]
        mode = validate_mode(query.get('mode', ['short'])[0])

        valid, error = validate_group_id(from_stop)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return
        valid, error = validate_group_id(to_stop)
        if not valid:
            self.serve_json({'error': error}, status=400)
            return

        svg = self._generate_og_image_svg(from_stop, to_stop, mode)
        body = svg.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', f'public, max-age={OG_IMAGE_CACHE_MAX_AGE}')
        self.end_headers()
        self._send_body(body)

    # ------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------
    def _security_headers(self):
        """Add security headers to a response."""
        # Reuse a nonce generated earlier during request handling (HTML
        # injection) so header and body always agree.
        nonce = getattr(self, '_csp_nonce', None) or generate_nonce()
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-XSS-Protection', '1; mode=block')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            # 'unsafe-inline' for styles is required: the initial hidden state
            # of modals/panels uses style="display:none" ATTRIBUTES in HTML,
            # and CSP cannot allowlist those with a nonce (only <style>
            # elements). Scripts stay strictly nonce-gated.
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        # Store nonce for use in templates
        self._csp_nonce = nonce

    def end_headers(self):
        """Send security headers on ALL responses."""
        self._headers_sent = True
        self._security_headers()
        self.send_header('X-App-Version', APP_VERSION)
        super().end_headers()

    def serve_json(self, data_obj, status=200):
        """Serve JSON response with optional gzip compression."""
        body = json.dumps(data_obj, ensure_ascii=False).encode('utf-8')
        accept = self.headers.get('Accept-Encoding', '')
        if len(body) > 1024 and 'gzip' in accept:
            body = gzip.compress(body, compresslevel=6)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
        else:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
        self._send_body(body)

    def serve_json_cached(self, path):
        """Serve pre-computed JSON for static endpoints."""
        global _cached_stops_json, _cached_routes_json
        global _cached_stops_json_gz, _cached_routes_json_gz

        if path == '/api/stops':
            if _cached_stops_json is None:
                _build_stops_json()
            body = _cached_stops_json
            body_gz = _cached_stops_json_gz
        elif path == '/api/routes':
            if _cached_routes_json is None:
                _build_routes_json()
            body = _cached_routes_json
            body_gz = _cached_routes_json_gz
        else:
            return

        accept = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body_gz)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self._send_body(body_gz)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self._send_body(body)

    def send_error_page(self, code):
        """Send a minimal error response."""
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    # ------------------------------------------------------------
    # OG image generation
    # ------------------------------------------------------------
    def _generate_og_image_svg(self, from_stop_id, to_stop_id, mode):
        """Generate OG image as SVG."""
        from_name = "Przystanek początkowy"
        to_name = "Przystanek końcowy"
        cost_text = ""

        if from_stop_id and to_stop_id:
            from_group = data.stops_grouped.get(from_stop_id)
            to_group = data.stops_grouped.get(to_stop_id)
            if from_group:
                from_name = from_group['name']
            if to_group:
                to_name = to_group['name']
            # For a single mode find_route_between_groups returns
            # (result, error) directly — not a pair of pairs.
            # Use timeout wrapper for protection
            result, _ = run_pathfinding_with_timeout(
                pathfinding.find_route_between_groups,
                from_stop_id, to_stop_id, mode,
                timeout=15.0
            )
            if result:
                cost_text = f"{result['cost_regular']:.2f} / {result['cost_reduced']:.2f} zł"

        def esc(s):
            return html.escape(str(s), quote=True)

        def truncate_name(name, max_chars=26):
            if len(name) <= max_chars:
                return name
            cut = name[:max_chars - 3]
            last_space = cut.rfind(' ')
            if last_space > max_chars * 0.5:
                cut = cut[:last_space]
            return cut.rstrip() + '...'

        from_name = truncate_name(from_name)
        to_name = truncate_name(to_name)

        from_name_esc = esc(from_name)
        to_name_esc = esc(to_name)
        cost_text_esc = esc(cost_text)

        from_fit = ' textLength="820" lengthAdjust="spacingAndGlyphs"' if len(from_name) > 24 else ''
        to_fit = ' textLength="820" lengthAdjust="spacingAndGlyphs"' if len(to_name) > 24 else ''

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630">\n'
            '  <defs>\n'
            '    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">\n'
            '      <stop offset="0%" stop-color="#0d2137"/>\n'
            '      <stop offset="100%" stop-color="#1a3a52"/>\n'
            '    </linearGradient>\n'
            '  </defs>\n'
            '  <rect width="1200" height="630" fill="url(#bg)"/>\n'
            '\n'
            '  <text x="80" y="77" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#5dade2">SKĄD</text>\n'
            f'  <text x="80" y="145" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1"{from_fit}>{from_name_esc}</text>\n'
            '  <text x="80" y="230" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#5dade2">DOKĄD</text>\n'
            f'  <text x="80" y="298" font-family="Arial, Helvetica, sans-serif" font-size="55" font-weight="bold" fill="#ecf0f1"{to_fit}>{to_name_esc}</text>\n'
            '  <line x1="80" y1="346" x2="240" y2="346" stroke="#5dade2" stroke-width="4"/>\n'
            '  <text x="80" y="431" font-family="Arial, Helvetica, sans-serif" font-size="36" font-weight="normal" fill="#5dade2">CENA BILETU</text>\n'
            f'  <text x="80" y="548" font-family="Arial, Helvetica, sans-serif" font-size="120" font-weight="bold" fill="#ffffff">{cost_text_esc}</text>\n'
            f'  <g transform="translate(930,400) scale(2.5)">\n'
        )
        logo_inner = re.sub(r'<svg[^>]*>', '', data.logo_svg_content, count=1)
        logo_inner = re.sub(r'</svg>', '', logo_inner)
        logo_inner = sanitize_svg(logo_inner)
        svg += f'  {logo_inner.strip()}\n'
        svg += '  </g>\n'
        svg += (
            f'  <text x="1010" y="577" font-family="Arial, Helvetica, sans-serif" font-size="32" font-weight="bold" fill="#7f8c8d" text-anchor="middle">{_PUBLIC_HOST}</text>\n'
            '</svg>'
        )
        return svg

    def log_message(self, format, *args):
        """Log API requests and errors through the structured logger."""
        msg = format % args
        if '/api/' in msg or 'Error' in msg or 'error' in msg:
            get_logger('mpk.http').info('HTTP access: %s', msg)
