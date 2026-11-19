"""Panel admina (VPS-only, hasło w processed/): brama dostępu, auth, dane."""

import json
import sys
import os
import unittest
import urllib.request
import urllib.error
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import admin_stats

# populate the pathfinding module state (server.py normally does this).
# Without it every find-route instantly fails with "not found", and those
# bogus instant errors get recorded as outcome 'timeout' — polluting both
# the dev database and any test asserting on recorded details.
from server.pathfinding import init_pathfinding as _init_pf
from server import data as _data
_init_pf(
    _data.adjacency, _data.stops_by_id, _data.stops_grouped,
    _data.stop_to_group, _data.routes_by_id, _data.route_shapes,
)


class TestAdminPanel(unittest.TestCase):
    """/panel istnieje tylko gdy ustawiono hasło (plik w processed/, nigdy
    w git); dane za sesją; błędne hasło = 401 + lockout."""

    @classmethod
    def setUpClass(cls):
        from http.server import HTTPServer
        from socketserver import ThreadingMixIn
        from server import handler as handler_mod
        from server.config import APP_VERSION

        cls.handler_mod = handler_mod
        # handler import → admin_stats.init(processed) — dopiero teraz
        # _config_path() jest używalne
        cls._existed = os.path.isfile(admin_stats._config_path())
        cls._cfg_backup = None
        if cls._existed:
            with open(admin_stats._config_path(), 'rb') as f:
                cls._cfg_backup = f.read()
        admin_stats.set_password('test-pass-123')

        class TestServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            _active_lock = threading.Lock()
            _active_requests = 0

        cls.server = TestServer(('127.0.0.1', 0), handler_mod.MPKRequestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.from_id = list(handler_mod.data.stops_grouped.keys())[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls._cfg_backup is not None:
            with open(admin_stats._config_path(), 'wb') as f:
                f.write(cls._cfg_backup)
        elif os.path.isfile(admin_stats._config_path()):
            os.remove(admin_stats._config_path())

    def _get(self, path, headers=None):
        import urllib.request as u
        req = u.Request(self.base + path, headers=headers or {})
        try:
            with u.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _login(self, password='test-pass-wrong'):
        import urllib.request as u
        req = u.Request(self.base + '/api/admin/login', method='POST',
                        data=json.dumps({'password': password}).encode(),
                        headers={'Content-Type': 'application/json'})
        try:
            with u.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def test_panel_404_without_password(self):
        """Bez pliku hasła (atelier/dev) /panel i API = 404 — funkcja
        istnieje wyłącznie tam, gdzie skonfigurowano hasło."""
        if not self._existed:
            admin_stats  # import guard
            os.remove(admin_stats._config_path())
            try:
                status, _, _ = self._get('/panel')
                self.assertEqual(status, 404)
                status, _, _ = self._get('/api/admin/session')
                self.assertEqual(status, 404)
            finally:
                admin_stats.set_password('test-pass-123')
        # gdy hasło skonfigurowane — panel serwuje stronę (bez danych)
        status, headers, body = self._get('/panel')
        self.assertEqual(status, 200)
        self.assertIn(b'<title>Panel', body)

    def test_login_wrong_password_401(self):
        status, _ = self._login('definitely-wrong')
        self.assertEqual(status, 401)

    def test_login_correct_then_stats(self):
        status, headers = self._login('test-pass-123')
        self.assertEqual(status, 200)
        cookie = headers.get('Set-Cookie', '')
        self.assertIn('admin_session=', cookie)
        token = cookie.split('admin_session=')[1].split(';')[0]
        # sesja działa: stats zwracają JSON z kluczami
        status, _, body = self._get('/api/admin/stats',
                                    {'Cookie': f'admin_session={token}'})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('daily', data)
        self.assertIn('restarts', data)
        self.assertIn('updates', data)
        self.assertIn('visitors', next(iter(data['daily'].values())))

    def test_stats_requires_session(self):
        status, _, _ = self._get('/api/admin/stats')
        self.assertEqual(status, 401)

    def test_session_flow(self):
        # bez cookie → 401
        status, _, _ = self._get('/api/admin/session')
        self.assertEqual(status, 401)
        # po zalogowaniu → 200
        status, headers = self._login('test-pass-123')
        cookie = headers.get('Set-Cookie', '').split(';')[0]
        status, _, body = self._get('/api/admin/session',
                                    {'Cookie': cookie})
        self.assertEqual(status, 200)

    def test_events_recorded(self):
        """Wyszukiwanie trasy zapisuje zdarzenie (kind=request)."""
        status, _, body = self._get(
            f'/api/find-route?from={self.from_id}&to={self.from_id}')
        self.assertEqual(status, 200)
        # daj chwilę na zapis (synchroniczny w handlerze)
        self.assertGreaterEqual(self._count_events('request'), 1)

    def _post(self, path, payload, headers=None):
        import urllib.request as u
        req = u.Request(self.base + path, method='POST',
                        data=json.dumps(payload).encode(),
                        headers=dict({'Content-Type': 'application/json'},
                                     **(headers or {})))
        try:
            with u.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _authed(self):
        status, headers = self._login('test-pass-123')
        self.assertEqual(status, 200)
        return {'Cookie': headers.get('Set-Cookie', '').split(';')[0]}

    def test_gtfs_status_shape(self):
        status, _, body = self._get('/api/admin/gtfs-status',
                                    self._authed())
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ('current', 'last_check', 'last_done', 'job',
                    'migration_pending'):
            self.assertIn(key, data)
        self.assertIn('version', data['current'])

    def test_gtfs_update_requires_confirm(self):
        status, body = self._post('/api/admin/gtfs-update', {},
                                  self._authed())
        self.assertEqual(status, 400)
        self.assertIn('error', json.loads(body))

    def test_gtfs_endpoints_require_session(self):
        status, _, _ = self._get('/api/admin/gtfs-status')
        self.assertEqual(status, 401)
        status, _ = self._post('/api/admin/gtfs-update', {'confirm': True})
        self.assertEqual(status, 401)
        status, _ = self._post('/api/admin/gtfs-check', {})
        self.assertEqual(status, 401)
        status, _ = self._post('/api/admin/gtfs-cancel', {})
        self.assertEqual(status, 401)

    def test_gtfs_cancel_without_schedule(self):
        # Stubbed like test_gtfs_check_starts: this dev instance may have a
        # real pending install, and the endpoint must not cancel it just
        # because a test ran. We only verify the "nothing to cancel" shape.
        from server import gtfs_update as gu_mod
        orig = gu_mod.cancel_scheduled
        gu_mod.cancel_scheduled = lambda: False
        try:
            status, body = self._post('/api/admin/gtfs-cancel', {},
                                      self._authed())
        finally:
            gu_mod.cancel_scheduled = orig
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get('cancelled'))

    def test_gtfs_check_starts(self):
        # Worker stubbed — never touches the network in tests. State is
        # stubbed to idle: a stale job file from a real run must not
        # make this hermetic test flake with 409.
        from server import gtfs_update as gu_mod
        orig = gu_mod.run_check_job
        orig_state = gu_mod.read_state
        gu_mod.run_check_job = lambda: {'started': True}
        gu_mod.read_state = lambda: {'job': {'state': 'idle'},
                                     'last_check': None}
        try:
            status, body = self._post('/api/admin/gtfs-check', {},
                                      self._authed())
            self.assertEqual(status, 202)
            self.assertTrue(json.loads(body).get('started'))
        finally:
            gu_mod.run_check_job = orig
            gu_mod.read_state = orig_state

    def test_maintenance_endpoint_public(self):
        status, _, body = self._get('/api/maintenance')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('active', data)
        self.assertFalse(data['active'])

    def _count_events(self, kind):
        import sqlite3
        db = os.path.join(os.path.dirname(admin_stats._config_path()),
                          'stats.sqlite')
        if not os.path.isfile(db):
            return 0
        conn = sqlite3.connect(db)
        try:
            return conn.execute('SELECT COUNT(*) FROM events WHERE kind=?',
                                (kind,)).fetchone()[0]
        finally:
            conn.close()


    def test_shutdown_marker_detects_unclean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, '.clean_shutdown')
            # first boot: no marker → clean
            self.assertFalse(admin_stats.consume_shutdown_marker(marker))
            self.assertTrue(os.path.isfile(marker))
            # clean shutdown removes it → next boot clean too
            admin_stats.clear_shutdown_marker(marker)
            self.assertFalse(os.path.isfile(marker))
            self.assertFalse(admin_stats.consume_shutdown_marker(marker))
            # leftover marker (kill -9 / OOM) → unclean detected
            self.assertTrue(admin_stats.consume_shutdown_marker(marker))

    def test_operator_requires_session(self):
        status, _, _ = self._get('/api/admin/operator')
        self.assertEqual(status, 401)
        status, _ = self._post('/api/admin/operator', {'excluded': True})
        self.assertEqual(status, 401)

    def test_operator_cookie_roundtrip(self):
        import urllib.request as u
        auth = self._authed()
        # status shape (session itself already excludes this browser)
        status, _, body = self._get('/api/admin/operator', auth)
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ('excluded', 'via_session', 'via_cookie'):
            self.assertIn(key, data)
        self.assertTrue(data['via_session'])
        # set the cookie
        req = u.Request(self.base + '/api/admin/operator', method='POST',
                        data=json.dumps({'excluded': True}).encode(),
                        headers=dict({'Content-Type': 'application/json'},
                                     **auth))
        with u.urlopen(req, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            set_cookie = resp.headers.get('Set-Cookie', '')
            payload = json.loads(resp.read())
        self.assertIn('operator=1', set_cookie)
        self.assertTrue(payload['via_cookie'])
        # the cookie alone (no session) also excludes
        status, _, body = self._get(
            '/api/admin/operator', {'Cookie': 'operator=1'})
        self.assertEqual(status, 401)  # still needs session to READ status
        # clear the cookie
        req = u.Request(self.base + '/api/admin/operator', method='POST',
                        data=json.dumps({'excluded': False}).encode(),
                        headers=dict({'Content-Type': 'application/json'},
                                     **auth))
        with u.urlopen(req, timeout=15) as resp:
            self.assertIn('Max-Age=0', resp.headers.get('Set-Cookie', ''))

    def test_operator_traffic_marked_not_skipped(self):
        # Ruch operatora jest ZAPISYWANY z op=1 (a nie pomijany) — panel
        # go domyślnie ukrywa, przełącznik pokazuje.
        op = {'Cookie': 'operator=1'}
        status, _, _ = self._get(
            '/api/find-route?from=%s&to=%s' % (self.from_id, self.from_id),
            op)
        self.assertEqual(status, 200)
        rows = [r for r in admin_stats.recent_searches(
            limit=5, include_op=True) if r.get('op')]
        self.assertTrue(rows, 'operator row missing with include_op=True')
        rows_hidden = admin_stats.recent_searches(limit=500)
        self.assertFalse(any(r.get('op') for r in rows_hidden),
                         'operator row leaked with default filter')

    def test_session_traffic_marked(self):
        # Sesja panelu też oznacza ruch jako operatorski (bez ciasteczka).
        auth = self._authed()
        status, _, _ = self._get(
            '/api/find-route?from=%s&to=%s' % (self.from_id, self.from_id),
            auth)
        self.assertEqual(status, 200)
        op_rows = [r for r in admin_stats.recent_searches(
            limit=5, include_op=True) if r.get('op')]
        self.assertTrue(op_rows, 'session row not marked op=1')

    def test_daily_series_hides_operator(self):
        admin_stats.record_visit('test-op-hide-ip', op=1)
        admin_stats.record_visit('test-op-hide-ip2')
        op_hash = admin_stats._ip_hash('test-op-hide-ip')
        plain_hash = admin_stats._ip_hash('test-op-hide-ip2')
        try:
            days, uniques, meta = admin_stats.daily_series(days=1)
            self.assertNotIn(op_hash, uniques)
            self.assertIn(plain_hash, uniques)
            self.assertGreaterEqual(meta['op_hidden']['visits'], 1)
            _, uniques2, _ = admin_stats.daily_series(
                days=1, include_op=True)
            self.assertIn(op_hash, uniques2)
        finally:
            with admin_stats._lock:
                admin_stats._conn.execute(
                    "DELETE FROM events WHERE iph IN (?, ?)",
                    (op_hash, plain_hash))
                admin_stats._conn.commit()

    def test_daily_series_reveals_operator_on_rolled_day(self):
        """Rollup never stores op rows, so include_op=True must back-fill
        them from retained raw events — otherwise the panel's reveal
        toggle reports hidden rows but shows nothing for rolled days."""
        import datetime
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(os.path.join(tmp, 'stats.sqlite'))
            conn.execute(
                'CREATE TABLE events (ts REAL, kind TEXT, outcome TEXT, '
                'iph TEXT, extra TEXT, op INTEGER NOT NULL DEFAULT 0)')
            conn.execute(
                'CREATE TABLE daily_rollup (day TEXT PRIMARY KEY, '
                'requests INTEGER, ok INTEGER, timeout INTEGER, '
                'busy INTEGER, visitors INTEGER)')
            day = '2026-01-02'
            start = int(datetime.datetime(
                2026, 1, 2, tzinfo=admin_stats.WARSAW).timestamp())
            end = start + 86399
            # A normal rolled-up day: 5 requests, 2 unique visitors.
            conn.execute('INSERT INTO daily_rollup VALUES (?, 5, 4, 1, 0, 2)',
                         (day,))
            op_hash = admin_stats._ip_hash('rolled-op-ip')
            conn.execute(
                'INSERT INTO events(ts, kind, outcome, iph, extra, op) '
                'VALUES (?, ?, ?, ?, ?, 1)',
                (start + 3600, 'request', 'ok', op_hash, '{}'))
            conn.commit()
            old = admin_stats._conn
            admin_stats._conn = conn
            try:
                days, uniques, meta = admin_stats.daily_series(
                    from_ts=start, to_ts=end)
                self.assertEqual(days[day]['requests'], 5)
                self.assertEqual(days[day]['visitors'], 2)
                self.assertNotIn(op_hash, uniques)
                self.assertEqual(meta['op_hidden']['requests'], 1)

                days2, uniques2, meta2 = admin_stats.daily_series(
                    from_ts=start, to_ts=end, include_op=True)
                self.assertEqual(days2[day]['requests'], 6)
                self.assertEqual(days2[day]['ok'], 5)
                self.assertEqual(days2[day]['visitors'], 3)
                self.assertIn(op_hash, uniques2)
                # The counter describes the range in both views.
                self.assertEqual(meta2['op_hidden']['requests'], 1)
            finally:
                admin_stats._conn = old
                conn.close()

    def test_recent_searches_roundtrip(self):
        """record_request(detail) → recent_searches() keeps from/to/ms."""
        detail = {'from': 'group_1', 'to': 'group_2', 'ms': 123,
                  'modes': {'cheap': {'dist': 3.5, 'reg': 4.0, 'red': 2.0},
                            'convenient': None}}
        admin_stats.record_request('ok', 'test-search-ip', detail)
        try:
            rows = admin_stats.recent_searches(limit=5)
            mine = [r for r in rows
                    if r['detail'].get('ms') == 123
                    and r['detail'].get('from') == 'group_1']
            self.assertTrue(mine, 'recorded search not found')
            self.assertEqual(mine[0]['outcome'], 'ok')
        finally:
            with admin_stats._lock:
                admin_stats._conn.execute(
                    "DELETE FROM events WHERE iph=?",
                    (admin_stats._ip_hash('test-search-ip'),))
                admin_stats._conn.commit()

    def test_stats_includes_searches_with_names(self):
        # Para ten sam przystanek: natychmiastowy wynik bez ciężkiego
        # liczenia (chodzi o łańcuch zapis → odczyt → nazwy, nie o trasy).
        status, _, body = self._get('/api/find-route?from=%s&to=%s'
                                    % (self.from_id, self.from_id))
        self.assertEqual(status, 200)
        status, _, body = self._get('/api/admin/stats', self._authed())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn('searches', data)
        self.assertIn('op_included', data)
        self.assertFalse(data['op_included'])
        self.assertIn('op_hidden', data)
        mine = [r for r in data['searches']
                if (r.get('detail') or {}).get('from') == self.from_id]
        self.assertTrue(mine, 'fresh search missing from stats')
        self.assertIn('ms', mine[0]['detail'])
        self.assertTrue(mine[0]['detail'].get('from_name'))
        # Per-mode summaries carry fare + ride count for the panel's
        # cheap-vs-convenient invariant checks.
        modes = mine[0]['detail'].get('modes') or {}
        self.assertIn('cheap', modes)
        self.assertIn('rides', modes['cheap'] or {})


if __name__ == '__main__':
    unittest.main()
