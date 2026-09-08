"""GTFS data update machinery (admin-approved).

Two jobs, both driven from the admin panel and reported through a small
state file (processed/gtfs_update.json):

  check   — lightweight upstream freshness check (runs automatically twice
            a day at 09:00/17:00 Europe/Warsaw when the admin panel is
            enabled; result cached in state.last_check).
  update  — full regenerate (download --force + process), then automatic
            restart so the server boots on the new data. Runs only after
            explicit admin confirmation.

Raw GTFS data never enters git: zips live in data/ (ignored), processed
JSONs are generated on the instance (see Phase A migration in
PROJECT_INFO.md). Rollback uses a local backup dir, not git.

This module holds pure helpers (testable without network/subprocess)
plus the job orchestration with small seams (_download, _run_process)
that tests monkeypatch. Threading/HTTP wiring lives in handler.py.
"""

import csv
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, time as dtime

from .admin_stats import WARSAW

try:
    from process_gtfs import FEEDS as _GTFS_FEEDS
except Exception:  # dev without process_gtfs on path — degrade gracefully
    _GTFS_FEEDS = []

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
DATA_DIR = os.path.join(BASE_DIR, 'data')

STATE_FILENAME = 'gtfs_update.json'
BACKUP_PREFIX = '.backup-'

# Files produced by process_gtfs.py (the update set — committed nowhere,
# rolled back from the backup dir, never via git).
TRACKED_OUTPUTS = (
    'stops.json', 'routes.json', 'adjacency.json', 'shapes.json',
    'metadata.json',
)

# Schedule: freshness checks at these local (Europe/Warsaw) hours.
CHECK_HOURS = (9, 17)

# Guardrails.
DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 20 * 60
MIN_FREE_BYTES = 500 * 1024 * 1024
GTFS_ROOT_URL = 'https://gtfs.ztp.krakow.pl/'

# process_gtfs.py prints "N. <step>" lines for steps 0..7.
_PROGRESS_RE = re.compile(r'^(\d)\.\s+(.*)$')
_PROGRESS_PHASES_PL = {
    '0': 'Pobieranie rozkładów',
    '1': 'Przystanki',
    '2': 'Linie',
    '3': 'Kursy i połączenia',
    '4': 'Przesiadki',
    '5': 'Graf połączeń',
    '6': 'Kształty tras',
    '7': 'Zapisywanie',
}
_PROGRESS_STEPS = 8

_lock = threading.Lock()


# ------------------------------------------------------------
# State file
# ------------------------------------------------------------

def _state_path():
    return os.path.join(PROCESSED_DIR, STATE_FILENAME)


def default_state():
    return {
        'job': {
            'state': 'idle',
            'progress': 0,
            'phase': '',
            'detail': '',
            'pid': None,
        },
        'last_check': None,
        'last_done': None,
    }


def read_state():
    """Read the update state file (best-effort, never raises)."""
    try:
        with open(_state_path(), encoding='utf-8') as f:
            state = json.load(f)
        merged = default_state()
        merged.update(state if isinstance(state, dict) else {})
        return merged
    except Exception:
        return default_state()


def write_state(state):
    """Persist the update state file (best-effort, never raises)."""
    try:
        os.makedirs(PROCESSED_DIR, exist_ok=True)
        tmp = _state_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
        return True
    except Exception:
        return False


def set_job(state_name, progress=0, phase='', detail=''):
    """Replace the job slot of the state file; returns the state dict."""
    state = read_state()
    state['job'] = {
        'state': state_name,
        'progress': progress,
        'phase': phase,
        'detail': detail,
        'pid': os.getpid(),
    }
    write_state(state)
    return state


# ------------------------------------------------------------
# Pure helpers (no network, no subprocess — unit tested)
# ------------------------------------------------------------

def next_check_run(now=None):
    """Next scheduled check as an aware datetime (Europe/Warsaw).

    Checks run at CHECK_HOURS daily; returns the nearest upcoming slot
    strictly after `now` (defaults to now).
    """
    now = now or datetime.now(WARSAW)
    if now.tzinfo is None:
        now = now.replace(tzinfo=WARSAW)
    for hour in sorted(CHECK_HOURS):
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    first = sorted(CHECK_HOURS)[0]
    nxt = now.replace(hour=first, minute=0, second=0, microsecond=0)
    return nxt + _one_day()


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def parse_progress(line):
    """Map a process_gtfs.py stdout line to (percent, phase_pl).

    Returns None for unrecognized lines (log noise passes through).
    """
    m = _PROGRESS_RE.match((line or '').strip())
    if not m:
        return None
    step = int(m.group(1))
    if step >= _PROGRESS_STEPS:
        return None
    percent = round(step / (_PROGRESS_STEPS - 1) * 100)
    phase = _PROGRESS_PHASES_PL.get(m.group(1), m.group(2).strip())
    return percent, phase


