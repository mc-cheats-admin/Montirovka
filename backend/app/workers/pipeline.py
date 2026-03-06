"""Main background processing pipeline for AutoEdit jobs.

This module coordinates the end-to-end lifecycle of a video processing job
executed by Celery workers. The implementation is intentionally defensive and
compatible with the currently evolving project structure.

Public API required by the project specification:
- PipelineContext
- process_job_pipeline(job_id: str) -> None
- publish_stage(job_id: str, status: str, current_stage: str, progress_percent: int, message: str) -> None
- assert_not_cancelled(job_id: str) -> None

Design goals:
1. Keep stage orchestration explicit and readable.
2. Update database job state and publish WebSocket progress after every stage.
3. Use lazy imports for heavy stage modules so the file remains importable even
   in partially generated project states or lightweight test environments.
4. Handle cancellation between stages.
5. Save output, preview and subtitle artifacts via the storage service.
6. Always clean up temporary files safely.

The pipeline attempts to integrate with:
- app.services.job_service
- app.services.progress_service
- app.services.storage_service
- app.services.file_cleanup_service
- app.workers.stages.*
- app.utils.media.probe_media
- app.db.session / app.db.models

If some modules are not yet present in the current repository state, the code
falls back to conservative local helpers where practical.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import importlib
import shutil
import traceback
from pathlib import Path
from typing import Any, Iterator

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineContext:
    """Mutable context shared across all pipeline stages.

    Attributes:
        job_id: Target job identifier as string.
        preset_name: Selected preset name for this job.
        input_path: Local accessible path to the uploaded source video.
        working_dir: Temporary working directory for intermediate artifacts.
        analysis_path: Optional path to serialized analysis file if generated.
        analysis: In-memory analysis payload collected during the analyze stage.
        settings: Fully merged runtime settings for the current job.
        intermediate_video_path: Path to the latest video artifact between stages.
        processed_audio_path: Optional path to the processed audio artifact.
        subtitle_path: Optional path to generated subtitle file.
        final_output_path: Target path for final rendered video before storing.
        preview_assets: Optional dictionary with generated preview asset paths.
    """

    job_id: str
    preset_name: str
    input_path: str
    working_dir: str
    analysis_path: str | None
    analysis: dict[str, Any] | None
    settings: dict[str, Any]
    intermediate_video_path: str
    processed_audio_path: str | None
    subtitle_path: str | None
    final_output_path: str
    preview_assets: dict[str, str] | None


# ---------------------------------------------------------------------------
# Generic import helpers
# ---------------------------------------------------------------------------


def _import_module(module_name: str) -> Any:
    """Import a module by name.

    Raises the original import exception to preserve debugging detail when the
    module is expected to exist.
    """
    return importlib.import_module(module_name)


def _import_optional_attribute(module_name: str, attribute_name: str) -> Any | None:
    """Try to import a named attribute from a module.

    Returns:
        Imported attribute or None when import/attribute lookup fails.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None

    return getattr(module, attribute_name, None)


# ---------------------------------------------------------------------------
# Database/session helpers
# ---------------------------------------------------------------------------


def _get_models_module() -> Any:
    """Return the models module.

    Supports the currently visible repository shape where all ORM models live in
    ``app.db.models``.
    """
    return _import_module("app.db.models")


def _get_job_model() -> Any:
    """Resolve the Job ORM model from the models module."""
    models_module = _get_models_module()
    job_model = getattr(models_module, "Job", None)
    if job_model is None:
        raise RuntimeError("INTERNAL_SERVER_ERROR")
    return job_model


def _get_session_factory() -> Any:
    """Resolve a SQLAlchemy session factory from the project.

    Tries common names used in SQLAlchemy 2.x based FastAPI projects.
    """
    session_module = _import_module("app.db.session")
    for candidate_name in ("SessionLocal", "session_factory", "get_session_factory"):
        candidate = getattr(session_module, candidate_name, None)
        if candidate is None:
            continue
        if callable(candidate) and candidate_name == "get_session_factory":
            return candidate()
        return candidate

    raise RuntimeError("INTERNAL_SERVER_ERROR")


@contextmanager
def _db_session_scope() -> Iterator[Any]:
    """Provide a short-lived database session for worker operations."""
    session_factory = _get_session_factory()
    db = session_factory()
    try:
        yield db
        with suppress(Exception):
            db.commit()
    except Exception:
        with suppress(Exception):
            db.rollback()
        raise
    finally:
        with suppress(Exception):
            db.close()


