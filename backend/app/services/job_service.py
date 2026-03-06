"""Job service layer for AutoEdit backend.

This module centralizes job lifecycle operations and provides a stable service
API for FastAPI routes, worker pipeline code, and tests.

Why this file exists:
- keep route handlers thin and focused on HTTP concerns;
- isolate SQLAlchemy data manipulation in one place;
- guarantee consistent status transitions and response shaping;
- preserve compatibility with the current consolidated project structure.

The project context shown in this generation step currently uses:
- ``app.db.models`` as a consolidated ORM module;
- ``app.schemas`` as a consolidated Pydantic schema module;
- ``app.services.storage_service`` already existing;
- a future plan where some of these modules become more granular.

This implementation is therefore intentionally defensive:
- it works with the current consolidated structure;
- it gracefully supports optional future helpers such as preset_service;
- it uses attribute checks when model fields may vary slightly across steps.

Public functions implemented exactly as requested:
- ``create_job``
- ``get_job``
- ``update_job_status``
- ``attach_analysis``
- ``attach_result_files``
- ``cancel_job``
- ``build_job_response``

All code is Windows-compatible and uses only cross-platform Python features.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Job, MediaFile
from app.schemas import JobResponse

try:
    # Present in the fuller planned project structure.
    from app.db.models import PresetSnapshot  # type: ignore
except Exception:  # pragma: no cover - defensive compatibility for partial trees
    PresetSnapshot = None  # type: ignore[assignment]

try:
    # Present in the fuller planned project structure.
    from app.services.preset_service import (  # type: ignore
        load_preset,
        merge_user_settings,
        normalize_runtime_settings,
    )
except Exception:  # pragma: no cover - fallback for current partial project
    load_preset = None  # type: ignore[assignment]
    merge_user_settings = None  # type: ignore[assignment]
    normalize_runtime_settings = None  # type: ignore[assignment]


logger = get_logger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
RUNNING_STATUSES = {
    "analyzing",
    "cutting",
    "enhancing",
    "interpolating",
    "processing_audio",
    "generating_subtitles",
    "rendering",
    "generating_preview",
}


def _utc_now() -> datetime:
    """Return timezone-aware UTC datetime.

    A tiny helper keeps timestamp creation consistent across this module.
    """
    return datetime.now(timezone.utc)


def _commit_refresh(db: Session, instance: Any) -> Any:
    """Commit current transaction and refresh the given ORM instance.

    Args:
        db: Active SQLAlchemy session.
        instance: ORM row instance to refresh.

    Returns:
        The same instance after commit/refresh for fluent usage.
    """
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _set_if_present(instance: Any, field_name: str, value: Any) -> None:
    """Set an attribute only if the ORM model exposes it.

    This helper makes the service more resilient across partially generated
    project stages where ORM models may not yet contain every planned field.
    """
    if hasattr(instance, field_name):
        setattr(instance, field_name, value)


def _job_query_by_id(db: Session, job_id: UUID) -> Job | None:
    """Return a job row by UUID or ``None`` when missing."""
    return db.query(Job).filter(Job.id == job_id).first()


def _media_query_by_id(db: Session, file_id: UUID) -> MediaFile | None:
    """Return a media file row by UUID or ``None`` when missing."""
    return db.query(MediaFile).filter(MediaFile.id == file_id).first()


def _safe_model_dict(value: Any) -> dict[str, Any]:
    """Convert model payload-ish value to a plain dictionary.

    Supports:
    - ``dict`` values directly;
    - Pydantic-like models with ``model_dump``;
    - objects with ``dict()``;
    - JSON-like plain objects.

    Returns an empty dictionary when conversion is not safe or possible.
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    model_dump: Callable[..., Any] | None = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped

    dict_method: Callable[..., Any] | None = getattr(value, "dict", None)
    if callable(dict_method):
        dumped = dict_method(exclude_none=True) if "exclude_none" in dict_method.__code__.co_varnames else dict_method()
        if isinstance(dumped, dict):
            return dumped

    return {}