def versions_differ(old_versions, new_versions):
    """True when any feed version changed between two per-zip mappings.

    old_versions/new_versions: {zip_filename: feed_version}. Feeds use
    heterogeneous schemes (dates vs counters), so comparison is strictly
    per zip — the joined metadata.json version string is display-only
    and never compared. Missing/empty data never triggers; unknown
    history triggers once (the check then pins versions in state, and
    the update flow re-verifies against real pre-update zips, so a
    same-data regen ends as a restart-free no-op).
    """
    if not new_versions or any(not v for v in new_versions.values()):
        return False
    if not old_versions:
        return True
    common = set(old_versions) & set(new_versions)
    if not common:
        return True
    return any(old_versions[z] != new_versions[z] for z in common)


def diff_route_keys(current_routes, upstream_routes):
    """Diff route (short_name, mode) sets.

    current_routes: iterable of {'short_name', 'mode'} (processed/routes.json).
    upstream_routes: iterable of (short_name, mode) from fresh zips.
    Returns (added, removed) as sorted lists of [short_name, mode].
    """
    cur = set(
        (str(r.get('short_name')), r.get('mode')) for r in current_routes)
    new = set((str(s), m) for s, m in upstream_routes)
    added = sorted(new - cur)
    removed = sorted(cur - new)
    return [[s, m] for s, m in added], [[s, m] for s, m in removed]


# ------------------------------------------------------------
# Network seams (monkeypatched in tests)
# ------------------------------------------------------------

