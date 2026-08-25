"""Structured logging configuration for MPK Kraków Ticket Calculator.

Provides JSON-formatted logging with consistent fields for production monitoring.
"""

import json
import logging
import logging.handlers
import sys
import time
from typing import Optional


# Standard LogRecord attributes that are not user-defined "extra" fields.
_STD_RECORD_ATTRS = frozenset(vars(logging.LogRecord(
    '%(name)s', logging.INFO, '%(pathname)s', 0, '%(message)s', None, None
))) | {'message', 'asctime'}


class JsonFormatter(logging.Formatter):
    """JSON log formatter; any extra fields are passed through as-is."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_RECORD_ATTRS and not key.startswith('_'):
                log_entry[key] = value

        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_logging(
    level: str = 'INFO',
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5
) -> logging.Logger:
    """Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for rotating file handler
        max_bytes: Max size per log file before rotation
        backup_count: Number of backup files to keep

    Returns:
        Root logger instance
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler with JSON formatting
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(console_handler)

    # File handler with rotation (if log_file specified)
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(JsonFormatter())
        root_logger.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module."""
    return logging.getLogger(name)


def log_cache_event(
    logger: logging.Logger,
    cache_type: str,
    event: str,
    entries: int,
    bytes_used: int,
    max_bytes: int
) -> None:
    """Log a cache operation."""
    logger.info(
        f'Cache {event}',
        extra={
            'cache_type': cache_type,
            'event': event,
            'entries': entries,
            'bytes_used': bytes_used,
            'max_bytes': max_bytes,
            'utilization_pct': round(bytes_used / max_bytes * 100, 1) if max_bytes > 0 else 0,
        }
    )


# Initialize default logger
logger = get_logger('mpk')
