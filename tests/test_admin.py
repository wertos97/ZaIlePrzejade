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


if __name__ == '__main__':
    unittest.main()
