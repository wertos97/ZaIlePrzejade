"""Persistent admin statistics for the VPS panel.

Lives outside git by design: the DATABASE and the password file live in
processed/ (VPS-only, survives autoupdate's git clean). The code here is
the generic mechanism — without processed/admin_config.json the whole
feature is invisible (the /panel route answers 404).

Events recorded (append-only sqlite, tiny writes):
  request  — one /api/find-route call (outcome: ok|timeout|busy)
  visit    — one page load of / (outcome = ip hash only)
  restart  — server process start (outcome = app version)

IP addresses are never stored raw — only sha256(salt + ip) with a
per-installation random salt, so unique-visitor counts work but the
original addresses cannot be recovered from the database.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo('Europe/Warsaw')

_DB_PATH = None
_conn = None
_lock = threading.Lock()

# Sessions (in-memory; a restart logs everyone out — acceptable here)
SESSION_TTL = 7 * 24 * 3600
_sessions = {}  # token -> expiry (time.time())

# Failed login attempts per IP: [window_start, count] (simple lockout)
_login_failures = {}
_LOGIN_MAX = 10
_LOGIN_WINDOW = 600


# ------------------------------------------------------------
# Setup / password
# ------------------------------------------------------------

def _config_path():
    return os.path.join(os.path.dirname(_DB_PATH), 'admin_config.json')


def enabled():
    """Admin panel exists only when a password has been configured on this
    machine (the file lives in processed/ — VPS-only, never in git)."""
    try:
        return os.path.isfile(_config_path())
    except Exception:
        return False


def verify_password(password: str) -> bool:
    try:
        with open(_config_path(), encoding='utf-8') as f:
            cfg = json.load(f)
        calc = hashlib.pbkdf2_hmac(
            'sha256', password.encode(), bytes.fromhex(cfg['salt']),
            int(cfg.get('iterations', 200_000)))
        return hmac.compare_digest(calc.hex(), cfg['hash'])
    except Exception:
        return False


def set_password(password: str):
    """One-time setup (run on the VPS): store salted PBKDF2 hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200_000)
    with open(_config_path(), 'w', encoding='utf-8') as f:
        json.dump({'salt': salt.hex(), 'hash': digest.hex()}, f)
    os.chmod(_config_path(), 0o600)


# ------------------------------------------------------------
# Sessions
# ------------------------------------------------------------

def create_session():
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    now = time.time()
    for t in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(t, None)
    return token


def session_ok(token):
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def drop_session(token):
    _sessions.pop(token, None)


# ------------------------------------------------------------
# Setup / events
# ------------------------------------------------------------

def init(db_dir):
    """Create/open the stats database. Safe to call on every boot; the
    whole module degrades to no-op on any sqlite error (stats are
    best-effort, never block serving)."""
    global _DB_PATH, _conn
    try:
        os.makedirs(db_dir, exist_ok=True)
        _DB_PATH = os.path.join(db_dir, 'stats.sqlite')
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('CREATE TABLE IF NOT EXISTS events ('
                     'ts REAL NOT NULL, kind TEXT NOT NULL, '
                     'outcome TEXT, iph TEXT, extra TEXT)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_events_ts '
                     'ON events(ts)')
        conn.execute('CREATE TABLE IF NOT EXISTS meta '
                     "(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        row = conn.execute("SELECT v FROM meta WHERE k='salt'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(k, v) VALUES ('salt', ?)",
                         (secrets.token_hex(16),))
        conn.commit()
        _conn = conn
    except Exception:
        _conn = None


def _ip_hash(ip):
    try:
        row = _conn.execute("SELECT v FROM meta WHERE k='salt'").fetchone()
        salt_b = bytes.fromhex(row[0]) if row else b''
        return hashlib.sha256(salt_b + ip.encode()).hexdigest()[:16]
    except Exception:
        return ''


def record_event(kind, outcome=None, ip=None, detail=None):
    """Append one event row (best-effort, never raises)."""
    if _conn is None:
        return
    try:
        iph = _ip_hash(ip) if ip else None
        with _lock:
            _conn.execute(
                'INSERT INTO events(ts, kind, outcome, iph, extra) '
                'VALUES (?, ?, ?, ?, ?)',
                (time.time(), kind, outcome, iph, detail))
            _conn.commit()
    except Exception:
        pass