def _fallback_merge_settings(preset_name: str, user_settings: dict[str, Any]) -> dict[str, Any]:
    """Fallback preset merge logic used when preset_service is not yet available.

    The full planned project contains a dedicated preset service. Until that
    file exists, this helper guarantees predictable behavior and keeps tests
    and routes usable.

    The returned structure intentionally includes the top-level user-facing
    settings expected by worker/pipeline code.
    """
    normalized_user = {key: value for key, value in user_settings.items() if value is not None}

    base_by_preset: dict[str, dict[str, Any]] = {
        "gaming": {
            "preset_name": "gaming",
            "display_name": "Gaming / Highlight",
            "target_fps": 120,
            "zoom_scale": 1.3,
            "cut_aggressiveness": 0.7,
            "noise_reduction_enabled": True,
            "subtitles_enabled": False,
            "output_aspect_ratio": "16:9",
            "codec": "h264",
        },
        "tutorial": {
            "preset_name": "tutorial",
            "display_name": "Tutorial / Обучение",
            "target_fps": 60,
            "zoom_scale": 1.1,
            "cut_aggressiveness": 0.85,
            "noise_reduction_enabled": True,
            "subtitles_enabled": True,
            "output_aspect_ratio": "16:9",
            "codec": "h264",
        },
        "cinematic": {
            "preset_name": "cinematic",
            "display_name": "Cinematic / Контент",
            "target_fps": 24,
            "zoom_scale": 1.0,
            "cut_aggressiveness": 0.35,
            "noise_reduction_enabled": False,
            "subtitles_enabled": False,
            "output_aspect_ratio": "21:9",
            "codec": "h265",
        },
    }

    merged = dict(base_by_preset.get(preset_name, {"preset_name": preset_name}))
    merged.update(normalized_user)
    return merged


