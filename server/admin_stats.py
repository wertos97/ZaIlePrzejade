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

Two-tier history (see prune_old_events / rollup_complete_days):
  raw events (with IP hashes) older than STATS_RETENTION_DAYS (default
  90) are automatically deleted — but BEFORE deletion each complete day
  is aggregated into daily_rollup (per-day requests/visitors, no
  identifiers), which is kept forever. The admin panel therefore shows
  searches/day and unique visitors/day for the whole recording period,
  while per-visitor identifiers always expire.
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

# Retention: raw request/visit events (the ones carrying IP hashes)
# older than this are deleted (see prune_old_events). Overridden by
# init(); mirrors config.STATS_RETENTION_DAYS. Per-day aggregates in
# daily_rollup are kept forever (no identifiers, panel history intact).
_retention_days = 90
_last_prune = 0.0

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

def init(db_dir, retention_days=90):
    """Create/open the stats database. Safe to call on every boot; the
    whole module degrades to no-op on any sqlite error (stats are
    best-effort, never block serving). Old raw events beyond
    `retention_days` are aggregated into daily_rollup and deleted right
    away (privacy: identifiers expire, per-day history stays forever)."""
    global _DB_PATH, _conn, _retention_days
    _retention_days = retention_days
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
        conn.execute('CREATE TABLE IF NOT EXISTS daily_rollup ('
                     'day TEXT PRIMARY KEY, '
                     'requests INTEGER NOT NULL DEFAULT 0, '
                     'ok INTEGER NOT NULL DEFAULT 0, '
                     'timeout INTEGER NOT NULL DEFAULT 0, '
                     'busy INTEGER NOT NULL DEFAULT 0, '
                     'visitors INTEGER NOT NULL DEFAULT 0)')
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
    prune_old_events()


def _today_start(tz=WARSAW):
    """Epoch of today's midnight in `tz` (days before it are complete)."""
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def rollup_complete_days():
    """Aggregate complete (past) days from raw events into daily_rollup.

    Only days with raw events present are (re)written — days whose raw
    events were already pruned keep their stored aggregates. Past days
    never gain new raw events (timestamps are always "now"), so a stored
    aggregate for a complete day is final. Returns the number of days
    written (0 on no-op/error)."""
    if _conn is None:
        return 0
    try:
        start = _today_start()
        with _lock:
            rows = _conn.execute(
                'SELECT ts, kind, outcome, iph FROM events '
                'WHERE ts < ?', (start,)).fetchall()
        per_day = {}
        for ts, kind, outcome, iph in rows:
            day = _day_key(ts, WARSAW)
            d = per_day.setdefault(day, {
                'requests': 0, 'ok': 0, 'timeout': 0,
                'busy': 0, 'visitors': set()})
            if kind == 'request':
                d['requests'] += 1
                if outcome in ('timeout', 'busy', 'ok'):
                    d[outcome] += 1
            if iph:
                d['visitors'].add(iph)
        if not per_day:
            return 0
        with _lock:
            for day, d in per_day.items():
                _conn.execute(
                    'INSERT OR REPLACE INTO daily_rollup'
                    '(day, requests, ok, timeout, busy, visitors) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (day, d['requests'], d['ok'], d['timeout'],
                     d['busy'], len(d['visitors'])))
            _conn.commit()
        return len(per_day)
    except Exception:
        return 0


def prune_old_events():
    """Aggregate-then-delete old raw events (best-effort).

    Called on boot and at most once a day afterwards (from record_event).
    Complete days are first rolled up into daily_rollup (kept forever),
    then raw request/visit events (the ones carrying IP hashes) older
    than the retention window are deleted. Restart events carry no
    identifiers and are kept. Returns the number of deleted rows
    (0 on no-op/error)."""
    global _last_prune
    if _conn is None:
        return 0
    now = time.time()
    if now - _last_prune < 86400 and _last_prune != 0.0:
        return 0
    try:
        rollup_complete_days()
        cutoff = now - _retention_days * 86400
        with _lock:
            cur = _conn.execute(
                "DELETE FROM events WHERE ts < ? "
                "AND kind IN ('request', 'visit')", (cutoff,))
            _conn.commit()
            deleted = cur.rowcount or 0
        _last_prune = now
        return deleted
    except Exception:
        return 0


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
        prune_old_events()
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


def record_restart(version, reason=None):
    record_event('restart', outcome=version, detail=reason)


# ------------------------------------------------------------
# Queries (for the panel)
# ------------------------------------------------------------

