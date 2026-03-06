"""Celery application bootstrap for the AutoEdit backend.

This module provides the shared Celery application instance used by:
- API code that enqueues background jobs;
- worker processes that execute long-running video pipelines;
- future maintenance tasks such as cleanup routines.

The implementation is intentionally production-oriented and synchronized with
the current project state visible in repository context:

Existing related modules:
- app.core.config.get_settings
- app.core.logging.configure_logging
- app.core.logging.get_logger
- app.services.progress_service.publish_progress
- planned worker entrypoints under app.workers.tasks and app.workers.pipeline

Design goals:
1. Keep configuration centralized and environment-driven.
2. Use Redis for broker and result backend, matching project requirements.
3. Provide safe defaults for large, long-running media processing jobs.
4. Remain importable both by the API process and Celery worker process.
5. Avoid eager side effects beyond logging configuration and Celery setup.
6. Be Windows-safe and cross-platform.

Typical usage:
- Worker CLI:
    celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
- Future beat CLI if needed:
    celery -A app.workers.celery_app:celery_app beat --loglevel=INFO

Public exports:
- celery_app: Celery
- get_celery_app() -> Celery
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger, worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()

# Configure structured logging immediately so imports performed by Celery worker
# subprocesses inherit a predictable logging setup.
configure_logging(settings.log_level)
logger = get_logger(__name__)


def _build_celery_config() -> dict[str, Any]:
    """Build the Celery configuration dictionary.

    The chosen settings reflect the nature of the project:
    - jobs are heavy and may run for a long time;
    - tasks should not be acknowledged before real execution;
    - worker loss should requeue jobs when possible;
    - JSON serialization keeps payloads portable and inspectable;
    - task tracking helps API/debugging surfaces.

    Returns:
        A dictionary suitable for ``Celery.conf.update(...)``.
    """
    return {
        # Core broker/backend configuration.
        "broker_url": settings.redis_url,
        "result_backend": settings.redis_url,
        # Serialization policy.
        "accept_content": ["json"],
        "task_serializer": "json",
        "result_serializer": "json",
        "event_serializer": "json",
        # Time and UTC handling.
        "enable_utc": True,
        "timezone": "UTC",
        # Task execution safety for long-running pipelines.
        "task_track_started": True,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "worker_prefetch_multiplier": 1,
        # Result behavior. Keeping results for a limited time is enough because
        # authoritative metadata is stored in PostgreSQL.
        "result_expires": 60 * 60 * settings.job_retention_hours,
        "result_extended": True,
        # Soft/hard time limits are deliberately generous because video
        # processing can be lengthy, especially on CPU-only hosts.
        "task_soft_time_limit": 60 * 60 * 6,
        "task_time_limit": 60 * 60 * 7,
        # Connection resilience.
        "broker_connection_retry_on_startup": True,
        "broker_transport_options": {
            "visibility_timeout": 60 * 60 * 8,
            "socket_keepalive": True,
        },
        "redis_backend_health_check_interval": 30,
        "redis_socket_keepalive": True,
        # Worker process behavior.
        "worker_send_task_events": True,
        "task_send_sent_event": True,
        "worker_disable_rate_limits": True,
        # Queues and routing. The project currently uses a single default media
        # processing queue, but the structure is ready for later expansion.
        "task_default_queue": "video_jobs",
        "task_default_exchange": "video_jobs",
        "task_default_routing_key": "video_jobs.default",
        "task_routes": {
            "app.workers.tasks.process_job": {
                "queue": "video_jobs",
                "routing_key": "video_jobs.process",
            },
            "app.workers.tasks.cleanup_old_jobs": {
                "queue": "maintenance",
                "routing_key": "maintenance.cleanup",
            },
        },
        # Imported task modules.
        "imports": ("app.workers.tasks",),
        # Avoid Celery hijacking root logger formatting unexpectedly; the
        # project already configures logging explicitly through structlog.
        "worker_hijack_root_logger": False,
        "worker_redirect_stdouts": False,
    }


def create_celery_app() -> Celery:
    """Create and configure the shared Celery application instance.

    Returns:
        Configured ``Celery`` application.

    Notes:
        The application name intentionally mirrors the overall project identity,
        while imports and routing target the backend worker package.
    """
    app = Celery("autoedit")
    app.conf.update(_build_celery_config())

    logger.info(
        "celery_app_configured",
        broker_url=settings.redis_url,
        default_queue=app.conf.task_default_queue,
        result_backend=app.conf.result_backend,
    )

    return app


celery_app = create_celery_app()


@worker_process_init.connect
def _on_worker_process_init(*_: Any, **__: Any) -> None:
    """Initialize logging and emit a startup event for each worker process.

    Celery can spawn multiple worker processes. Reapplying the logging setup is
    inexpensive and helps keep child processes consistent.
    """
    configure_logging(settings.log_level)
    get_logger(__name__).info(
        "celery_worker_process_initialized",
        app_name=settings.app_name,
        environment=settings.app_env,
    )


@after_setup_logger.connect
def _after_setup_logger(*_: Any, **__: Any) -> None:
    """Reinforce project logging after Celery logger initialization.

    Celery may initialize logging on its own depending on CLI flags and worker
    startup order. Reapplying the project logger setup keeps output consistent.
    """
    configure_logging(settings.log_level)


@after_setup_task_logger.connect
def _after_setup_task_logger(*_: Any, **__: Any) -> None:
    """Reinforce task logger formatting after Celery task logger setup."""
    configure_logging(settings.log_level)


def get_celery_app() -> Celery:
    """Return the shared Celery application instance.

    This small accessor keeps external imports explicit and test-friendly.

    Returns:
        The module-level configured Celery application.
    """
    return celery_app


# A small worker-side health probe task can be useful for diagnostics and tests.
@celery_app.task(name="app.workers.healthcheck.ping")
def ping() -> dict[str, str]:
    """Return a lightweight worker health payload.

    This task is safe, fast, and useful when verifying that the queue system is
    wired correctly in development or operational checks.
    """
    task_logger = get_logger(__name__)
    task_logger.info("celery_ping_task_executed")
    return {
        "status": "ok",
        "app_name": settings.app_name,
    }


__all__ = ["celery_app", "create_celery_app", "get_celery_app", "ping"]