def _http_get(url, timeout=30, max_bytes=DOWNLOAD_MAX_BYTES):
    """GET url, return response bytes (capped). Raises on error."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'ZaIlePrzejade-gtfs-check/1.0 (https://zaileprzeja.de)',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f'HTTP {resp.status} for {url}')
        return resp.read(max_bytes + 1)


def _download(url, dest_path, timeout=DOWNLOAD_TIMEOUT_SECONDS,
              max_bytes=DOWNLOAD_MAX_BYTES):
    """Download url to dest_path (atomic via tmp+rename). Raises on error."""
    body = _http_get(url, timeout=timeout, max_bytes=max_bytes)
    if len(body) > max_bytes:
        raise OSError(f'{url} exceeds size cap ({len(body)} bytes)')
    tmp = dest_path + '.part'
    with open(tmp, 'wb') as f:
        f.write(body)
    os.replace(tmp, dest_path)
    return dest_path


def _run_process(script_args, timeout=PROCESS_TIMEOUT_SECONDS):
    """Run a subprocess, yielding stdout lines as they arrive.

    The child must run unbuffered (argv includes -u) or step lines sit
    in its pipe buffer until exit. Overall deadline enforced even when
    the child goes silent. Raises OSError on failure/timeout.
    """
    import subprocess
    import select as _select
    import time as _time
    proc = subprocess.Popen(
        script_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1)
    deadline = _time.monotonic() + timeout
    tail = []
    try:
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise OSError('GTFS processing timed out')
            ready, _, _ = _select.select([proc.stdout], [], [],
                                         min(30, remaining))
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            tail.append(line)
            if len(tail) > 20:
                tail.pop(0)
            yield line
        rc = proc.wait(timeout=60)
    except Exception:
        proc.kill()
        raise OSError('GTFS processing timed out or failed')
    if rc != 0:
        raise OSError(
            'GTFS processing failed (exit %d): %s' % (rc, ''.join(tail)))


# ------------------------------------------------------------
# Upstream inspection (used by check job)
# ------------------------------------------------------------

_ROOT_DATE_RE = re.compile(
    r'aktualizacja:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\[(GTFS_KRK_[ATM]\.zip)\]')


def parse_root_page_dates(html):
    """Parse per-zip update timestamps from the gtfs.ztp.krakow.pl index.

    Returns {zip_filename: 'YYYY-MM-DD HH:MM:SS'} (may be empty).
    """
    out = {}
    for date_str, zip_name in _ROOT_DATE_RE.findall(html or ''):
        out[zip_name] = date_str
    return out


def read_feed_infos(extract_dirs):
    """Read feed_info.txt from extracted feed dirs.

    extract_dirs: {zip_filename: dir_path}. Returns
    {zip_filename: {'version', 'start_date', 'end_date'}} (best-effort;
    missing feeds are skipped).
    """
    infos = {}
    for zip_name, feed_dir in extract_dirs.items():
        info_path = os.path.join(feed_dir, 'feed_info.txt')
        try:
            with open(info_path, encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    infos[zip_name] = {
                        'version': (row.get('feed_version') or '').strip(),
                        'start_date': (row.get('feed_start_date') or '').strip(),
                        'end_date': (row.get('feed_end_date') or '').strip(),
                    }
        except OSError:
            continue
    return infos


def collect_upstream_routes(extract_dirs):
    """Collect (short_name, mode) pairs from extracted feed dirs.

    Mode mapping mirrors process_gtfs FEEDS (tram feed → tram, else bus).
    Returns a list of (short_name, mode).
    """
    mode_by_zip = {}
    for entry in _GTFS_FEEDS:
        try:
            _dir, _name, mode, zip_name, _url = entry
            mode_by_zip[zip_name] = mode
        except (TypeError, ValueError):
            continue
    out = []
    for zip_name, feed_dir in extract_dirs.items():
        routes_path = os.path.join(feed_dir, 'routes.txt')
        try:
            with open(routes_path, encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    out.append(((row.get('route_short_name') or '').strip(),
                                mode_by_zip.get(zip_name, 'bus')))
        except OSError:
            continue
    return out


# ------------------------------------------------------------
# Migration guard (Phase A → Phase 2)
# ------------------------------------------------------------

def outputs_tracked_in_git():
    """True while processed outputs are still git-tracked (pre-migration).

    Regenerating then would be silently reverted by autoupdate's
    `git reset --hard`. Best-effort: any error means "not tracked".
    """
    import subprocess
    try:
        out = subprocess.run(
            ['git', '-C', BASE_DIR, 'ls-files', 'processed/stops.json'],
            capture_output=True, text=True, timeout=15)
        return bool(out.stdout.strip())
    except Exception:
        return False


# ------------------------------------------------------------
# Job orchestration (run in background threads from handler.py)
# ------------------------------------------------------------

LOCK_PATH = '/tmp/mpk_autoupdate.lock'  # same flock autoupdate.sh uses


def _acquire_flock():
    """Hold autoupdate's lock for the whole job (or None if busy)."""
    import fcntl
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _release_flock(fd):
    try:
        if fd is not None:
            os.close(fd)  # closing releases the flock
    except OSError:
        pass


def _zip_name_to_dir(tmpdir, zip_name):
    return os.path.join(tmpdir, zip_name[:-4])


def _extract_needed(zip_path, dest_dir):
    """Extract only feed_info.txt + routes.txt (enough for check)."""
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in ('feed_info.txt', 'routes.txt'):
                target = os.path.join(dest_dir, base)
                os.makedirs(dest_dir, exist_ok=True)
                with z.open(name) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)


def read_feed_infos_from_zips(zip_paths):
    """{zip_filename: {version, start_date, end_date}} from zip files."""
    infos = {}
    for zip_path in zip_paths:
        try:
            with zipfile.ZipFile(zip_path) as z:
                feed_files = [n for n in z.namelist()
                              if os.path.basename(n) == 'feed_info.txt']
                if not feed_files:
                    continue
                with z.open(feed_files[0]) as f:
                    text = f.read().decode('utf-8', errors='replace')
                for row in csv.DictReader(text.splitlines()):
                    infos[os.path.basename(zip_path)] = {
                        'version': (row.get('feed_version') or '').strip(),
                        'start_date': (row.get('feed_start_date') or '').strip(),
                        'end_date': (row.get('feed_end_date') or '').strip(),
                    }
        except Exception:
            continue
    return infos