def _query_job(db: Any, job_id: str) -> Any | None:
    """Load a job either via service layer or direct SQLAlchemy query."""
    get_job = _import_optional_attribute("app.services.job_service", "get_job")
    if callable(get_job):
        with suppress(Exception):
            return get_job(db, job_id)

    job_model = _get_job_model()
    return db.get(job_model, job_id)


def _get_job_or_raise(db: Any, job_id: str) -> Any:
    """Return job or raise JOB_NOT_FOUND."""
    job = _query_job(db, job_id)
    if job is None:
        raise RuntimeError("JOB_NOT_FOUND")
    return job


# ---------------------------------------------------------------------------
# Generic job state helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _extract_error_code(exc: Exception) -> str:
    """Best-effort extraction of a domain error code from an exception.

    The broader project uses upper-case symbolic codes such as:
    - JOB_CANCELLED
    - FFMPEG_FAILED
    - INTERNAL_SERVER_ERROR

    Strategy:
    1. if ``exc.args[0]`` looks like an upper-case token, use it;
    2. otherwise fallback to INTERNAL_SERVER_ERROR.
    """
    if exc.args:
        first = str(exc.args[0]).strip()
        if first and first.upper() == first and " " not in first and len(first) <= 64:
            return first
        if ":" in first:
            prefix = first.split(":", 1)[0].strip()
            if prefix and prefix.upper() == prefix and " " not in prefix and len(prefix) <= 64:
                return prefix
    return "INTERNAL_SERVER_ERROR"


def _serialize_exception_message(exc: Exception) -> str:
    """Return a safe user-facing exception message."""
    message = str(exc).strip()
    if not message:
        return "Unexpected pipeline failure."
    return message[:4000]


