"""Centralized configuration for MPK Kraków Ticket Calculator.

All magic numbers, timeouts, and tunable parameters are defined here.
"""

import os

# ============================================================
# Server Configuration
# ============================================================
DEFAULT_PORT = int(os.environ.get('PORT', 8080))
# Searches hold their HTTP thread for up to 30s, so the cap must leave
# room for everyone else's static/page requests during that window.
MAX_CONCURRENT_REQUESTS = 24
REQUEST_QUEUE_SIZE = 64

# ============================================================
# Rate Limiting
# ============================================================
RATE_LIMIT_WINDOW_SECONDS = 30.0
# One page load issues ~12 requests (HTML + CSS + JS + icons); the limit
# must comfortably cover a few loads in a row from one IP.
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_EXPENSIVE_MAX_REQUESTS = 12
RATE_LIMIT_CLEANUP_INTERVAL_SECONDS = 60.0
RATE_LIMIT_STALE_CUTOFF_MULTIPLIER = 3  # entries older than window * this are stale

# ============================================================
# Pathfinding / A* Configuration
# ============================================================
# A* safety limits
ASTAR_MAX_ITERATIONS = 200000
# Short (distance) A* budget: it is an internal upper-bound provider for the
# exact fare searches, not a user-facing mode. Phase budgets sum well below
# the 30s outer request timeout (8 + 8 + 10 = 26s + result-build overhead).
ASTAR_TIMEOUT_SECONDS = 8

# Exact Pareto fare-search budgets (per phase, wall clock). On timeout the
# phase returns an ERROR — exact-only product, no approximate fallbacks.
CHEAP_SEARCH_MAX_SECONDS = 10.0
CONVENIENT_SEARCH_MAX_SECONDS = 8.0
CHEAP_SEARCH_CONCURRENCY = 2  # 1-core VPS: ≤ PATHFINDING_EXECUTOR_WORKERS
CHEAP_HEURISTIC_CACHE_MAX = 14000  # LRU cache: key space ~13,680

# Memory protection: RSS threshold (MB) — A* bails out to avoid OOM on
# small servers.  Use current RSS (via /proc/self/statm) not peak (ru_maxrss)
# because peak never decreases and would kill every search after first spike.
MEMORY_LIMIT_MB = int(os.environ.get('MEMORY_LIMIT_MB', 190))

# "Convenient" route: exact objective = fare + this penalty per boarding.
# Each transfer is "worth" this much extra zł — tunes the balance between
# ticket price and number of rides (higher = fewer transfers, pricier).
CONVENIENT_BOARDING_PENALTY_ZL = 2.0

# Executor threads running pathfinding. More than the cheap-search gate so
# that distance-based searches are never queued behind fare searches that
# are waiting for the gate.
# The GIL serialises CPU-bound searches anyway (total throughput is the
    # same for any pool size), but a larger pool lets queued requests START
    # earlier — their 30s budget then covers real work instead of queue
    # wait. 2 workers ≈ 2 searches in flight; memory is guarded by
    # MEMORY_LIMIT_MB and the queue shed below.
PATHFINDING_EXECUTOR_WORKERS = 2
# Peak shedding: when this many searches are already WAITING in the
# executor queue, new requests fail fast with 503 + Retry-After instead of
# sitting 30s for a guaranteed timeout. The frontend retries 503s
# automatically, and by then earlier results are cached / dedup applies.
PATHFINDING_QUEUE_SHED = 2

# Cache configuration (byte budgets can be tuned via environment variables)
FIND_CACHE_MAX_BYTES = int(os.environ.get('FIND_CACHE_MAX_BYTES', 20 * 1024 * 1024))

ROUTE_CACHE_MAX_ENTRIES = 5000
ROUTE_CACHE_MAX_BYTES = int(os.environ.get('ROUTE_CACHE_MAX_BYTES', 25 * 1024 * 1024))

# Platform pairing limits
MAX_PLATFORMS_TO_TRY_PER_GROUP = 2

# Transfer time (5 minutes in seconds)
TRANSFER_TIME_SECONDS = 300

# Price calculation constants (also in pricing.json)
ACC_CAP_KM = 8.5  # beyond this distance, riding is free (daily cap)
PRICE_LOOKUP_MAX_KM = 20.0

# ============================================================
# Static File Serving
# ============================================================
STATIC_CACHE_MAX_AGE_VERSIONED = 31536000  # 1 year for versioned assets
STATIC_CACHE_MAX_AGE_UNVERSIONED = 3600    # 1 hour for unversioned assets
OG_IMAGE_CACHE_MAX_AGE = 3600              # 1 hour
FAVICON_CACHE_MAX_AGE = 86400              # 1 day

# ============================================================
# Search Configuration
# ============================================================
SEARCH_MIN_QUERY_LENGTH = 2
SEARCH_MAX_QUERY_LENGTH = 100
SEARCH_MAX_RESULTS = 50
SEARCH_PREFIX_MAX_LENGTH = 5

# ============================================================
# Logging
# ============================================================
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

# ============================================================
# Security
# ============================================================
CSP_NONCE_BYTES = 16
# Canonical public origin used for absolute URLs (OG meta, OG images).
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://zaileprzeja.de').rstrip('/')
# Set TRUST_PROXY_HEADERS=true only behind a reverse proxy that overwrites
# X-Real-IP / X-Forwarded-For; when exposed directly the socket address is
# used so a spoofed header cannot rotate rate-limit buckets.
TRUST_PROXY_HEADERS = os.environ.get('TRUST_PROXY_HEADERS', 'false').lower() in ('1', 'true', 'yes')
BLOCKED_PATH_PREFIXES = ('/.', '/_')
BLOCKED_PATHS = (
    '/.env', '/.env.old', '/.env.local', '/.env.production', '/.env.development',
    '/.env.backup', '/.env.bak', '/.env.config', '/.env.staging',
    '/.git', '/.git/', '/.git/config', '/.git/HEAD',
    '/.htaccess', '/.htpasswd',
    '/.vscode', '/.vscode/', '/.vscode/sftp.json',
    '/wp-admin', '/wp-login.php', '/xmlrpc.php',
    '/admin', '/phpmyadmin',
    '/cgi-bin', '/scripts',
    '/server-status', '/server-info',
    '/favicon.ico',
)

# Bot user agents for crawler detection
BOT_USER_AGENTS = [
    'facebookexternalhit', 'twitterbot', 'linkedinbot', 'slackbot',
    'telegrambot', 'discordbot', 'whatsapp', 'skypeuripreview',
    'applebot', 'bingbot', 'googlebot', 'yandexbot', 'duckduckbot',
    'facebot', 'meta-externalagent',
]

# ============================================================
# Input Validation
# ============================================================
GROUP_ID_PATTERN = r'^group_\d+$'
MAX_GROUP_ID_LENGTH = 64
MAX_DISTANCE_PARAM_LENGTH = 32
MAX_ROUTE_ID_LENGTH = 64
MAX_STOP_ID_LENGTH = 64

# ============================================================
# File Paths
# ============================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, 'processed')
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
PRICING_PATH = os.path.join(BASE_DIR, 'pricing.json')

# ============================================================
# Application Version
# ============================================================
APP_VERSION = "1.4.6"
