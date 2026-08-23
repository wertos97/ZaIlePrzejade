#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Entry point — loads modules and starts the threaded server.
"""

import os
import random
import signal
import threading
import time

from server.config import (
    APP_VERSION,
    DEFAULT_PORT,
    LOG_LEVEL,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_QUEUE_SIZE,
    WARMUP_SAMPLE_SIZE,
    WARMUP_PAIRS_PER_SAMPLE,
    WARMUP_YIELD_DELAY_SECONDS,
)
from server.logging_config import setup_logging, get_logger, log_cache_event

# Configure logging FIRST so startup logs from data loading are captured.
setup_logging(level=LOG_LEVEL, log_file=os.environ.get('LOG_FILE'))
logger = get_logger('mpk.server')

from server.data import (  # noqa: E402 — needs logging configured first
    PUBLIC_DIR, stops_grouped,
)
from server.pathfinding import (
    find_route_between_groups, init_pathfinding,
    find_cache_info, route_cache_info,
)
from server.handler import MPKRequestHandler, _build_stops_json, _build_routes_json


# Import threading server from stdlib and wrap it
from socketserver import ThreadingMixIn
from http.server import HTTPServer


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread, with a concurrency limit."""
    daemon_threads = True
    request_queue_size = REQUEST_QUEUE_SIZE
    _active_requests = 0
    _active_lock = threading.Lock()
    MAX_CONCURRENT = MAX_CONCURRENT_REQUESTS

    def process_request(self, request, client_address):
        """Limit concurrent requests to prevent resource exhaustion."""
        from server import handler as _h
        with self._active_lock:
            if self._active_requests >= self.MAX_CONCURRENT:
                _h._bump_counter('blocked')
                try:
                    # Raw response (no handler instance yet) — keep the same
                    # security headers the handler would send.
                    request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                    b'Content-Type: text/plain; charset=utf-8\r\n'
                                    b'Content-Length: 0\r\n'
                                    b'Connection: close\r\n'
                                    b'Retry-After: 1\r\n'
                                    b'X-Content-Type-Options: nosniff\r\n'
                                    b'X-Frame-Options: DENY\r\n'
                                    b'Referrer-Policy: no-referrer\r\n'
                                    b'\r\n')
                except OSError:
                    pass
                finally:
                    try:
                        request.close()
                    except OSError:
                        pass
                return
            self._active_requests += 1

        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active_requests -= 1
            raise

    def process_request_thread(self, request, client_address):
        """Run the request in a thread; the active counter was already
        incremented (under lock) by process_request."""
        super().process_request_thread(request, client_address)


# ============================================================
# Initialize pathfinding module (needs data structures from data module)
# ============================================================
from server.data import (
    adjacency, stops_by_id, stops_grouped as _sg,
    stop_to_group, routes_by_id, route_shapes,
)

logger.info('Server starting', extra={'version': APP_VERSION})

init_pathfinding(adjacency, stops_by_id, _sg, stop_to_group, routes_by_id, route_shapes)

# Pre-build cached JSON responses
_build_stops_json()
_build_routes_json()

log_cache_event(logger, 'find', 'startup', *find_cache_info())
_rc_count, _rc_max, _rc_bytes, _rc_max_bytes = route_cache_info()
log_cache_event(logger, 'route', 'startup', _rc_count, _rc_bytes, _rc_max_bytes)


# ============================================================
# Warmup: pre-compute routes for popular stop pairs at startup
# ============================================================
_warmup_started = False
_warmup_lock = threading.Lock()


def _warmup_cache():
    """Pre-compute routes for popular stop pairs to warm up caches.

    Kept light on purpose: the route cache is restored from disk anyway, and
    this thread must not steal the GIL from real requests right after boot.
    Each search yields the GIL briefly so early page loads stay fast.
    """
    group_ids = list(stops_grouped.keys())
    sample = random.sample(group_ids, min(WARMUP_SAMPLE_SIZE, len(group_ids)))

    count = 0
    for i, g1 in enumerate(sample):
        for g2 in sample[i+1:i+1+WARMUP_PAIRS_PER_SAMPLE]:
            try:
                find_route_between_groups(g1, g2)
                count += 1
            except Exception:
                pass
            time.sleep(WARMUP_YIELD_DELAY_SECONDS)  # yield the GIL — don't starve live requests

    logger.info('Cache warmup completed', extra={'entries_warmed': count})


def trigger_warmup():
    """Start the warmup thread exactly once."""
    global _warmup_started
    with _warmup_lock:
        if _warmup_started:
            return
        _warmup_started = True
    threading.Thread(target=_warmup_cache, daemon=True,
                     name='cache-warmup').start()


def _request_shutdown(signum, frame):
    """Signal handler: break out of serve_forever via SystemExit so atexit
    handlers (route cache persistence) still run."""
    raise SystemExit(0)


def main():
    port = int(os.environ.get('PORT', DEFAULT_PORT))

    server = ThreadedHTTPServer(('0.0.0.0', port), MPKRequestHandler)

    # Start background rate limit cleanup thread
    from server.handler import _start_rate_limit_cleanup
    _start_rate_limit_cleanup()

    # Warmup is deferred until first health check passes (via trigger_warmup)
    # to avoid stealing GIL from early requests.
    MPKRequestHandler.on_health_check = staticmethod(trigger_warmup)

    signal.signal(signal.SIGTERM, _request_shutdown)

    logger.info('Server ready', extra={
        'port': port,
        'public_dir': PUBLIC_DIR,
        'version': APP_VERSION,
    })

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info('Server shutting down')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
