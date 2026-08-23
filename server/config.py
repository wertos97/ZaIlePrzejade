"""Centralized configuration for MPK Kraków Ticket Calculator.

All magic numbers, timeouts, and tunable parameters are defined here.
"""

import os

# ============================================================
# Server Configuration
# ============================================================
DEFAULT_PORT = int(os.environ.get('PORT', 8080))
MAX_CONCURRENT_REQUESTS = 20
REQUEST_QUEUE_SIZE = 64

# ============================================================
# Rate Limiting
# ============================================================
RATE_LIMIT_WINDOW_SECONDS = 10.0
# One page load issues ~12 requests (HTML + CSS + JS + icons); the limit
# must comfortably cover a few loads in a row from one IP.
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_EXPENSIVE_MAX_REQUESTS = 10
RATE_LIMIT_CLEANUP_INTERVAL_SECONDS = 60.0
RATE_LIMIT_STALE_CUTOFF_MULTIPLIER = 3  # entries older than window * this are stale

# ============================================================
# Pathfinding / A* Configuration
# ============================================================
# A* safety limits
ASTAR_MAX_ITERATIONS = 200000
ASTAR_TIMEOUT_SECONDS = 30

# Cheap (fare-based) search limits
CHEAP_SEARCH_MAX_SECONDS = 6.0
CHEAP_SEARCH_CONCURRENCY = 2  # max concurrent fare searches (GIL gate)

# Cache configuration (byte budgets can be tuned via environment variables)
FIND_CACHE_MAX_BYTES = int(os.environ.get('FIND_CACHE_MAX_BYTES', 20 * 1024 * 1024))

ROUTE_CACHE_MAX_ENTRIES = 3000
ROUTE_CACHE_MAX_BYTES = int(os.environ.get('ROUTE_CACHE_MAX_BYTES', 24 * 1024 * 1024))

# Platform pairing limits
MAX_PLATFORMS_TO_TRY_PER_GROUP = 3

# Transfer time (5 minutes in seconds)
TRANSFER_TIME_SECONDS = 300

# Price calculation constants (also in pricing.json)
ACC_CAP_KM = 8.5  # beyond this distance, riding is free (daily cap)
PRICE_LOOKUP_MAX_KM = 20.0

# ============================================================
# Cache Warmup
# ============================================================
WARMUP_SAMPLE_SIZE = 40
WARMUP_PAIRS_PER_SAMPLE = 2
WARMUP_YIELD_DELAY_SECONDS = 0.05

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
# Set TRUST_PROXY_HEADERS=false when the app is exposed directly (no reverse
# proxy in front); otherwise X-Real-IP / X-Forwarded-For are honoured.
TRUST_PROXY_HEADERS = os.environ.get('TRUST_PROXY_HEADERS', 'true').lower() in ('1', 'true', 'yes')
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
APP_VERSION = "1.0.0"