def _build_merged_settings(preset_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Build final runtime settings snapshot for a job.

    Preferred behavior:
    1. load built-in preset via preset_service;
    2. merge user overrides;
    3. normalize runtime fields.

    Fallback behavior:
    - use a local minimal default mapping.

    Args:
        preset_name: Built-in preset name.
        settings: User-provided overrides from request payload.

    Returns:
        Fully merged settings dictionary suitable for storing as snapshot JSON.
    """
    clean_settings = {key: value for key, value in dict(settings).items() if value is not None}

    if callable(load_preset) and callable(merge_user_settings) and callable(normalize_runtime_settings):
        preset_config = load_preset(preset_name)
        merged = merge_user_settings(preset_config, clean_settings)
        normalized = normalize_runtime_settings(preset_name, merged)
        if isinstance(normalized, dict):
            return normalized

    return _fallback_merge_settings(preset_name, clean_settings)


def _extract_job_result_payload(job: Job) -> dict[str, Any] | None:
    """Build the ``result`` object for API responses from ORM fields.

    The current consolidated ``app.schemas.JobResponse`` supports several
    optional result-related keys. This helper populates the ones available in
    the current ORM model without making assumptions beyond existing fields.
    """
    output_file_id = getattr(job, "output_file_id", None)
    preview_file_id = getattr(job, "preview_file_id", None)
    subtitle_file_id = getattr(job, "subtitle_file_id", None)

    if not output_file_id and not preview_file_id and not subtitle_file_id:
        return None

    payload: dict[str, Any] = {
        "output_file_id": output_file_id,
        "preview_file_id": preview_file_id,
        "subtitle_file_id": subtitle_file_id,
    }

    return payload


def _extract_job_error_payload(job: Job) -> dict[str, str] | None:
    """Build the ``error`` object for API responses."""
    error_code = getattr(job, "error_code", None)
    error_message = getattr(job, "error_message", None)

    if not error_code and not error_message:
        return None

    return {
        "error_code": error_code or "INTERNAL_SERVER_ERROR",
        "message": error_message or "Во время обработки задачи произошла ошибка.",
    }


def _job_to_response_payload(job: Job) -> dict[str, Any]:
    """Convert a Job ORM instance into the dictionary expected by JobResponse.

    The helper prefers a model-provided ``to_dict()`` when available, but also
    supports manual assembly for compatibility.
    """
    to_dict_method = getattr(job, "to_dict", None)
    if callable(to_dict_method):
        payload = to_dict_method()
        if isinstance(payload, dict):
            # Normalize a few fields to the current schema contract.
            payload.setdefault("job_id", payload.get("id", getattr(job, "id", None)))
            payload.setdefault("analysis", getattr(job, "analysis_json", None))
            payload.setdefault("result", _extract_job_result_payload(job))
            payload.setdefault("error", _extract_job_error_payload(job))
            payload.setdefault("original_filename", getattr(job, "original_filename", None))
            payload.setdefault("preset_name", getattr(job, "preset_name", "unknown"))
            payload.setdefault("status", getattr(job, "status", "queued"))
            payload.setdefault("current_stage", getattr(job, "current_stage", "queued"))
            payload.setdefault("progress_percent", getattr(job, "progress_percent", 0))
            payload.setdefault("created_at", getattr(job, "created_at", None))
            payload.setdefault("updated_at", getattr(job, "updated_at", None))
            payload.setdefault("started_at", getattr(job, "started_at", None))
            payload.setdefault("completed_at", getattr(job, "completed_at", None))
            return payload

    return {
        "job_id": getattr(job, "id"),
        "status": getattr(job, "status", "queued"),
        "current_stage": getattr(job, "current_stage", "queued"),
        "progress_percent": getattr(job, "progress_percent", 0),
        "preset_name": getattr(job, "preset_name", "unknown"),
        "analysis": getattr(job, "analysis_json", None),
        "result": _extract_job_result_payload(job),
        "error": _extract_job_error_payload(job),
        "created_at": getattr(job, "created_at", None),
        "updated_at": getattr(job, "updated_at", None),
        "started_at": getattr(job, "started_at", None),
        "completed_at": getattr(job, "completed_at", None),
        "original_filename": getattr(job, "original_filename", None),
    }


def _create_preset_snapshot_if_supported(
    db: Session,
    job: Job,
    preset_name: str,
    merged_settings: dict[str, Any],
) -> None:
    """Persist preset snapshot row when the ORM model exists in this project step.

    The fuller planned project contains ``PresetSnapshot``. In the current
    partial structure this model may not yet exist, so the function is a safe
    no-op in that scenario.
    """
    if PresetSnapshot is None:
        return

    snapshot = PresetSnapshot(
        job_id=getattr(job, "id"),
        preset_name=preset_name,
        config_json=merged_settings,
    )
    db.add(snapshot)


def create_job(db: Session, file_id: UUID, preset_name: str, settings: dict[str, Any]) -> Job:
    """Create a new processing job in ``queued`` state.

    Behavior:
    - validates existence of uploaded media file;
    - stores original filename from media metadata when available;
    - merges preset defaults with user settings;
    - persists a preset snapshot row when supported by current ORM;
    - initializes the job as queued at 0%.

    Args:
        db: Active SQLAlchemy session.
        file_id: Uploaded media file ID.
        preset_name: One of built-in preset names.
        settings: User-provided settings overrides.

    Returns:
        Persisted ``Job`` ORM instance.

    Raises:
        RuntimeError: When uploaded media file does not exist.
    """
    media_file = _media_query_by_id(db, file_id)
    if media_file is None:
        raise RuntimeError("FILE_NOT_FOUND")

    merged_settings = _build_merged_settings(preset_name, settings)

    original_filename = (
        getattr(media_file, "public_name", None)
        or getattr(media_file, "original_filename", None)
        or "uploaded_video"
    )

    now = _utc_now()

    job = Job(
        status="queued",
        preset_name=preset_name,
        original_filename=original_filename,
        input_file_id=file_id,
        settings_json=merged_settings,
        progress_percent=0,
        current_stage="queued",
    )

    # Preserve expected timestamp fields when the model already contains them.
    _set_if_present(job, "created_at", now)
    _set_if_present(job, "updated_at", now)
    _set_if_present(job, "started_at", None)
    _set_if_present(job, "completed_at", None)
    _set_if_present(job, "analysis_json", None)
    _set_if_present(job, "error_code", None)
    _set_if_present(job, "error_message", None)
    _set_if_present(job, "output_file_id", None)
    _set_if_present(job, "preview_file_id", None)
    _set_if_present(job, "subtitle_file_id", None)

    db.add(job)
    db.flush()

    _create_preset_snapshot_if_supported(db, job, preset_name, merged_settings)

    db.commit()
    db.refresh(job)

    logger.info(
        "job_created",
        job_id=str(getattr(job, "id", "")),
        preset_name=preset_name,
        input_file_id=str(file_id),
        original_filename=original_filename,
        current_stage="queued",
    )

    return job


def get_job(db: Session, job_id: UUID) -> Job | None:
    """Return a job by UUID or ``None`` if it does not exist."""
    job = _job_query_by_id(db, job_id)

    logger.info(
        "job_fetched",
        job_id=str(job_id),
        found=job is not None,
    )

    return job


def update_job_status(
    db: Session,
    job_id: UUID,
    status: str,
    current_stage: str,
    progress_percent: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Job:
    """Update core lifecycle fields for a job.

    Additional behavior:
    - clamps progress to the [0, 100] range;
    - sets ``started_at`` the first time a running status is observed;
    - sets ``completed_at`` for terminal statuses;
    - clears previous error fields unless new ones are provided.

    Args:
        db: Active SQLAlchemy session.
        job_id: Target job UUID.
        status: New status string.
        current_stage: New stage label.
        progress_percent: Integer progress percentage.
        error_code: Optional machine-readable error code.
        error_message: Optional human-readable error message.

    Returns:
        Updated ``Job`` ORM instance.

    Raises:
        RuntimeError: If the job does not exist.
    """
    job = _job_query_by_id(db, job_id)
    if job is None:
        raise RuntimeError("JOB_NOT_FOUND")

    normalized_progress = max(0, min(100, int(progress_percent)))
    now = _utc_now()

    previous_status = getattr(job, "status", None)
    _set_if_present(job, "status", status)
    _set_if_present(job, "current_stage", current_stage)
    _set_if_present(job, "progress_percent", normalized_progress)
    _set_if_present(job, "updated_at", now)

    if status in RUNNING_STATUSES and getattr(job, "started_at", None) is None:
        _set_if_present(job, "started_at", now)

    if status in TERMINAL_STATUSES:
        _set_if_present(job, "completed_at", now)

    if error_code is not None or error_message is not None:
        _set_if_present(job, "error_code", error_code)
        _set_if_present(job, "error_message", error_message)
    elif status != "failed":
        # Clear stale error information when transitioning into non-failed states.
        _set_if_present(job, "error_code", None)
        _set_if_present(job, "error_message", None)

    job = _commit_refresh(db, job)

    logger.info(
        "job_status_updated",
        job_id=str(job_id),
        previous_status=previous_status,
        status=status,
        current_stage=current_stage,
        progress_percent=normalized_progress,
        error_code=error_code,
    )

    return job


def attach_analysis(db: Session, job_id: UUID, analysis: dict[str, Any]) -> Job:
    """Attach analysis payload to a job.

    Args:
        db: Active SQLAlchemy session.
        job_id: Target job UUID.
        analysis: JSON-serializable analysis dictionary.

    Returns:
        Updated job row.

    Raises:
        RuntimeError: If the job does not exist.
    """
    job = _job_query_by_id(db, job_id)
    if job is None:
        raise RuntimeError("JOB_NOT_FOUND")

    _set_if_present(job, "analysis_json", dict(analysis))
    _set_if_present(job, "updated_at", _utc_now())

    job = _commit_refresh(db, job)

    logger.info(
        "job_analysis_attached",
        job_id=str(job_id),
        keys=sorted(list(analysis.keys())),
    )

    return job


def attach_result_files(
    db: Session,
    job_id: UUID,
    output_file_id: UUID | None,
    preview_file_id: UUID | None,
    subtitle_file_id: UUID | None,
) -> Job:
    """Attach generated output-related file IDs to a job.

    Note:
    - the current project step models a single ``preview_file_id`` field;
    - fuller future steps may additionally expose before/after/thumbnail IDs via
      separate result payloads or related tables.

    Args:
        db: Active SQLAlchemy session.
        job_id: Target job UUID.
        output_file_id: Final rendered video file ID.
        preview_file_id: Preview asset file ID.
        subtitle_file_id: Subtitle sidecar file ID.

    Returns:
        Updated job row.

    Raises:
        RuntimeError: If the job does not exist.
    """
    job = _job_query_by_id(db, job_id)
    if job is None:
        raise RuntimeError("JOB_NOT_FOUND")

    _set_if_present(job, "output_file_id", output_file_id)
    _set_if_present(job, "preview_file_id", preview_file_id)
    _set_if_present(job, "subtitle_file_id", subtitle_file_id)
    _set_if_present(job, "updated_at", _utc_now())

    job = _commit_refresh(db, job)

    logger.info(
        "job_result_files_attached",
        job_id=str(job_id),
        output_file_id=str(output_file_id) if output_file_id else None,
        preview_file_id=str(preview_file_id) if preview_file_id else None,
        subtitle_file_id=str(subtitle_file_id) if subtitle_file_id else None,
    )

    return job


def cancel_job(db: Session, job_id: UUID) -> Job | None:
    """Mark a job as cancelled when possible.

    Behavior:
    - returns ``None`` if the job does not exist;
    - idempotently returns the job when already terminal;
    - marks non-terminal jobs as cancelled with current stage ``cancelled``.

    Args:
        db: Active SQLAlchemy session.
        job_id: Job UUID.

    Returns:
        Updated job instance or ``None`` if missing.
    """
    job = _job_query_by_id(db, job_id)
    if job is None:
        logger.warning("job_cancel_requested_missing", job_id=str(job_id))
        return None

    current_status = getattr(job, "status", "queued")
    if current_status in TERMINAL_STATUSES:
        logger.info(
            "job_cancel_skipped_terminal",
            job_id=str(job_id),
            status=current_status,
        )
        return job

    now = _utc_now()
    _set_if_present(job, "status", "cancelled")
    _set_if_present(job, "current_stage", "cancelled")
    _set_if_present(job, "progress_percent", max(0, min(100, int(getattr(job, "progress_percent", 0)))))
    _set_if_present(job, "error_code", "JOB_CANCELLED")
    _set_if_present(job, "error_message", "Задача была отменена пользователем.")
    _set_if_present(job, "completed_at", now)
    _set_if_present(job, "updated_at", now)

    job = _commit_refresh(db, job)

    logger.info(
        "job_cancelled",
        job_id=str(job_id),
        previous_status=current_status,
    )

    return job


def build_job_response(job: Job) -> JobResponse:
    """Build a validated API response model from a ``Job`` ORM instance.

    This function is the main translation boundary between persistence and API
    contracts. It ensures the returned payload conforms to the current
    consolidated ``app.schemas.JobResponse`` model.

    Args:
        job: ORM job instance.

    Returns:
        Pydantic ``JobResponse`` object.
    """
    payload = _job_to_response_payload(job)

    # Normalize nested fields for schema compatibility.
    analysis_value = payload.get("analysis")
    if analysis_value is not None and not isinstance(analysis_value, dict):
        payload["analysis"] = _safe_model_dict(analysis_value) or None

    result_value = payload.get("result")
    if result_value is not None and not isinstance(result_value, dict):
        payload["result"] = _safe_model_dict(result_value) or None

    error_value = payload.get("error")
    if error_value is not None and not isinstance(error_value, dict):
        payload["error"] = _safe_model_dict(error_value) or None

    response = JobResponse.model_validate(payload)

    logger.info(
        "job_response_built",
        job_id=str(response.job_id),
        status=response.status,
        current_stage=response.current_stage,
        progress_percent=response.progress_percent,
    )

    return response