def record_request(outcome, ip):
    record_event('request', outcome=outcome, ip=ip)


def record_visit(ip):
    record_event('visit', ip=ip)


def record_restart(version):
    record_event('restart', outcome=version)


# ------------------------------------------------------------
# Queries (for the panel)
# ------------------------------------------------------------

def _day_key(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime('%Y-%m-%d')


def daily_series(days=None, tz=ZoneInfo('Europe/Warsaw'),
                 from_ts=None, to_ts=None):
    """Per-day buckets (Europe/Warsaw): requests (ok/timeout/busy) and
    unique visitors (distinct IP hashes). Zakres: from_ts/to_ts (epoch);
    bez nich — ostatnie `days` dni (domyślnie 30). Dane nie są nigdy
    usuwane, więc zakres może sięgać dowolnie głęboko w historię."""
    out = {}
    unique_total = set()
    if _conn is None:
        return out, unique_total
    if from_ts is None or to_ts is None:
        days = days or 30
        to_ts = time.time()
        from_ts = to_ts - days * 86400
    try:
        with _lock:
            rows = _conn.execute(
                'SELECT ts, kind, outcome, iph FROM events '
                'WHERE ts >= ? AND ts <= ?', (from_ts, to_ts)).fetchall()
    except Exception:
        return out, unique_total
    for ts, kind, outcome, iph in rows:
        day = _day_key(ts, tz)
        d = out.setdefault(day, {'requests': 0, 'ok': 0, 'timeout': 0,
                                 'busy': 0, 'visitors': set()})
        if kind == 'request':
            d['requests'] += 1
            if outcome in ('timeout', 'busy', 'ok'):
                d[outcome] += 1
        if iph:
            d['visitors'].add(iph)
            unique_total.add(iph)
    for d in out.values():
        d['visitors'] = len(d['visitors'])
    return dict(sorted(out.items())), unique_total


def parse_warsaw_range(from_str, to_str):
    """'YYYY-MM-DD' × 2 → (from_epoch, to_epoch) granice dnia w
    Europe/Warsaw (to = koniec dnia). Rzuca ValueError przy złym formacie."""
    if not from_str or not to_str:
        raise ValueError('missing range')
    f = datetime.strptime(from_str, '%Y-%m-%d').replace(tzinfo=WARSAW)
    t = datetime.strptime(to_str, '%Y-%m-%d').replace(tzinfo=WARSAW)
    if f > t:
        f, t = t, f
    t = t + timedelta(days=1) - timedelta(seconds=1)
    return f.timestamp(), t.timestamp()


def restarts(from_ts=None, to_ts=None, limit=1000):
    """Server restarts from the events table (zakres opcjonalny)."""
    if _conn is None:
        return []
    try:
        q = "SELECT ts, outcome FROM events WHERE kind='restart'"
        args = []
        if from_ts is not None:
            q += ' AND ts >= ?'
            args.append(from_ts)
        if to_ts is not None:
            q += ' AND ts <= ?'
            args.append(to_ts)
        q += ' ORDER BY ts DESC'
        with _lock:
            rows = _conn.execute(q, args).fetchall()
        return [{'ts': ts, 'version': v or ''} for ts, v in rows]
    except Exception:
        return []


def read_updates(from_str=None, to_str=None, limit=2000):
    """Successful auto-updates parsed from autoupdate.log (VPS-only file;
    missing on dev machines — returns []). Filtr opcjonalny po dacie
    'YYYY-MM-DD' (prefiks znacznika czasu w logu)."""
    try:
        log_path = os.path.join(os.path.dirname(os.path.dirname(_DB_PATH)),
                                'autoupdate.log')
        events = []
        with open(log_path, encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '📌 Commit:' not in line:
                    continue
                ts_str = line[1:20] if line.startswith('[') else ''
                day = ts_str[:10]
                if from_str and day < from_str:
                    continue
                if to_str and day > to_str:
                    continue
                what = line.split('Commit:', 1)[-1].strip()
                events.append({'ts': ts_str, 'what': what})
        return events[-limit:][::-1]
    except Exception:
        return []
