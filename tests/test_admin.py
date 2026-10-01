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
        status, body = self._post('/api/admin/gtfs-cancel', {},
                                  self._authed())
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
        mine = [r for r in data['searches']
                if (r.get('detail') or {}).get('from') == self.from_id]
        self.assertTrue(mine, 'fresh search missing from stats')
        self.assertIn('ms', mine[0]['detail'])
        self.assertTrue(mine[0]['detail'].get('from_name'))


if __name__ == '__main__':
    unittest.main()
