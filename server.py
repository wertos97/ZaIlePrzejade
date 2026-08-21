#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Entry point — loads modules and starts the threaded server.
"""

import os
import random
import threading
import time

from server.data import (
    BASE_DIR, PUBLIC_DIR, APP_VERSION, stops_grouped,
)
from server.pathfinding import (
    find_route_between_groups, init_pathfinding,
    find_cache_info, route_cache_info,
)
from server.handler import MPKRequestHandler, _build_stops_json, _build_routes_json
from server.logging_config import setup_logging, get_logger, log_cache_event


# Import threading server from stdlib and wrap it
from socketserver import ThreadingMixIn
from http.server import HTTPServer


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread, with a concurrency limit."""
    daemon_threads = True
    request_queue_size = 64
    _active_requests = 0
    _active_lock = threading.Lock()
    MAX_CONCURRENT = 20

    def process_request(self, request, client_address):
        """Limit concurrent requests to prevent resource exhaustion."""
        from server import handler as _h
        with self._active_lock:
            if self._active_requests >= self.MAX_CONCURRENT:
                _h._bump_counter('blocked')
                try:
                    request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                    b'Content-Type: text/plain; charset=utf-8\r\n'
                                    b'Content-Length: 0\r\n'
                                    b'Connection: close\r\n'
                                    b'Retry-After: 1\r\n'
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
        """Run the request in a thread, decrementing the active counter when done."""
        from server import handler as _h
        _h._active_requests += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            _h._active_requests -= 1
            with self._active_lock:
                self._active_requests -= 1


# ============================================================
# Initialize pathfinding module (needs data structures from data module)
# ============================================================
from server.data import (
    adjacency, stops_by_id, stops_grouped as _sg,
    stop_to_group, routes_by_id, route_shapes,
)

# Setup structured logging
log_level = os.environ.get('LOG_LEVEL', 'INFO')
log_file = os.environ.get('LOG_FILE')  # Optional: /var/log/mpk/app.log
setup_logging(level=log_level, log_file=log_file)
logger = get_logger('mpk.server')

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
_warmup_done = False
_warmup_lock = threading.Lock()


def _warmup_cache():
    """Pre-compute routes for popular stop pairs to warm up caches.

    Kept light on purpose: the route cache is restored from disk anyway, and
    this thread must not steal the GIL from real requests right after boot.
    Each search yields the GIL briefly so early page loads stay fast.
    """
    global _warmup_done
    group_ids = list(stops_grouped.keys())
    sample = random.sample(group_ids, min(40, len(group_ids)))

    count = 0
    for i, g1 in enumerate(sample):
        for g2 in sample[i+1:i+3]:
            try:
                find_route_between_groups(g1, g2)
                count += 1
            except Exception:
                pass
            time.sleep(0.05)  # yield the GIL — don't starve live requests

    logger.info('Cache warmup completed', extra={'entries_warmed': count})
    with _warmup_lock:
        _warmup_done = True


def trigger_warmup():
    """Start the warmup thread if not already done."""
    with _warmup_lock:
        if not _warmup_done:
            threading.Thread(target=_warmup_cache, daemon=True).start()


def main():
    port = int(os.environ.get('PORT', 8080))

    server = ThreadedHTTPServer(('0.0.0.0', port), MPKRequestHandler)

    # Start background rate limit cleanup thread
    from server.handler import _start_rate_limit_cleanup
    _start_rate_limit_cleanup()

    # Warmup is deferred until first health check passes (via trigger_warmup)
    # to avoid stealing GIL from early requests.
    logger.info('Server ready', extra={
        'port': port,
        'public_dir': PUBLIC_DIR,
        'version': APP_VERSION,
    })

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Server shutting down')
        server.shutdown()


if __name__ == '__main__':
    main()