def _update_job_status_direct(
    db: Any,
    job_id: str,
    *,
    status: str,
    current_stage: str,
    progress_percent: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Any:
    """Update job state directly through ORM when service helper is unavailable."""
    job = _get_job_or_raise(db, job_id)
    setattr(job, "status", status)
    setattr(job, "current_stage", current_stage)
    setattr(job, "progress_percent", int(progress_percent))

    if error_code is not None or hasattr(job, "error_code"):
        setattr(job, "error_code", error_code)
    if error_message is not None or hasattr(job, "error_message"):
        setattr(job, "error_message", error_message)

    if status not in {"queued", "uploaded"} and getattr(job, "started_at", None) is None:
        with suppress(Exception):
            setattr(job, "started_at", _now_utc())

    if status in {"completed", "failed", "cancelled"}:
        with suppress(Exception):
            setattr(job, "completed_at", _now_utc())

    with suppress(Exception):
        setattr(job, "updated_at", _now_utc())

    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _update_job_status(
    db: Any,
    job_id: str,
    *,
    status: str,
    current_stage: str,
    progress_percent: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Any:
    """Update job state through service layer with direct fallback."""
    update_job_status = _import_optional_attribute("app.services.job_service", "update_job_status")
    if callable(update_job_status):
        try:
            return update_job_status(
                db,
                job_id,
                status,
                current_stage,
                progress_percent,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            # Fall back to direct ORM update if service layer fails unexpectedly.
            pass

    return _update_job_status_direct(
        db,
        job_id,
        status=status,
        current_stage=current_stage,
        progress_percent=progress_percent,
        error_code=error_code,
        error_message=error_message,
    )


def _attach_analysis_direct(db: Any, job_id: str, analysis: dict[str, Any]) -> Any:
    """Attach analysis JSON directly on the job model."""
    job = _get_job_or_raise(db, job_id)
    if hasattr(job, "analysis_json"):
        setattr(job, "analysis_json", analysis)
    with suppress(Exception):
        setattr(job, "updated_at", _now_utc())
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _attach_analysis(db: Any, job_id: str, analysis: dict[str, Any]) -> Any:
    """Attach analysis using service helper with direct fallback."""
    attach_analysis = _import_optional_attribute("app.services.job_service", "attach_analysis")
    if callable(attach_analysis):
        try:
            return attach_analysis(db, job_id, analysis)
        except Exception:
            pass

    return _attach_analysis_direct(db, job_id, analysis)


def _attach_result_files(
    db: Any,
    job_id: str,
    *,
    output_file_id: Any | None,
    preview_file_id: Any | None,
    subtitle_file_id: Any | None,
) -> Any:
    """Attach output artifact ids to job using service helper with fallback."""
    attach_result_files = _import_optional_attribute("app.services.job_service", "attach_result_files")
    if callable(attach_result_files):
        try:
            return attach_result_files(
                db,
                job_id,
                output_file_id=output_file_id,
                preview_file_id=preview_file_id,
                subtitle_file_id=subtitle_file_id,
            )
        except Exception:
            pass

    job = _get_job_or_raise(db, job_id)
    if hasattr(job, "output_file_id"):
        setattr(job, "output_file_id", output_file_id)
    if hasattr(job, "preview_file_id"):
        setattr(job, "preview_file_id", preview_file_id)
    if hasattr(job, "subtitle_file_id"):
        setattr(job, "subtitle_file_id", subtitle_file_id)
    with suppress(Exception):
        setattr(job, "updated_at", _now_utc())
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Progress publication helpers
# ---------------------------------------------------------------------------


def _build_progress_payload(
    job_id: str,
    status: str,
    current_stage: str,
    progress_percent: int,
    message: str,
) -> dict[str, Any]:
    """Build the canonical progress event payload."""
    return {
        "job_id": str(job_id),
        "status": status,
        "current_stage": current_stage,
        "progress_percent": int(progress_percent),
        "message": message,
        "timestamp": _now_utc().isoformat(),
    }


def _publish_progress_payload(payload: dict[str, Any]) -> None:
    """Publish payload via progress service when available."""
    publish_progress = _import_optional_attribute("app.services.progress_service", "publish_progress")
    if callable(publish_progress):
        try:
            publish_progress(str(payload["job_id"]), payload)
        except Exception:
            logger.warning(
                "progress_publish_failed",
                job_id=payload.get("job_id"),
                stage=payload.get("current_stage"),
            )


def publish_stage(
    job_id: str,
    status: str,
    current_stage: str,
    progress_percent: int,
    message: str,
) -> None:
    """Persist and publish job progress for a specific stage.

    This function is intentionally small and synchronous because it is called
    frequently by the worker pipeline. It updates the authoritative job state in
    the database and then broadcasts the progress payload through Redis pub/sub
    when the progress service exists.
    """
    payload = _build_progress_payload(
        job_id=job_id,
        status=status,
        current_stage=current_stage,
        progress_percent=progress_percent,
        message=message,
    )

    with _db_session_scope() as db:
        _update_job_status(
            db,
            job_id,
            status=status,
            current_stage=current_stage,
            progress_percent=progress_percent,
        )

    _publish_progress_payload(payload)

    logger.info(
        "pipeline_stage_published",
        job_id=job_id,
        stage=current_stage,
        status=status,
        progress_percent=progress_percent,
        message=message,
    )


def assert_not_cancelled(job_id: str) -> None:
    """Raise JOB_CANCELLED if the target job has been cancelled.

    The pipeline calls this between major stages so that cancellation remains
    responsive without needing process-level interruption.
    """
    with _db_session_scope() as db:
        job = _get_job_or_raise(db, job_id)
        if str(getattr(job, "status", "")).lower() == "cancelled":
            logger.info(
                "pipeline_cancel_detected",
                job_id=job_id,
                stage="cancel_check",
            )
            raise RuntimeError("JOB_CANCELLED")


# ---------------------------------------------------------------------------
# Storage and file helpers
# ---------------------------------------------------------------------------


def _get_storage_service(db: Any) -> Any:
    """Resolve storage service instance from the project."""
    factory = _import_optional_attribute("app.services.storage_service", "get_storage_service")
    if callable(factory):
        return factory(db)

    settings = get_settings()
    storage_module = _import_module("app.services.storage_service")
    if str(getattr(settings, "storage_mode", "local")).lower() == "s3":
        storage_cls = getattr(storage_module, "S3StorageService")
    else:
        storage_cls = getattr(storage_module, "LocalStorageService")
    return storage_cls(db)


def _safe_mkdir(path: Path) -> None:
    """Create directory recursively."""
    path.mkdir(parents=True, exist_ok=True)


def _copy_file(source: str | Path, destination: str | Path) -> str:
    """Copy file to a new location and return destination path."""
    source_path = Path(source)
    destination_path = Path(destination)
    _safe_mkdir(destination_path.parent)
    shutil.copy2(source_path, destination_path)
    return str(destination_path)


def _resolve_media_public_name(job_id: str, suffix: str) -> str:
    """Create stable user-facing filenames for generated artifacts."""
    return f"{job_id}_{suffix}"


def _create_working_dir(job_id: str) -> Path:
    """Create the per-job working directory under configured temp root."""
    settings = get_settings()
    base_dir = Path(settings.temp_dir).expanduser().resolve()
    working_dir = base_dir / f"job_{job_id}"
    _safe_mkdir(working_dir)
    return working_dir


def _cleanup_working_dir(working_dir: str) -> None:
    """Clean temporary working directory using service helper or local fallback."""
    cleanup_working_dir = _import_optional_attribute(
        "app.services.file_cleanup_service",
        "cleanup_working_dir",
    )
    if callable(cleanup_working_dir):
        with suppress(Exception):
            cleanup_working_dir(working_dir)
            return

    path = Path(working_dir)
    with suppress(Exception):
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def _probe_media(file_path: str) -> dict[str, Any]:
    """Best-effort media probe using the project's utility module."""
    probe_media = _import_optional_attribute("app.utils.media", "probe_media")
    if callable(probe_media):
        return probe_media(file_path)
    return {}


def _should_generate_subtitles(preset_name: str, settings: dict[str, Any]) -> bool:
    """Determine whether subtitle generation is enabled."""
    explicit_value = settings.get("subtitles_enabled")
    if isinstance(explicit_value, bool):
        return explicit_value

    subtitles_section = settings.get("subtitles")
    if isinstance(subtitles_section, dict):
        nested_enabled = subtitles_section.get("enabled")
        if isinstance(nested_enabled, bool):
            return nested_enabled

    return preset_name == "tutorial"


def _resolve_codec(settings: dict[str, Any]) -> str:
    """Resolve target codec with safe fallback."""
    codec = str(settings.get("codec") or "h264").lower()
    return codec if codec in {"h264", "h265"} else "h264"


def _resolve_target_fps(preset_name: str, settings: dict[str, Any], analysis: dict[str, Any] | None) -> int | None:
    """Resolve target FPS for rendering and interpolation."""
    if isinstance(settings.get("target_fps"), int):
        return int(settings["target_fps"])

    if preset_name == "gaming":
        return 120
    if preset_name == "tutorial":
        return 60
    if preset_name == "cinematic":
        return 24

    with suppress(Exception):
        fps_value = analysis.get("fps") if analysis else None
        if fps_value is not None:
            return int(round(float(fps_value)))

    return None


# ---------------------------------------------------------------------------
# Stage wrappers
# ---------------------------------------------------------------------------


def _run_analyze_stage(context: PipelineContext) -> dict[str, Any]:
    """Execute the analyze stage with fallback probing."""
    analyze_video = _import_optional_attribute("app.workers.stages.analyzer", "analyze_video")
    if callable(analyze_video):
        return analyze_video(context.job_id, context.input_path, context.settings)

    metadata = _probe_media(context.input_path)
    duration = metadata.get("duration_seconds", metadata.get("duration"))
    fps = metadata.get("fps")
    width = metadata.get("width")
    height = metadata.get("height")
    bitrate = metadata.get("bitrate")
    video_codec = metadata.get("codec_name")
    audio_codec = metadata.get("audio_codec_name")

    return {
        "fps": float(fps) if fps is not None else 0.0,
        "width": int(width) if width is not None else 0,
        "height": int(height) if height is not None else 0,
        "duration_seconds": float(duration) if duration is not None else 0.0,
        "bitrate": int(bitrate) if bitrate is not None else 0,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "silence_segments": [],
        "scene_changes": [],
        "audio_peak_db": 0.0,
        "audio_rms_db": 0.0,
        "estimated_noise_floor_db": 0.0,
        "audio_clipping_ratio": 0.0,
        "dead_segments": [],
    }


def _run_cutting_stage(context: PipelineContext, output_path: str) -> str:
    """Execute cutting stage using preset-specific logic with copy fallback."""
    apply_highlight_cut = _import_optional_attribute("app.workers.stages.cutter", "apply_highlight_cut")
    apply_jump_cut = _import_optional_attribute("app.workers.stages.cutter", "apply_jump_cut")

    if context.preset_name == "gaming" and callable(apply_highlight_cut):
        return apply_highlight_cut(
            context.intermediate_video_path,
            output_path,
            context.analysis or {},
            context.settings,
            wav_path=None,
        )

    if callable(apply_jump_cut):
        return apply_jump_cut(
            context.intermediate_video_path,
            output_path,
            context.analysis or {},
            context.settings,
        )

    return _copy_file(context.intermediate_video_path, output_path)


def _run_enhancing_stage(context: PipelineContext, output_path: str) -> str:
    """Execute enhancement stage with safe passthrough fallback."""
    enhance_video = _import_optional_attribute("app.workers.stages.enhancer", "enhance_video")
    if callable(enhance_video):
        return enhance_video(
            context.intermediate_video_path,
            output_path,
            context.preset_name,
            context.analysis or {},
            context.settings,
        )

    return _copy_file(context.intermediate_video_path, output_path)


def _run_interpolating_stage(context: PipelineContext, output_path: str) -> str:
    """Execute interpolation stage with safe passthrough fallback."""
    interpolate_video = _import_optional_attribute("app.workers.stages.interpolator", "interpolate_video")
    if callable(interpolate_video):
        return interpolate_video(
            context.intermediate_video_path,
            output_path,
            context.preset_name,
            context.settings,
            context.analysis or {},
        )

    return _copy_file(context.intermediate_video_path, output_path)


def _run_audio_stage(context: PipelineContext, output_path: str) -> str | None:
    """Execute audio processing stage.

    Returns:
        Processed audio path or None when the input has no audio or the stage is
        intentionally skipped.
    """
    process_audio = _import_optional_attribute("app.workers.stages.audio_processor", "process_audio")
    if callable(process_audio):
        return process_audio(
            context.intermediate_video_path,
            output_path,
            context.preset_name,
            context.settings,
        )

    analysis = context.analysis or {}
    if not analysis.get("audio_codec"):
        return None

    # Without a dedicated stage implementation we skip standalone audio output.
    return None


def _run_subtitle_stage(context: PipelineContext, output_path: str) -> str:
    """Execute subtitle generation stage."""
    generate_subtitles = _import_optional_attribute("app.workers.stages.subtitler", "generate_subtitles")
    whisper_model = str(context.settings.get("whisper_model") or get_settings().whisper_model)
    audio_input_path = context.processed_audio_path

    if audio_input_path is None:
        # Try to reuse extracted/processed audio if no audio stage artifact exists.
        audio_input_path = str(Path(context.working_dir) / "subtitle_input.wav")
        extract_audio_to_wav = _import_optional_attribute("app.utils.ffmpeg", "extract_audio_to_wav")
        if callable(extract_audio_to_wav):
            audio_input_path = extract_audio_to_wav(context.intermediate_video_path, audio_input_path)
        else:
            raise RuntimeError("WHISPER_MODEL_MISSING")

    if callable(generate_subtitles):
        return generate_subtitles(audio_input_path, output_path, whisper_model)

    raise RuntimeError("WHISPER_MODEL_MISSING")


def _run_render_stage(context: PipelineContext) -> str:
    """Execute final render stage with fallback copy behavior."""
    render_pipeline_output = _import_optional_attribute(
        "app.workers.stages.renderer",
        "render_pipeline_output",
    )
    if callable(render_pipeline_output):
        return render_pipeline_output(
            context.intermediate_video_path,
            context.processed_audio_path,
            context.subtitle_path,
            context.final_output_path,
            context.settings,
            context.analysis or {},
            context.preset_name,
        )

    return _copy_file(context.intermediate_video_path, context.final_output_path)


def _run_preview_stage(context: PipelineContext) -> dict[str, str]:
    """Generate preview assets with renderer helper or ffmpeg-like fallback."""
    generate_preview_assets = _import_optional_attribute(
        "app.workers.stages.renderer",
        "generate_preview_assets",
    )
    settings = get_settings()
    preview_dir = Path(settings.preview_dir).expanduser().resolve() / context.job_id
    _safe_mkdir(preview_dir)

    if callable(generate_preview_assets):
        return generate_preview_assets(context.input_path, context.final_output_path, str(preview_dir))

    before_path = preview_dir / "before.mp4"
    after_path = preview_dir / "after.mp4"
    thumb_path = preview_dir / "thumbnail.jpg"

    create_preview_clip = _import_optional_attribute("app.utils.ffmpeg", "create_preview_clip")
    extract_thumbnail = _import_optional_attribute("app.utils.ffmpeg", "extract_thumbnail")

    if callable(create_preview_clip):
        create_preview_clip(context.input_path, str(before_path))
        create_preview_clip(context.final_output_path, str(after_path))
    else:
        _copy_file(context.input_path, before_path)
        _copy_file(context.final_output_path, after_path)

    if callable(extract_thumbnail):
        extract_thumbnail(context.final_output_path, str(thumb_path))
    else:
        # No image tool available: do not fail the whole job just because the
        # thumbnail could not be created.
        pass

    assets: dict[str, str] = {
        "before": str(before_path),
        "after": str(after_path),
    }
    if thumb_path.exists():
        assets["thumbnail"] = str(thumb_path)
    return assets


# ---------------------------------------------------------------------------
# Artifact persistence helpers
# ---------------------------------------------------------------------------


def _store_output_artifacts(
    db: Any,
    context: PipelineContext,
) -> tuple[Any | None, Any | None, Any | None]:
    """Persist generated media through the storage service.

    Returns:
        Tuple of (output_file_id, preview_file_id, subtitle_file_id).
    """
    storage = _get_storage_service(db)

    output_media = storage.save_local_file(
        context.final_output_path,
        "output",
        _resolve_media_public_name(context.job_id, "final.mp4"),
        "video/mp4",
    )

    subtitle_media = None
    if context.subtitle_path:
        subtitle_media = storage.save_local_file(
            context.subtitle_path,
            "subtitle",
            _resolve_media_public_name(context.job_id, "subtitles.srt"),
            "application/x-subrip",
        )

    preview_file_id = None

    create_job_previews = _import_optional_attribute("app.services.preview_service", "create_job_previews")
    if callable(create_job_previews):
        with suppress(Exception):
            preview_ids = create_job_previews(
                context.job_id,
                context.input_path,
                context.final_output_path,
            )
            if isinstance(preview_ids, dict):
                preview_file_id = (
                    preview_ids.get("after_file_id")
                    or preview_ids.get("before_file_id")
                    or preview_ids.get("thumbnail_file_id")
                )

    if preview_file_id is None and context.preview_assets:
        # Fallback path: store preview artifacts directly through storage service.
        after_path = context.preview_assets.get("after")
        before_path = context.preview_assets.get("before")
        thumbnail_path = context.preview_assets.get("thumbnail")

        with suppress(Exception):
            if before_path:
                storage.save_local_file(
                    before_path,
                    "preview_before",
                    _resolve_media_public_name(context.job_id, "before.mp4"),
                    "video/mp4",
                )

        with suppress(Exception):
            if thumbnail_path:
                storage.save_local_file(
                    thumbnail_path,
                    "thumbnail",
                    _resolve_media_public_name(context.job_id, "thumbnail.jpg"),
                    "image/jpeg",
                )

        if after_path:
            after_media = storage.save_local_file(
                after_path,
                "preview_after",
                _resolve_media_public_name(context.job_id, "after.mp4"),
                "video/mp4",
            )
            preview_file_id = getattr(after_media, "id", None)

    return (
        getattr(output_media, "id", None),
        preview_file_id,
        getattr(subtitle_media, "id", None) if subtitle_media is not None else None,
    )


# ---------------------------------------------------------------------------
# Context construction helpers
# ---------------------------------------------------------------------------


def _resolve_job_settings(job: Any) -> dict[str, Any]:
    """Extract runtime settings from job row."""
    settings_json = getattr(job, "settings_json", None)
    if isinstance(settings_json, dict):
        return dict(settings_json)
    return {}


def _resolve_job_preset(job: Any) -> str:
    """Extract preset name with a safe default."""
    preset_name = str(getattr(job, "preset_name", "") or "").strip().lower()
    return preset_name or "gaming"


def _resolve_input_file_id(job: Any) -> Any:
    """Extract the uploaded input file id from a job row."""
    input_file_id = getattr(job, "input_file_id", None)
    if input_file_id is None:
        raise RuntimeError("FILE_NOT_FOUND")
    return input_file_id


def _resolve_input_path(db: Any, job: Any) -> str:
    """Resolve a local path to the uploaded source media using storage service."""
    storage = _get_storage_service(db)
    input_file_id = _resolve_input_file_id(job)

    with suppress(Exception):
        open_result = storage.open_file(input_file_id)
        if isinstance(open_result, tuple) and len(open_result) >= 1:
            return str(open_result[0])

    with suppress(Exception):
        return str(storage.get_path(input_file_id))

    raise RuntimeError("FILE_NOT_FOUND")


def _build_pipeline_context(db: Any, job: Any) -> PipelineContext:
    """Construct initial pipeline context for a job."""
    settings = get_settings()
    job_id = str(getattr(job, "id"))
    preset_name = _resolve_job_preset(job)
    input_path = _resolve_input_path(db, job)
    working_dir = _create_working_dir(job_id)

    final_name = f"{job_id}_final.mp4"
    final_output_path = Path(settings.output_dir).expanduser().resolve() / final_name
    _safe_mkdir(final_output_path.parent)

    initial_video_path = Path(working_dir) / "00_input.mp4"
    if Path(input_path).resolve() != initial_video_path.resolve():
        _copy_file(input_path, initial_video_path)
    else:
        initial_video_path = Path(input_path)

    return PipelineContext(
        job_id=job_id,
        preset_name=preset_name,
        input_path=input_path,
        working_dir=str(working_dir),
        analysis_path=None,
        analysis=None,
        settings=_resolve_job_settings(job),
        intermediate_video_path=str(initial_video_path),
        processed_audio_path=None,
        subtitle_path=None,
        final_output_path=str(final_output_path),
        preview_assets=None,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def process_job_pipeline(job_id: str) -> None:
    """Execute the full AutoEdit media processing pipeline for one job.

    High-level stage progression:
    1. analyzing
    2. cutting
    3. enhancing
    4. interpolating
    5. processing_audio
    6. generating_subtitles (optional)
    7. rendering
    8. generating_preview
    9. completed

    On failure:
    - mark the job as failed;
    - store error_code and error_message;
    - publish failure progress event;
    - clean working directory in ``finally``.

    On cancellation:
    - stop between stages;
    - mark the job as cancelled;
    - publish cancellation event;
    - clean working directory in ``finally``.
    """
    settings = get_settings()
    bound_logger = logger.bind(job_id=job_id, stage="pipeline")

    context: PipelineContext | None = None

    bound_logger.info("pipeline_started")

    try:
        with _db_session_scope() as db:
            job = _get_job_or_raise(db, job_id)

            if getattr(job, "started_at", None) is None:
                with suppress(Exception):
                    setattr(job, "started_at", _now_utc())
                with suppress(Exception):
                    db.add(job)
                    db.commit()
                    db.refresh(job)

            context = _build_pipeline_context(db, job)

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 1: analyzing
        # ------------------------------------------------------------------
        publish_stage(job_id, "analyzing", "analyzing", 5, "Начат анализ исходного видео")
        bound_logger.info("pipeline_stage_start", stage="analyzing")

        analysis = _run_analyze_stage(context)
        context.analysis = analysis

        analysis_path = Path(context.working_dir) / "analysis.json"
        with suppress(Exception):
            analysis_path.write_text(
                importlib.import_module("json").dumps(analysis, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            context.analysis_path = str(analysis_path)

        with _db_session_scope() as db:
            _attach_analysis(db, job_id, analysis)

        publish_stage(job_id, "analyzing", "analyzing", 20, "Анализ видео завершён")
        bound_logger.info("pipeline_stage_finish", stage="analyzing")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 2: cutting
        # ------------------------------------------------------------------
        cutting_output = str(Path(context.working_dir) / "10_cut.mp4")
        publish_stage(job_id, "cutting", "cutting", 20, "Выполняется умная нарезка")
        bound_logger.info("pipeline_stage_start", stage="cutting")

        context.intermediate_video_path = _run_cutting_stage(context, cutting_output)

        publish_stage(job_id, "cutting", "cutting", 35, "Нарезка завершена")
        bound_logger.info("pipeline_stage_finish", stage="cutting")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 3: enhancing
        # ------------------------------------------------------------------
        enhance_output = str(Path(context.working_dir) / "20_enhanced.mp4")
        publish_stage(job_id, "enhancing", "enhancing", 35, "Применяются улучшения видео")
        bound_logger.info("pipeline_stage_start", stage="enhancing")

        context.intermediate_video_path = _run_enhancing_stage(context, enhance_output)

        publish_stage(job_id, "enhancing", "enhancing", 55, "Улучшение качества завершено")
        bound_logger.info("pipeline_stage_finish", stage="enhancing")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 4: interpolating
        # ------------------------------------------------------------------
        interpolate_output = str(Path(context.working_dir) / "30_interpolated.mp4")
        publish_stage(job_id, "interpolating", "interpolating", 55, "Нормализация и интерполяция FPS")
        bound_logger.info("pipeline_stage_start", stage="interpolating")

        context.intermediate_video_path = _run_interpolating_stage(context, interpolate_output)

        publish_stage(job_id, "interpolating", "interpolating", 70, "Этап FPS завершён")
        bound_logger.info("pipeline_stage_finish", stage="interpolating")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 5: audio processing
        # ------------------------------------------------------------------
        audio_output = str(Path(context.working_dir) / "40_audio.wav")
        publish_stage(job_id, "processing_audio", "processing_audio", 70, "Выполняется обработка аудио")
        bound_logger.info("pipeline_stage_start", stage="processing_audio")

        context.processed_audio_path = _run_audio_stage(context, audio_output)

        publish_stage(job_id, "processing_audio", "processing_audio", 82, "Аудиообработка завершена")
        bound_logger.info("pipeline_stage_finish", stage="processing_audio")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 6: subtitles (optional)
        # ------------------------------------------------------------------
        if _should_generate_subtitles(context.preset_name, context.settings):
            subtitle_output = str(Path(context.working_dir) / "50_subtitles.srt")
            publish_stage(
                job_id,
                "generating_subtitles",
                "generating_subtitles",
                82,
                "Генерируются локальные субтитры",
            )
            bound_logger.info("pipeline_stage_start", stage="generating_subtitles")

            context.subtitle_path = _run_subtitle_stage(context, subtitle_output)

            publish_stage(
                job_id,
                "generating_subtitles",
                "generating_subtitles",
                88,
                "Субтитры успешно созданы",
            )
            bound_logger.info("pipeline_stage_finish", stage="generating_subtitles")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 7: rendering
        # ------------------------------------------------------------------
        publish_stage(job_id, "rendering", "rendering", 88, "Запущен финальный рендер")
        bound_logger.info("pipeline_stage_start", stage="rendering")

        context.final_output_path = _run_render_stage(context)

        if not Path(context.final_output_path).exists():
            raise RuntimeError("FILE_NOT_FOUND")

        publish_stage(job_id, "rendering", "rendering", 96, "Финальный рендер завершён")
        bound_logger.info("pipeline_stage_finish", stage="rendering")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Stage 8: preview generation
        # ------------------------------------------------------------------
        publish_stage(
            job_id,
            "generating_preview",
            "generating_preview",
            96,
            "Подготавливаются preview-артефакты",
        )
        bound_logger.info("pipeline_stage_start", stage="generating_preview")

        context.preview_assets = _run_preview_stage(context)

        publish_stage(
            job_id,
            "generating_preview",
            "generating_preview",
            99,
            "Preview-артефакты подготовлены",
        )
        bound_logger.info("pipeline_stage_finish", stage="generating_preview")

        assert_not_cancelled(job_id)

        # ------------------------------------------------------------------
        # Persist artifacts and mark completed
        # ------------------------------------------------------------------
        with _db_session_scope() as db:
            output_file_id, preview_file_id, subtitle_file_id = _store_output_artifacts(db, context)

            _attach_result_files(
                db,
                job_id,
                output_file_id=output_file_id,
                preview_file_id=preview_file_id,
                subtitle_file_id=subtitle_file_id,
            )

            job = _get_job_or_raise(db, job_id)
            with suppress(Exception):
                setattr(job, "completed_at", _now_utc())
            with suppress(Exception):
                setattr(job, "updated_at", _now_utc())
            db.add(job)
            db.commit()
            db.refresh(job)

        publish_stage(job_id, "completed", "completed", 100, "Обработка видео успешно завершена")
        bound_logger.info(
            "pipeline_completed",
            stage="completed",
            final_output_path=context.final_output_path,
            target_fps=_resolve_target_fps(context.preset_name, context.settings, context.analysis),
            codec=_resolve_codec(context.settings),
        )

    except Exception as exc:
        error_code = _extract_error_code(exc)
        error_message = _serialize_exception_message(exc)

        if error_code == "JOB_CANCELLED":
            with suppress(Exception):
                with _db_session_scope() as db:
                    _update_job_status(
                        db,
                        job_id,
                        status="cancelled",
                        current_stage="cancelled",
                        progress_percent=0,
                        error_code="JOB_CANCELLED",
                        error_message="Job was cancelled by user request.",
                    )
            _publish_progress_payload(
                _build_progress_payload(
                    job_id=job_id,
                    status="cancelled",
                    current_stage="cancelled",
                    progress_percent=0,
                    message="Задача отменена",
                )
            )
            bound_logger.warning("pipeline_cancelled", stage="cancelled")
            return

        bound_logger.error(
            "pipeline_failed",
            stage="failed",
            error_code=error_code,
            error_message=error_message,
            traceback=traceback.format_exc(),
        )

        with suppress(Exception):
            with _db_session_scope() as db:
                _update_job_status(
                    db,
                    job_id,
                    status="failed",
                    current_stage="failed",
                    progress_percent=100 if error_code == "INTERNAL_SERVER_ERROR" else 0,
                    error_code=error_code,
                    error_message=error_message,
                )

        _publish_progress_payload(
            _build_progress_payload(
                job_id=job_id,
                status="failed",
                current_stage="failed",
                progress_percent=0,
                message=error_message,
            )
        )

        raise

    finally:
        if context is not None:
            _cleanup_working_dir(context.working_dir)

        # Optional retention/cleanup hook may exist in later project stages.
        cleanup_job_files = _import_optional_attribute(
            "app.services.file_cleanup_service",
            "cleanup_job_files",
        )
        if callable(cleanup_job_files) and context is not None:
            with suppress(Exception):
                # Keep final outputs, remove only temp artifacts.
                cleanup_job_files(context.job_id, delete_outputs=False)

        logger.info(
            "pipeline_finalized",
            job_id=job_id,
            stage="finalize",
            temp_dir=context.working_dir if context is not None else None,
            app_env=settings.app_env,
        )


__all__ = [
    "PipelineContext",
    "process_job_pipeline",
    "publish_stage",
    "assert_not_cancelled",
]