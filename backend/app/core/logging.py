"""Structured JSON logging configuration for the AutoEdit backend.

This module provides two public functions required by the rest of the project:

- ``configure_logging(log_level: str) -> None``
- ``get_logger(name: str) -> structlog.stdlib.BoundLogger``

Primary goals of this logging setup:
1. Emit JSON logs to stdout for container-friendly operation.
2. Keep logs consistent across FastAPI API handlers, services and Celery workers.
3. Preserve important contextual fields such as:
   - timestamp
   - level
   - logger
   - event
   - job_id
   - stage
4. Remain safe for repeated imports and repeated startup initialization.
5. Avoid logging secrets or binary payloads by default.

The configuration intentionally uses the standard logging module together with
``structlog`` so that:
- third-party libraries still integrate with Python logging;
- application code can use structured ``logger.info("event_name", key=value)``;
- exception traces are serialized in a machine-readable way.

This file is Windows-compatible and uses only cross-platform Python features.
"""

from __future__ import annotations

import logging
import logging.config
import sys
from typing import Any

import structlog


_CONFIGURED = False


def _normalize_log_level(log_level: str | None) -> str:
    """Normalize a user-provided log level to a safe uppercase string.

    Args:
        log_level: Raw log level value, typically from environment settings.

    Returns:
        A normalized logging level name suitable for both stdlib logging and
        structlog configuration.
    """
    if not log_level:
        return "INFO"

    normalized = str(log_level).strip().upper()
    return normalized or "INFO"


def _shared_processors() -> list[Any]:
    """Build the common structlog processor chain.

    The returned processors are shared by stdlib integration and final rendering.
    The chain is designed to produce stable JSON with project-specific fields.

    Returns:
        A list of structlog processors.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.EventRenamer("event"),
    ]


def _renderer_processors() -> list[Any]:
    """Build the final processor chain used by the stdlib formatter.

    Returns:
        A list of processors ending in JSON rendering.
    """
    return [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]


def _build_logging_dict_config(log_level: str) -> dict[str, Any]:
    """Create the stdlib logging configuration dictionary.

    Args:
        log_level: Effective normalized log level.

    Returns:
        A ``dictConfig``-compatible configuration.
    """
    pre_chain = _shared_processors()

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.processors.JSONRenderer(),
                "foreign_pre_chain": pre_chain,
                "processors": _renderer_processors(),
            }
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json",
                "level": log_level,
            }
        },
        "loggers": {
            "": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "celery": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "sqlalchemy": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
            "asyncio": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }


def configure_logging(log_level: str) -> None:
    """Configure structured application logging.

    This function is intentionally idempotent enough for practical use in:
    - FastAPI startup;
    - Celery worker startup;
    - tests that initialize the application more than once.

    Reconfiguration is allowed so that different processes or tests can apply
    their own effective log level. The module-level flag mainly records that
    logging has already been initialized at least once.

    Args:
        log_level: Desired root log level, e.g. ``INFO`` or ``DEBUG``.
    """
    global _CONFIGURED

    effective_level = _normalize_log_level(log_level)

    logging.config.dictConfig(_build_logging_dict_config(effective_level))

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.captureWarnings(True)
    _CONFIGURED = True

    bootstrap_logger = structlog.get_logger(__name__)
    bootstrap_logger.info(
        "logging_configured",
        configured=_CONFIGURED,
        log_level=effective_level,
        logger=__name__,
        job_id=None,
        stage=None,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound with default project fields.

    The project requirements explicitly mention common fields such as ``job_id``
    and ``stage``. To keep the output schema stable, these keys are pre-bound
    with ``None`` values and can later be overridden by callers:

        logger = get_logger(__name__).bind(job_id="...", stage="analyzing")

    Args:
        name: Logger name, typically ``__name__``.

    Returns:
        A configured ``structlog.stdlib.BoundLogger`` instance.
    """
    if not _CONFIGURED:
        configure_logging("INFO")

    return structlog.get_logger(name).bind(job_id=None, stage=None)