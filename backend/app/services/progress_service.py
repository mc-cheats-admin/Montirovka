"""Progress publication and streaming service for AutoEdit backend.

This module provides a small, focused abstraction over Redis pub/sub for
real-time job progress updates.

Required public API:
- publish_progress(job_id: str, payload: dict[str, Any]) -> None
- progress_channel(job_id: str) -> str
- stream_progress(job_id: str) -> Iterator[dict[str, Any]] | AsyncIterator[dict[str, Any]]

Design goals:
- keep worker-side progress publication simple and synchronous;
- provide an async generator suitable for FastAPI WebSocket routes;
- use JSON payloads over Redis pub/sub channels;
- be resilient: progress publication failures should not crash the pipeline;
- remain compatible with the current consolidated project structure.

Channel naming contract:
- job_progress:{job_id}

Typical payload shape:
{
    "job_id": "<uuid>",
    "status": "enhancing",
    "current_stage": "enhancing",
    "progress_percent": 48,
    "message": "Applying stabilization pass",
    "timestamp": "2026-03-06T12:00:00+00:00"
}
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import UUID

from app.core.config import get_settings
from app.core.logging import get_logger

try:
    import redis
    import redis.asyncio as redis_async
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - defensive import path for partial environments
    redis = None  # type: ignore[assignment]
    redis_async = None  # type: ignore[assignment]

    class RedisError(Exception):
        """Fallback Redis error type used when redis package is unavailable."""


logger = get_logger(__name__)

_CHANNEL_PREFIX = "job_progress"
_STREAM_POLL_INTERVAL_SECONDS = 0.25


def _utc_now_iso() -> str:
    """Return the current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _is_probably_uuid(value: str) -> bool:
    """Best-effort validation that a string looks like a UUID.

    The service should remain permissive because some tests may use simple job
    identifiers. Therefore invalid values are not rejected globally, but this
    helper is used only for payload normalization where applicable.
    """
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _json_dumps(payload: dict[str, Any]) -> str:
    """Serialize payload into JSON text.

    The project may later add a dedicated JSON utility layer. For now we keep
    serialization local, predictable and UTF-8 friendly.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_loads(raw_payload: str) -> dict[str, Any] | None:
    """Deserialize a JSON string into a dictionary.

    Returns:
        Parsed dictionary, or ``None`` when the payload is malformed or not an
        object.
    """
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.warning("progress_payload_decode_failed")
        return None

    if not isinstance(parsed, dict):
        logger.warning("progress_payload_invalid_type", payload_type=type(parsed).__name__)
        return None

    return parsed


def _normalize_progress_percent(value: Any) -> int:
    """Normalize progress into integer range 0..100."""
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return 0

    if numeric < 0:
        return 0
    if numeric > 100:
        return 100
    return numeric


def _normalize_payload(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a published progress payload into a stable event structure.

    The worker pipeline is allowed to send partial data. This helper enriches
    it with defaults so WebSocket consumers receive consistent event objects.
    """
    normalized: dict[str, Any] = dict(payload)

    normalized["job_id"] = str(normalized.get("job_id") or job_id)
    normalized["status"] = str(normalized.get("status") or "queued")
    normalized["current_stage"] = str(
        normalized.get("current_stage") or normalized["status"] or "queued"
    )
    normalized["progress_percent"] = _normalize_progress_percent(
        normalized.get("progress_percent", 0)
    )
    normalized["message"] = str(normalized.get("message") or "")
    normalized["timestamp"] = str(normalized.get("timestamp") or _utc_now_iso())

    return normalized


def _get_sync_redis_client() -> Any:
    """Create a synchronous Redis client from application settings.

    Returns:
        Redis client instance.

    Raises:
        RuntimeError: If the redis package is unavailable.
    """
    if redis is None:
        raise RuntimeError("REDIS_CLIENT_NOT_AVAILABLE")

    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
    )


def _get_async_redis_client() -> Any:
    """Create an asynchronous Redis client from application settings.

    Returns:
        Async Redis client instance.

    Raises:
        RuntimeError: If the async redis package path is unavailable.
    """
    if redis_async is None:
        raise RuntimeError("ASYNC_REDIS_CLIENT_NOT_AVAILABLE")

    settings = get_settings()
    return redis_async.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
    )


