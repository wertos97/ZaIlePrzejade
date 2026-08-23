"""Structured logging configuration for MPK Kraków Ticket Calculator.

Provides JSON-formatted logging with consistent fields for production monitoring.
"""

import json
import logging
import logging.handlers
import sys
import time
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    """JSON log formatter with standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created)),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, 'request_id'):
            log_entry['request_id'] = record.request_id
        if hasattr(record, 'client_ip'):
            log_entry['client_ip'] = record.client_ip
        if hasattr(record, 'path'):
            log_entry['path'] = record.path
        if hasattr(record, 'method'):
            log_entry['method'] = record.method
        if hasattr(record, 'status_code'):
            log_entry['status_code'] = record.status_code
        if hasattr(record, 'duration_ms'):
            log_entry['duration_ms'] = record.duration_ms
        if hasattr(record, 'error'):
            log_entry['error'] = record.error

        # Include exception info if present
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


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


class RequestLogger:
    """Context manager for logging HTTP requests with timing."""

    def __init__(self, logger: logging.Logger, method: str, path: str, client_ip: str):
        self.logger = logger
        self.method = method
        self.path = path
        self.client_ip = client_ip
        self.start_time = time.time()
        self.status_code = 200
        self.error = None

    def __enter__(self) -> 'RequestLogger':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        duration_ms = int((time.time() - self.start_time) * 1000)
        if exc_type:
            self.status_code = 500
            self.error = str(exc_val)
        self.logger.info(
            'HTTP request completed',
            extra={
                'method': self.method,
                'path': self.path,
                'client_ip': self.client_ip,
                'status_code': self.status_code,
                'duration_ms': duration_ms,
                'error': self.error,
            }
        )


def log_request(
    logger: logging.Logger,
    method: str,
    path: str,
    client_ip: str,
    status_code: int,
    duration_ms: int,
    error: Optional[str] = None
) -> None:
    """Log an HTTP request with structured fields."""
    logger.info(
        'HTTP request',
        extra={
            'method': method,
            'path': path,
            'client_ip': client_ip,
            'status_code': status_code,
            'duration_ms': duration_ms,
            'error': error,
        }
    )


def log_rate_limit(
    logger: logging.Logger,
    client_ip: str,
    endpoint: str,
    limit: int
) -> None:
    """Log rate limit exceeded event."""
    logger.warning(
        'Rate limit exceeded',
        extra={
            'client_ip': client_ip,
            'endpoint': endpoint,
            'limit': limit,
            'event': 'rate_limit_exceeded',
        }
    )


def log_pathfinding(
    logger: logging.Logger,
    from_id: str,
    to_id: str,
    mode: str,
    duration_ms: int,
    success: bool,
    error: Optional[str] = None
) -> None:
    """Log pathfinding operation."""
    if success:
        logger.info(
            'Pathfinding completed',
            extra={
                'from_id': from_id,
                'to_id': to_id,
                'mode': mode,
                'duration_ms': duration_ms,
                'event': 'pathfinding_success',
            }
        )
    else:
        logger.warning(
            'Pathfinding failed',
            extra={
                'from_id': from_id,
                'to_id': to_id,
                'mode': mode,
                'duration_ms': duration_ms,
                'error': error,
                'event': 'pathfinding_failed',
            }
        )


def log_cache_event(
    logger: logging.Logger,
    cache_type: str,
    event: str,
    entries: int,
    bytes_used: int,
    max_bytes: int
) -> None:
    """Log cache operation."""
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