def _day_key(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime('%Y-%m-%d')


def daily_series(days=None, tz=ZoneInfo('Europe/Warsaw'),
                 from_ts=None, to_ts=None):
    """Per-day buckets (Europe/Warsaw): requests (ok/timeout/busy) and
    unique visitors (distinct IP hashes). Zakres: from_ts/to_ts (epoch);
    bez nich — ostatnie `days` dni (domyślnie 30).

    Two-tier history: complete past days come from daily_rollup (kept
    forever, no identifiers), today and not-yet-rolled days from raw
    events — so the series spans the whole recording period. A day
    present in the rollup is never double-counted from raw events.

    Returns (days, unique_total, meta), where unique_total holds the
    distinct IP hashes found in RETAINED raw events only, and meta is
    {'unique_exact': bool, 'unique_since': 'YYYY-MM-DD' | None}:
    unique_exact is False when the range reaches before the oldest
    retained raw event (pruned identifiers cannot be distinguished
    retroactively) — the panel then labels the KPI accordingly.
    """
    out = {}
    unique_total = set()
    meta = {'unique_exact': True, 'unique_since': None}
    if _conn is None:
        return out, unique_total, meta
    if from_ts is None or to_ts is None:
        days = days or 30
        to_ts = time.time()
        from_ts = to_ts - days * 86400
    from_day = _day_key(from_ts, tz)
    to_day = _day_key(to_ts, tz)
    try:
        with _lock:
            rollup_rows = _conn.execute(
                'SELECT day, requests, ok, timeout, busy, visitors '
                'FROM daily_rollup WHERE day >= ? AND day <= ?',
                (from_day, to_day)).fetchall()
            raw_rows = _conn.execute(
                'SELECT ts, kind, outcome, iph FROM events '
                'WHERE ts >= ? AND ts <= ?', (from_ts, to_ts)).fetchall()
            oldest = _conn.execute(
                "SELECT MIN(ts) FROM events "
                "WHERE kind IN ('request', 'visit')").fetchone()
    except Exception:
        return out, unique_total, meta
    rolled = set()
    for day, req, ok, timeout, busy, visitors in rollup_rows:
        out[day] = {'requests': req, 'ok': ok, 'timeout': timeout,
                    'busy': busy, 'visitors': visitors}
        rolled.add(day)
    for ts, kind, outcome, iph in raw_rows:
        if iph:
            unique_total.add(iph)
        day = _day_key(ts, tz)
        if day in rolled:
            continue  # covered by the rollup — no double counting
        d = out.setdefault(day, {'requests': 0, 'ok': 0, 'timeout': 0,
                                 'busy': 0, 'visitors': set()})
        if kind == 'request':
            d['requests'] += 1
            if outcome in ('timeout', 'busy', 'ok'):
                d[outcome] += 1
        if iph:
            d['visitors'].add(iph)
    for d in out.values():
        if isinstance(d['visitors'], set):
            d['visitors'] = len(d['visitors'])
    oldest_ts = oldest[0] if oldest else None
    if oldest_ts is not None:
        meta['unique_since'] = _day_key(oldest_ts, tz)
        meta['unique_exact'] = from_ts >= oldest_ts
    else:
        # No raw request/visit events retained: distinct counts are exact
        # only when there is no rolled-up history in range either.
        meta['unique_exact'] = not rolled
    return dict(sorted(out.items())), unique_total, meta


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
    """Server restarts from the events table (zakres opcjonalny).
    reason — powód restartu (autoupdate) albo 'ręczny start'."""
    if _conn is None:
        return []
    try:
        q = ("SELECT ts, outcome, extra FROM events "
             "WHERE kind='restart'")
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
        return [{'ts': ts, 'version': v or '', 'reason': r or ''}
                for ts, v, r in rows]
    except Exception:
        return []


def read_last_reason(boot_ts, max_age=600, log_path=None):
    """Ostatni powód restartu z autoupdate.log sprzed bootu serwera.

    autoupdate.loguje '📌 Powód: ...' przed każdym restartem, który wykonuje
    (nowe commity / błąd spójności / serwer nie odpowiada). Jeśli ostatni
    taki wpis jest świeższy niż `max_age` sekund przed startem procesu,
    to jest to powód TEGO restartu; brak świeżego wpisu = restart ręczny.
    Zwraca string albo None."""
    try:
        p = log_path or os.path.join(
            os.path.dirname(os.path.dirname(_DB_PATH)), 'autoupdate.log')
        marker = 'Powód:'
        best, best_ts = None, None
        with open(p, encoding='utf-8', errors='ignore') as f:
            for line in f:
                i = line.find(marker)
                if i == -1:
                    continue
                ts_str = line[1:20]
                try:
                    ts = datetime.strptime(
                        ts_str, '%Y-%m-%d %H:%M:%S').timestamp()
                except Exception:
                    continue
                if ts <= boot_ts and (best_ts is None or ts > best_ts):
                    best_ts = ts
                    best = line[i + len(marker):].strip()
        if best is None or best_ts is None:
            return None
        if boot_ts - best_ts > max_age:
            return None  # zbyt stary powód — restart nie z autoupdate
        return best[:200]
    except Exception:
        return None


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