def progress_channel(job_id: str) -> str:
    """Return the Redis pub/sub channel name for a job.

    Args:
        job_id: Job identifier as string.

    Returns:
        Channel name using the required project contract.

    Example:
        ``job_progress:9c7f9f25-4c72-4ce3-9125-3f45b89c2a10``
    """
    return f"{_CHANNEL_PREFIX}:{job_id}"


def publish_progress(job_id: str, payload: dict[str, Any]) -> None:
    """Publish a progress event to Redis pub/sub.

    This function is intentionally best-effort. The video processing pipeline
    should not fail solely because the progress bus is temporarily unavailable.

    Args:
        job_id: Job identifier.
        payload: Progress payload dictionary.

    Behavior:
        - normalizes payload fields;
        - serializes them to JSON;
        - publishes to ``job_progress:{job_id}``;
        - logs failures without raising them to callers.
    """
    channel = progress_channel(job_id)
    normalized_payload = _normalize_payload(job_id, payload)
    serialized_payload = _json_dumps(normalized_payload)

    try:
        client = _get_sync_redis_client()
        published_count = client.publish(channel, serialized_payload)
        logger.info(
            "progress_published",
            job_id=job_id,
            channel=channel,
            subscribers=published_count,
            status=normalized_payload.get("status"),
            stage=normalized_payload.get("current_stage"),
            progress_percent=normalized_payload.get("progress_percent"),
        )
    except (RedisError, RuntimeError, OSError) as exc:
        logger.warning(
            "progress_publish_failed",
            job_id=job_id,
            channel=channel,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover - final defensive guard
        logger.error(
            "progress_publish_unexpected_error",
            job_id=job_id,
            channel=channel,
            error=str(exc),
        )


async def stream_progress(job_id: str) -> AsyncIterator[dict[str, Any]]:
    """Stream progress events for a job from Redis pub/sub.

    This function is designed for FastAPI WebSocket routes and returns an async
    iterator yielding decoded progress payload dictionaries.

    Args:
        job_id: Job identifier.

    Yields:
        Progress event dictionaries.

    Notes:
        - heartbeat messages are not generated here; the WebSocket route should
          emit them independently according to application requirements;
        - malformed Redis messages are skipped;
        - disconnect/cancellation cleanup is handled internally.

    Example integration:
        async for event in stream_progress(job_id):
            await websocket.send_json(event)
    """
    channel = progress_channel(job_id)
    client: Any = None
    pubsub: Any = None

    try:
        client = _get_async_redis_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)

        logger.info(
            "progress_stream_subscribed",
            job_id=job_id,
            channel=channel,
            valid_uuid=_is_probably_uuid(job_id),
        )

        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_STREAM_POLL_INTERVAL_SECONDS,
                )
            except asyncio.CancelledError:
                logger.info(
                    "progress_stream_cancelled",
                    job_id=job_id,
                    channel=channel,
                )
                raise
            except (RedisError, OSError) as exc:
                logger.warning(
                    "progress_stream_read_failed",
                    job_id=job_id,
                    channel=channel,
                    error=str(exc),
                )
                break

            if message is None:
                await asyncio.sleep(_STREAM_POLL_INTERVAL_SECONDS)
                continue

            raw_data = message.get("data")
            if raw_data is None:
                await asyncio.sleep(0)
                continue

            if isinstance(raw_data, bytes):
                try:
                    raw_text = raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "progress_stream_decode_failed",
                        job_id=job_id,
                        channel=channel,
                    )
                    continue
            else:
                raw_text = str(raw_data)

            payload = _json_loads(raw_text)
            if payload is None:
                continue

            normalized_payload = _normalize_payload(job_id, payload)

            logger.info(
                "progress_stream_event",
                job_id=job_id,
                channel=channel,
                status=normalized_payload.get("status"),
                stage=normalized_payload.get("current_stage"),
                progress_percent=normalized_payload.get("progress_percent"),
            )

            yield normalized_payload

    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                logger.warning(
                    "progress_stream_unsubscribe_failed",
                    job_id=job_id,
                    channel=channel,
                )

            try:
                await pubsub.close()
            except Exception:
                logger.warning(
                    "progress_stream_pubsub_close_failed",
                    job_id=job_id,
                    channel=channel,
                )

        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.warning(
                    "progress_stream_client_close_failed",
                    job_id=job_id,
                    channel=channel,
                )

        logger.info(
            "progress_stream_closed",
            job_id=job_id,
            channel=channel,
        )