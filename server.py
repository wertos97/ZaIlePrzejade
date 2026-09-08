#!/usr/bin/env python3
"""
HTTP Server for MPK Kraków Ticket Cost Calculator.
Entry point — loads modules and starts the threaded server.
"""

import os
import signal
import sys
import threading
import time

from server.config import (
    APP_VERSION,
    DEFAULT_PORT,
    LOG_LEVEL,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_QUEUE_SIZE,
)
from server.logging_config import setup_logging, get_logger, log_cache_event

# Configure logging FIRST so startup logs from data loading are captured.
setup_logging(level=LOG_LEVEL, log_file=os.environ.get('LOG_FILE'))
logger = get_logger('mpk.server')


def _ensure_gtfs_data():
    """Generate processed GTFS data on first boot (fresh instances).

    Processed JSONs are NOT in git (generated per instance). If any are
    missing, run process_gtfs.py synchronously (downloads + processes,
    several minutes). Fatal when generation fails — without data the
    server cannot serve anything.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    processed = os.path.join(base, 'processed')
    needed = ('stops.json', 'routes.json', 'adjacency.json', 'shapes.json',
              'metadata.json')
    missing = [n for n in needed
               if not os.path.isfile(os.path.join(processed, n))]
    if not missing:
        return
    logger.warning('Missing GTFS data (%s) — generating from scratch. '
                   'First boot takes several minutes.',
                   ', '.join(missing))
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, '-u', os.path.join(base, 'process_gtfs.py')],
            cwd=base, timeout=20 * 60)
        ok = result.returncode == 0
    except Exception as e:
        logger.critical('GTFS generation failed: %s', e)
        ok = False
    still_missing = [n for n in needed
                     if not os.path.isfile(os.path.join(processed, n))]
    if not ok or still_missing:
        logger.critical('GTFS data unavailable (%s) — cannot start.',
                        ', '.join(still_missing))
        sys.exit(1)
    logger.warning('GTFS data ready.')


_ensure_gtfs_data()

from server.data import (  # noqa: E402 — needs logging configured first
    PUBLIC_DIR,
)
from server.pathfinding import (
    init_pathfinding,
    find_cache_info,
    route_cache_info,
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
        with self._active_lock:
            if self._active_requests >= self.MAX_CONCURRENT:
                try:
                    # Raw response (no handler instance yet) — keep the same
                    # security headers the handler would send.
                    body = b'{"error":"Serwer jest przeciazony. Sprobuj ponownie za chwile."}'
                    request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                    b'Content-Type: application/json; charset=utf-8\r\n'
                                    b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
                                    b'Connection: close\r\n'
                                    b'X-Content-Type-Options: nosniff\r\n'
                                    b'X-Frame-Options: DENY\r\n'
                                    b'Referrer-Policy: no-referrer\r\n'
                                    b'\r\n'
                                    + body)
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
        """Run the request in a thread, decrementing the active counter
        (incremented under lock by process_request) when handling ends."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active_requests -= 1


# ============================================================
# Initialize pathfinding module (needs data structures from data module)
# ============================================================
from server.data import (
    adjacency, stops_by_id, stops_grouped as _sg,
    stop_to_group, routes_by_id, route_shapes, feed_metadata,
)

logger.info('Server starting', extra={'version': APP_VERSION})

init_pathfinding(adjacency, stops_by_id, _sg, stop_to_group, routes_by_id, route_shapes)

# Reconcile an interrupted/finished GTFS update (cleans the backup only
# when on-disk data matches the expected new version).
from server import gtfs_update as _gtfs_update
_gtfs_update.reconcile_after_boot(feed_metadata)

# Pre-build cached JSON responses
_build_stops_json()
_build_routes_json()

log_cache_event(logger, 'find', 'startup', *find_cache_info())
_rc_count, _rc_max, _rc_bytes, _rc_max_bytes = route_cache_info()
log_cache_event(logger, 'route', 'startup', _rc_count, _rc_bytes, _rc_max_bytes)


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

    # Twice-daily GTFS freshness checks (VPS-only: no admin password file
    # → no scheduler; dev machines stay quiet).
    from server.handler import _start_gtfs_scheduler
    _start_gtfs_scheduler()

    # Trwały zapis restartu do statystyk panelu (VPS-only) — z powodem:
    # ostatni 'Powód' z autoupdate.log (update / naprawa) albo restart ręczny
    from server import admin_stats
    reason = admin_stats.read_last_reason(time.time())
    admin_stats.record_restart(APP_VERSION, reason=reason or 'ręczny start')

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