def collect_routes_from_zips(zip_paths):
    """[(short_name, mode)] from zip files (mode mirrors FEEDS)."""
    mode_by_zip = {}
    for entry in _GTFS_FEEDS:
        try:
            _dir, _name, mode, zip_name, _url = entry
            mode_by_zip[zip_name] = mode
        except (TypeError, ValueError):
            continue
    out = []
    for zip_path in zip_paths:
        try:
            with zipfile.ZipFile(zip_path) as z:
                route_files = [n for n in z.namelist()
                               if os.path.basename(n) == 'routes.txt']
                if not route_files:
                    continue
                with z.open(route_files[0]) as f:
                    text = f.read().decode('utf-8', errors='replace')
                for row in csv.DictReader(text.splitlines()):
                    out.append(((row.get('route_short_name') or '').strip(),
                                mode_by_zip.get(os.path.basename(zip_path),
                                                'bus')))
        except Exception:
            continue
    return out


def run_check_job():
    """Freshness check: root page → download-if-changed → diff.

    Stores state.last_check {at, stamps, upstream, newer, added,
    removed} (or {at, error}). Never raises.
    """
    import tempfile
    set_job('checking', 5, 'Sprawdzanie wersji')
    try:
        state = read_state()
        prev = state.get('last_check') or {}
        root_html = _http_get(GTFS_ROOT_URL, timeout=30,
                              max_bytes=256 * 1024).decode(
                                  'utf-8', errors='replace')
        stamps = parse_root_page_dates(root_html)
        if stamps and stamps == prev.get('stamps') and prev.get('upstream'):
            prev['at'] = time.time()
            state['last_check'] = prev
            set_job('idle')
            write_state(state)
            return state['last_check']
        tmpdir = tempfile.mkdtemp(prefix='gtfs_check_')
        try:
            zip_paths = []
            for _dir, _name, _mode, zip_name, url in _GTFS_FEEDS:
                dest = os.path.join(tmpdir, zip_name)
                _download(url, dest)
                zip_paths.append(dest)
            upstream = read_feed_infos_from_zips(zip_paths)
            new_routes = collect_routes_from_zips(zip_paths)
            cur_routes = []
            try:
                with open(os.path.join(PROCESSED_DIR, 'routes.json'),
                          encoding='utf-8') as f:
                    cur_routes = json.load(f)
            except Exception:
                pass
            added, removed = diff_route_keys(cur_routes, new_routes)
            old_versions = (prev.get('upstream') or {})
            old_versions = {z: i.get('version', '')
                            for z, i in old_versions.items()
                            if isinstance(i, dict)}
            new_versions = {z: i.get('version', '')
                            for z, i in upstream.items()}
            check = {
                'at': time.time(),
                'stamps': stamps,
                'upstream': upstream,
                'newer': versions_differ(old_versions, new_versions),
                'added': added,
                'removed': removed,
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        state = read_state()
        state['last_check'] = check
        write_state(state)
        set_job('idle')
        return check
    except Exception as e:
        state = read_state()
        state['last_check'] = {'at': time.time(), 'error': str(e)[:300]}
        write_state(state)
        set_job('idle')
        return state['last_check']


def _backup_outputs():
    """Copy the 5 processed JSONs aside; returns backup dir path."""
    stamp = datetime.now(WARSAW).strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(PROCESSED_DIR, BACKUP_PREFIX + stamp)
    # One backup at a time — previous leftovers are stale by definition.
    for entry in os.listdir(PROCESSED_DIR):
        if entry.startswith(BACKUP_PREFIX):
            shutil.rmtree(os.path.join(PROCESSED_DIR, entry),
                          ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for name in TRACKED_OUTPUTS:
        src = os.path.join(PROCESSED_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
    return dest


def _restore_backup(backup_dir):
    for name in TRACKED_OUTPUTS:
        src = os.path.join(backup_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(PROCESSED_DIR, name))


def _remove_backups():
    for entry in os.listdir(PROCESSED_DIR):
        if entry.startswith(BACKUP_PREFIX):
            shutil.rmtree(os.path.join(PROCESSED_DIR, entry),
                          ignore_errors=True)


def _append_restart_reason(new_version):
    """Write an autoupdate.log line so the boot picks the restart reason.

    Format must match admin_stats.read_last_reason: '[%Y-%m-%d %H:%M:%S]
    📌 Powód: ...' within 600 s before boot.
    """
    line = '[%s] 📌 Powód: aktualizacja danych GTFS do %s (panel admina).\n' % (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'), new_version)
    try:
        with open(os.path.join(BASE_DIR, 'autoupdate.log'), 'a',
                  encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def _maintenance_on():
    """Maintenance mode is derived from state (job.state == updating)."""
    return read_state().get('job', {}).get('state') == 'updating'


def run_update_job():
    """Full regenerate (must be confirm-gated by the caller).

    Backup → process --force → verify → restart reason → detached
    restart.sh. Rollback from backup on any failure. Never raises
    (all outcomes land in the state file).
    """
    import subprocess
    import sys as _sys
    set_job('updating', 2, 'Przygotowanie', 'sprawdzanie blokady')
    lock_fd = _acquire_flock()
    if lock_fd is None:
        set_job('error', 0, '',
                'Serwer jest zajęty (autoupdate w toku). Spróbuj za chwilę.')
        return read_state()
    backup_dir = None
    try:
        state = read_state()
        if not (state.get('last_check') or {}).get('newer'):
            set_job('error', 0, '',
                    'Brak potwierdzonej nowszej wersji (najpierw sprawdzenie).')
            return read_state()
        if shutil.disk_usage(PROCESSED_DIR).free < MIN_FREE_BYTES:
            set_job('error', 0, '', 'Za mało miejsca na dysku na aktualizację.')
            return read_state()
        set_job('updating', 5, 'Przygotowanie', 'kopia zapasowa danych')
        backup_dir = _backup_outputs()
        script = os.path.join(BASE_DIR, 'process_gtfs.py')
        set_job('updating', 8, 'Pobieranie rozkładów', '')
        argv = [_sys.executable, '-u', script, '--force']
        for line in _run_process(argv):
            parsed = parse_progress(line)
            if parsed:
                pct, phase = parsed
                # Download + processing share 8..95; verify/restart take rest.
                pct = 8 + round(pct * 87 / 100)
                set_job('updating', pct, phase, '')
        set_job('updating', 96, 'Weryfikacja', 'porównanie wersji')
        try:
            with open(os.path.join(PROCESSED_DIR, 'metadata.json'),
                      encoding='utf-8') as f:
                new_version = str(json.load(f).get('version') or '')
        except Exception:
            new_version = ''
        if not new_version:
            raise OSError('regeneracja nie zapisała metadata.json')
        _append_restart_reason(new_version)
        state = read_state()
        state['job'] = {'state': 'restarting', 'progress': 100,
                        'phase': 'Restart serwera', 'detail': '',
                        'pid': os.getpid(), 'new_version': new_version}
        write_state(state)
        # Detached restart: our own process gets SIGTERM (atexit flushes
        # the route cache); the fresh process boots on the new data.
        subprocess.Popen(
            ['bash', os.path.join(BASE_DIR, 'restart.sh')],
            cwd=BASE_DIR, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return read_state()
    except Exception as e:
        if backup_dir:
            try:
                _restore_backup(backup_dir)
            except Exception:
                pass
        set_job('error', 0, '', 'Aktualizacja nieudana: %s' % str(e)[:300])
        return read_state()
    finally:
        _release_flock(lock_fd)


def reconcile_after_boot(feed_metadata):
    """Boot-time reconciliation of an interrupted/finished update.

    Call once after data load with data.feed_metadata. Cleans the backup
    only when the on-disk data matches the expected new version; marks
    stale running jobs as interrupted. Returns the state dict.
    """
    state = read_state()
    job = state.get('job') or {}
    if job.get('state') in ('updating', 'checking'):
        if job.get('pid') != os.getpid():
            job.update({'state': 'error', 'detail':
                        'Aktualizacja przerwana przez restart serwera.',
                        'progress': 0, 'phase': ''})
            state['job'] = job
            write_state(state)
    elif job.get('state') == 'restarting':
        expected = job.get('new_version') or ''
        current = str((feed_metadata or {}).get('version') or '')
        if expected and current == expected:
            _remove_backups()
            state['last_done'] = {'at': time.time(), 'version': current}
            state['job'] = {'state': 'idle', 'progress': 100,
                            'phase': '', 'detail': '', 'pid': None}
        else:
            job.update({'state': 'error', 'detail':
                        'Po restarcie dane nie zgadzają się z oczekiwanymi '
                        '(kopia zapasowa zachowana).',
                        'progress': 0, 'phase': ''})
            state['job'] = job
        write_state(state)
    return state
