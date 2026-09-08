"""Unit tests for server.gtfs_update pure helpers (no network/subprocess)."""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import gtfs_update as gu
from server.admin_stats import WARSAW


class TestNextCheckRun(unittest.TestCase):
    def _at(self, h, m=0):
        return datetime(2026, 9, 8, h, m, tzinfo=WARSAW)

    def test_morning_slot(self):
        self.assertEqual(gu.next_check_run(self._at(7)).hour, 9)

    def test_between_slots(self):
        self.assertEqual(gu.next_check_run(self._at(10)).hour, 17)

    def test_after_last_slot_rolls_to_tomorrow(self):
        nxt = gu.next_check_run(self._at(18))
        self.assertEqual((nxt.hour, nxt.day), (9, 9))

    def test_exact_slot_boundary_goes_next(self):
        # strictly after `now`: 09:00 sharp means the 17:00 slot
        self.assertEqual(gu.next_check_run(self._at(9)).hour, 17)


class TestParseProgress(unittest.TestCase):
    def test_steps_map_to_percent(self):
        self.assertEqual(gu.parse_progress('0. Downloading latest GTFS data...'),
                         (0, 'Pobieranie rozkładów'))
        self.assertEqual(gu.parse_progress('7. Saving processed data...'),
                         (100, 'Zapisywanie'))

    def test_middle_step(self):
        pct, phase = gu.parse_progress('3. Processing trips and building connections...')
        self.assertEqual(pct, round(3 / 7 * 100))
        self.assertTrue(phase)

    def test_noise_ignored(self):
        self.assertIsNone(gu.parse_progress('  Saved stops.json (123 stops)'))
        self.assertIsNone(gu.parse_progress(''))
        self.assertIsNone(gu.parse_progress(None))


class TestVersionsDiffer(unittest.TestCase):
    def test_same_versions(self):
        old = {'a.zip': '20260901', 'b.zip': '1673_1674'}
        self.assertFalse(gu.versions_differ(old, dict(old)))

    def test_changed_version(self):
        old = {'a.zip': '20260901'}
        self.assertTrue(gu.versions_differ(old, {'a.zip': '20260905'}))

    def test_incomplete_upstream_never_triggers(self):
        self.assertFalse(gu.versions_differ({'a.zip': 'v'}, {}))
        self.assertFalse(gu.versions_differ(
            {'a.zip': 'v'}, {'a.zip': ''}))

    def test_unknown_history_triggers_once(self):
        self.assertTrue(gu.versions_differ(
            {}, {'a.zip': '20260905'}))


class TestDiffRouteKeys(unittest.TestCase):
    def test_added_removed(self):
        cur = [{'short_name': '17', 'mode': 'tram'},
               {'short_name': '273', 'mode': 'bus'}]
        new = [('77', 'tram'), ('273', 'bus')]
        added, removed = gu.diff_route_keys(cur, new)
        self.assertEqual(added, [['77', 'tram']])
        self.assertEqual(removed, [['17', 'tram']])

    def test_identical(self):
        cur = [{'short_name': '1', 'mode': 'tram'}]
        self.assertEqual(gu.diff_route_keys(cur, [('1', 'tram')]), ([], []))


class TestParseRootPageDates(unittest.TestCase):
    def test_parses_listings(self):
        html = ('<li>aktualizacja: 2026-09-04 11:28:34 '
                '[GTFS_KRK_A.zip](./GTFS_KRK_A.zip)</li>')
        self.assertEqual(gu.parse_root_page_dates(html),
                         {'GTFS_KRK_A.zip': '2026-09-04 11:28:34'})

    def test_empty(self):
        self.assertEqual(gu.parse_root_page_dates(''), {})
        self.assertEqual(gu.parse_root_page_dates(None), {})


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = gu.PROCESSED_DIR
        gu.PROCESSED_DIR = self.tmp.name

    def tearDown(self):
        gu.PROCESSED_DIR = self._orig
        self.tmp.cleanup()

    def test_roundtrip(self):
        self.assertEqual(gu.read_state()['job']['state'], 'idle')
        gu.set_job('updating', progress=42, phase='Linie', detail='x')
        state = gu.read_state()
        self.assertEqual(state['job']['state'], 'updating')
        self.assertEqual(state['job']['progress'], 42)
        self.assertEqual(state['job']['phase'], 'Linie')


if __name__ == '__main__':
    unittest.main()


class TestZipHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        zp = os.path.join(self.tmp.name, 'GTFS_KRK_T.zip')
        import zipfile
        with zipfile.ZipFile(zp, 'w') as z:
            z.writestr('feed_info.txt',
                       'feed_publisher_name,feed_publisher_url,feed_lang,'
                       'feed_start_date,feed_end_date,feed_version\n'
                       '"ZTP","http://x/",pl,20260905,,20260905\n')
            z.writestr('routes.txt',
                       'route_id,route_short_name,route_long_name\n'
                       'r1,77,Tramwaj\n')
        self.zp = zp

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_feed_infos_from_zips(self):
        infos = gu.read_feed_infos_from_zips([self.zp])
        self.assertEqual(infos['GTFS_KRK_T.zip']['version'], '20260905')

    def test_collect_routes_from_zips(self):
        routes = gu.collect_routes_from_zips([self.zp])
        self.assertIn(('77', 'tram'), routes)


class TestBackupAndReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = gu.PROCESSED_DIR
        gu.PROCESSED_DIR = self.tmp.name
        for name in gu.TRACKED_OUTPUTS:
            with open(os.path.join(self.tmp.name, name), 'w') as f:
                f.write('{"v": 1}')

    def tearDown(self):
        gu.PROCESSED_DIR = self._orig
        self.tmp.cleanup()

    def test_backup_restore(self):
        backup = gu._backup_outputs()
        with open(os.path.join(self.tmp.name, 'stops.json'), 'w') as f:
            f.write('{"v": 2}')
        gu._restore_backup(backup)
        with open(os.path.join(self.tmp.name, 'stops.json')) as f:
            self.assertEqual(f.read(), '{"v": 1}')

    def test_reconcile_restarting_match(self):
        gu.set_job('restarting')
        state = gu.read_state()
        state['job']['new_version'] = 'NEWV'
        gu.write_state(state)
        backup = gu._backup_outputs()
        out = gu.reconcile_after_boot({'version': 'NEWV'})
        self.assertEqual(out['job']['state'], 'idle')
        self.assertEqual(out['last_done']['version'], 'NEWV')
        self.assertFalse(os.path.isdir(backup))

    def test_reconcile_restarting_mismatch_keeps_backup(self):
        gu.set_job('restarting')
        state = gu.read_state()
        state['job']['new_version'] = 'NEWV'
        gu.write_state(state)
        backup = gu._backup_outputs()
        out = gu.reconcile_after_boot({'version': 'OLDV'})
        self.assertEqual(out['job']['state'], 'error')
        self.assertTrue(os.path.isdir(backup))

    def test_reconcile_stale_running_job(self):
        gu.set_job('updating', progress=50, phase='Linie')
        state = gu.read_state()
        state['job']['pid'] = -999  # someone else's (dead) job
        gu.write_state(state)
        out = gu.reconcile_after_boot({'version': 'X'})
        self.assertEqual(out['job']['state'], 'error')
