"""Unified API router tree for the current AutoEdit backend state.

This project snapshot currently contains a simplified backend layout where:
- schemas live in a single module: ``app.schemas``;
- ORM models live in a single module: ``app.db.models``;
- service-layer modules from the long-term project plan are not yet present.

Because of that, this file provides a fully working, self-contained router that:
1. exposes the REST and WebSocket endpoints already expected by the frontend;
2. keeps function names and response contracts aligned with the specification;
3. uses safe local filesystem storage for uploads in the current generation step;
4. uses an in-memory registry for jobs/media metadata as a temporary fallback;
5. remains easy to replace later with dedicated services and database-backed logic.

Implemented endpoints:
- GET    /api/v1/health
- GET    /api/v1/presets
- POST   /api/v1/uploads
- POST   /api/v1/jobs
- GET    /api/v1/jobs/{job_id}
- GET    /api/v1/jobs/{job_id}/preview
- DELETE /api/v1/jobs/{job_id}
- GET    /api/v1/jobs/{job_id}/download
- GET    /api/v1/results/media/{file_id}
- WS     /api/v1/jobs/{job_id}/events

Important notes:
- The current implementation intentionally avoids Unix-only modules.
- All filesystem work uses ``pathlib.Path``.
- The module is Windows-friendly.
- The current job-processing implementation is a stub/fallback. Jobs are created
  in ``queued`` state and can be observed via REST/WebSocket, but no Celery
  worker pipeline is started yet because those files are not present in the
  current project snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import JobStatus
from app.schemas import (
    JobCreateRequest,
    JobResponse,
    PresetItem,
    PresetListResponse,
    UploadResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api"])


# ---------------------------------------------------------------------------
# In-memory fallback stores
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MediaRecord:
    """Small in-memory representation of a stored media file.

    This is a temporary runtime fallback used until the dedicated DB/session and
    storage service modules are generated. It intentionally mirrors the fields
    that the frontend and routes currently need.
    """

    file_id: UUID
    file_role: str
    storage_path: str
    public_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


_MEDIA_STORE: dict[UUID, MediaRecord] = {}
_JOB_STORE: dict[UUID, dict[str, Any]] = {}
_JOB_EVENT_HISTORY: dict[UUID, list[dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
}

_ALLOWED_PRESETS = {"gaming", "tutorial", "cinematic"}
_ALLOWED_TARGET_FPS = {24, 30, 60, 120}
_ALLOWED_ASPECT_RATIOS = {"16:9", "21:9", "9:16"}
_ALLOWED_CODECS = {"h264", "h265"}


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    """Return a current UTC timestamp in ISO format."""
    return _utc_now().isoformat()


def _json_safe(value: Any) -> Any:
    """Convert non-JSON-native values into JSON-friendly data.

    This helper is intentionally small and conservative.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _raise_http(status_code: int, error_code: str, message: str) -> None:
    """Raise a project-style HTTP exception."""
    raise HTTPException(
        status_code=status_code,
        detail={
            "error_code": error_code,
            "message": message,
        },
    )


def _sanitize_filename(filename: str) -> str:
    """Return a filesystem-safe filename.

    Rules:
    - remove directory traversal components;
    - replace unsafe characters with underscores;
    - ensure a non-empty result;
    - preserve extension if present.
    """
    raw_name = Path(filename or "").name.strip()
    if not raw_name:
        raw_name = "upload.bin"

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
    if not safe_name:
        safe_name = "upload.bin"

    return safe_name[:255]


def _parse_allowed_extensions() -> list[str]:
    """Parse allowed extensions from settings.

    Returns normalized lowercase values like ``.mp4``.
    """
    settings = get_settings()
    raw = settings.allowed_video_extensions or ".mp4,.mov,.avi,.mkv"
    items = [item.strip().lower() for item in raw.split(",") if item.strip()]
    return [item if item.startswith(".") else f".{item}" for item in items]


def _validate_preset_name(preset_name: str) -> None:
    """Validate preset name against the allowed built-in set."""
    if preset_name not in _ALLOWED_PRESETS:
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_PRESET",
            "Недопустимое имя пресета.",
        )


def _validate_job_settings(settings_payload: Any) -> None:
    """Validate user-adjustable job settings.

    The request schema already performs most validation, but this helper keeps
    a stable guard layer close to the API contract.
    """
    if settings_payload is None:
        return

    data = settings_payload.model_dump() if hasattr(settings_payload, "model_dump") else dict(settings_payload)

    target_fps = data.get("target_fps")
    if target_fps is not None and target_fps not in _ALLOWED_TARGET_FPS:
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_SETTINGS",
            "target_fps должен быть одним из: 24, 30, 60, 120.",
        )

    zoom_scale = data.get("zoom_scale")
    if zoom_scale is not None and not (1.0 <= float(zoom_scale) <= 2.0):
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_SETTINGS",
            "zoom_scale должен быть в диапазоне от 1.0 до 2.0.",
        )

    cut_aggressiveness = data.get("cut_aggressiveness")
    if cut_aggressiveness is not None and not (0.0 <= float(cut_aggressiveness) <= 1.0):
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_SETTINGS",
            "cut_aggressiveness должен быть в диапазоне от 0.0 до 1.0.",
        )

    output_aspect_ratio = data.get("output_aspect_ratio")
    if output_aspect_ratio is not None and output_aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_SETTINGS",
            "output_aspect_ratio должен быть одним из: 16:9, 21:9, 9:16.",
        )

    codec = data.get("codec")
    if codec is not None and codec not in _ALLOWED_CODECS:
        _raise_http(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_SETTINGS",
            "codec должен быть одним из: h264 или h265.",
        )


def _validate_upload(filename: str, mime_type: str, size_bytes: int) -> None:
    """Validate uploaded file by extension, MIME type and size."""
    settings = get_settings()

    if size_bytes <= 0:
        _raise_http(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_FILE_SIZE",
            "Пустой файл запрещён.",
        )

    if size_bytes > int(settings.upload_max_size_bytes):
        _raise_http(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "INVALID_FILE_SIZE",
            "Файл превышает допустимый размер.",
        )

    extension = Path(filename).suffix.lower()
    if extension not in _parse_allowed_extensions():
        _raise_http(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_FILE_EXTENSION",
            "Неподдерживаемое расширение файла.",
        )

    if mime_type not in _ALLOWED_MIME_TYPES:
        _raise_http(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_MIME_TYPE",
            "Неподдерживаемый MIME type файла.",
        )


def _sha256_of_path(file_path: Path) -> str:
    """Compute SHA-256 hash of a file using streaming I/O."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_dir(*parts: str) -> Path:
    """Build a path under the configured temporary root."""
    settings = get_settings()
    base = Path(settings.temp_dir)
    return base.joinpath(*parts)


def _uploads_dir() -> Path:
    """Return the upload directory, creating it if missing."""
    path = _runtime_dir("uploads")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _output_dir() -> Path:
    """Return the output directory, creating it if missing."""
    settings = get_settings()
    path = Path(settings.output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _preview_dir() -> Path:
    """Return the preview directory, creating it if missing."""
    settings = get_settings()
    path = Path(settings.preview_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _guess_mime_type(filename: str, upload_content_type: str | None) -> str:
    """Resolve a stable MIME type for uploaded files."""
    if upload_content_type and upload_content_type.strip():
        return upload_content_type.strip().lower()

    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or "application/octet-stream").lower()


def _load_preset_from_disk(preset_name: str) -> dict[str, Any]:
    """Load a preset JSON file from the configured preset directory.

    If the file does not exist, fall back to a built-in minimal preset payload.
    """
    settings = get_settings()
    preset_path = Path(settings.preset_dir) / f"{preset_name}.json"

    if preset_path.exists() and preset_path.is_file():
        with preset_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    logger.warning(
        "preset_file_missing_fallback_used",
        preset_name=preset_name,
        preset_path=str(preset_path),
    )
    return _builtin_preset_map()[preset_name]


def _builtin_preset_map() -> dict[str, dict[str, Any]]:
    """Return built-in fallback preset definitions.

    These values are intentionally aligned with the specification and guarantee
    that the frontend can work even before preset files are generated.
    """
    return {
        "gaming": {
            "name": "gaming",
            "display_name": "Gaming / Highlight",
            "target_fps": 120,
            "interpolation_engine": "rife",
            "enable_motion_blur": True,
            "cutting": {
                "remove_silence": True,
                "remove_dead_segments": True,
                "aggressiveness": 0.7,
            },
            "audio": {
                "noise_reduction": True,
                "target_lufs": -14,
            },
            "transitions": {
                "type": "whip",
                "duration_ms": 180,
            },
        },
        "tutorial": {
            "name": "tutorial",
            "display_name": "Tutorial / Обучение",
            "target_fps": 60,
            "interpolation_engine": "minterpolate",
            "remove_fillers": True,
            "subtitles": {
                "enabled": True,
                "model": "small",
                "format": "srt",
            },
            "cutting": {
                "remove_silence": True,
                "aggressiveness": 0.85,
            },
        },
        "cinematic": {
            "name": "cinematic",
            "display_name": "Cinematic / Контент",
            "target_fps": 24,
            "interpolation_engine": "none",
            "color": {
                "lut_name": "teal_orange",
                "contrast": 1.08,
                "saturation": 1.12,
            },
            "transitions": {
                "type": "crossfade",
                "duration_ms": 300,
            },
        },
    }


def _list_presets_payload() -> list[dict[str, Any]]:
    """Return all preset payloads."""
    return [_load_preset_from_disk(name) for name in ("gaming", "tutorial", "cinematic")]


def _record_job_event(
    job_id: UUID,
    *,
    status_value: str,
    current_stage: str,
    progress_percent: int,
    message: str,
) -> dict[str, Any]:
    """Store a job event in in-memory history and return it."""
    payload = {
        "job_id": str(job_id),
        "status": status_value,
        "current_stage": current_stage,
        "progress_percent": progress_percent,
        "message": message,
        "timestamp": _iso_now(),
    }
    _JOB_EVENT_HISTORY.setdefault(job_id, []).append(payload)
    return payload


def _build_job_response_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Convert an internal job dict to the API response payload."""
    error_payload: dict[str, str] | None = None
    if job.get("error_code") or job.get("error_message"):
        error_payload = {
            "error_code": job.get("error_code") or "INTERNAL_SERVER_ERROR",
            "message": job.get("error_message") or "Unknown job error.",
        }

    result_payload = job.get("result")
    analysis_payload = job.get("analysis")

    return {
        "job_id": str(job["job_id"]),
        "status": job["status"],
        "current_stage": job["current_stage"],
        "progress_percent": int(job["progress_percent"]),
        "preset_name": job["preset_name"],
        "analysis": analysis_payload,
        "result": result_payload,
        "error": error_payload,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "original_filename": job.get("original_filename"),
    }


def _get_job_or_404(job_id: UUID) -> dict[str, Any]:
    """Return a job from the in-memory store or raise 404."""
    job = _JOB_STORE.get(job_id)
    if not job:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "JOB_NOT_FOUND",
            "Задача не найдена.",
        )
    return job


def _get_media_or_404(file_id: UUID) -> MediaRecord:
    """Return a media record or raise 404."""
    media = _MEDIA_STORE.get(file_id)
    if not media:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Файл не найден.",
        )
    return media


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.get("/health")
async def healthcheck() -> dict[str, str]:
    """Return a minimal health response.

    This endpoint intentionally matches the specification exactly.
    """
    settings = get_settings()
    logger.info("healthcheck_called")
    return {
        "status": "ok",
        "app_name": settings.app_name,
    }


@router.get("/presets", response_model=PresetListResponse)
async def get_presets() -> PresetListResponse:
    """Return the list of built-in presets.

    The response structure is aligned with the frontend ``fetchPresets`` helper.
    """
    items: list[PresetItem] = []
    for payload in _list_presets_payload():
        items.append(
            PresetItem(
                name=payload["name"],
                display_name=payload["display_name"],
                default_settings=payload,
            )
        )

    logger.info("presets_listed", count=len(items))
    return PresetListResponse(items=items)


@router.post("/uploads", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_video(file: UploadFile = File(...)) -> UploadResponse:
    """Save an uploaded video file locally and return its metadata.

    Current behavior:
    - validates extension, size and MIME type;
    - stores the upload under ``TEMP_DIR/uploads``;
    - records the file in an in-memory registry;
    - returns metadata expected by the frontend.

    This route intentionally avoids loading huge files into memory in one read.
    """
    settings = get_settings()

    safe_name = _sanitize_filename(file.filename or "upload.bin")
    mime_type = _guess_mime_type(safe_name, file.content_type)

    target_file_id = uuid4()
    target_path = _uploads_dir() / f"{target_file_id}_{safe_name}"

    logger.info(
        "upload_started",
        filename=safe_name,
        mime_type=mime_type,
        target_path=str(target_path),
    )

    size_bytes = 0
    try:
        with target_path.open("wb") as output_handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                output_handle.write(chunk)
                size_bytes += len(chunk)
    except PermissionError as exc:
        logger.exception("upload_permission_error", filename=safe_name, error=str(exc))
        _raise_http(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "STORAGE_PERMISSION_ERROR",
            "Недостаточно прав для сохранения файла.",
        )
    except OSError as exc:
        logger.exception("upload_save_error", filename=safe_name, error=str(exc))
        _raise_http(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "FILE_SAVE_ERROR",
            "Не удалось сохранить загруженный файл.",
        )
    finally:
        await file.close()

    _validate_upload(safe_name, mime_type, size_bytes)

    sha256 = _sha256_of_path(target_path)

    media = MediaRecord(
        file_id=target_file_id,
        file_role="upload",
        storage_path=str(target_path),
        public_name=safe_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        sha256=sha256,
        duration_seconds=None,
        width=None,
        height=None,
        fps=None,
    )
    _MEDIA_STORE[target_file_id] = media

    logger.info(
        "upload_completed",
        file_id=str(target_file_id),
        filename=safe_name,
        size_bytes=size_bytes,
    )

    return UploadResponse(
        file_id=target_file_id,
        original_filename=safe_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        duration_seconds=None,
        width=None,
        height=None,
        fps=None,
    )


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job_endpoint(request: JobCreateRequest) -> JobResponse:
    """Create a new processing job in queued state.

    The current generation snapshot does not yet include the dedicated DB,
    service layer or Celery worker modules, so this route creates a stable
    in-memory job object instead.

    The response still matches the final contract expected by the frontend.
    """
    _validate_preset_name(request.preset_name)
    _validate_job_settings(request.settings)

    media = _MEDIA_STORE.get(request.file_id)
    if media is None:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Исходный загруженный файл не найден.",
        )

    job_id = uuid4()
    now_iso = _iso_now()

    merged_settings = request.settings.model_dump(exclude_none=True)

    job = {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "current_stage": JobStatus.QUEUED.value,
        "progress_percent": 0,
        "preset_name": request.preset_name,
        "analysis": None,
        "result": None,
        "error_code": None,
        "error_message": None,
        "created_at": now_iso,
        "updated_at": now_iso,
        "started_at": None,
        "completed_at": None,
        "original_filename": media.public_name,
        "input_file_id": request.file_id,
        "settings_json": merged_settings,
    }

    _JOB_STORE[job_id] = job
    _record_job_event(
        job_id,
        status_value=JobStatus.QUEUED.value,
        current_stage=JobStatus.QUEUED.value,
        progress_percent=0,
        message="Задача поставлена в очередь.",
    )

    logger.info(
        "job_created",
        job_id=str(job_id),
        preset_name=request.preset_name,
        input_file_id=str(request.file_id),
    )

    return JobResponse.model_validate(_build_job_response_payload(job))


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job_endpoint(job_id: UUID) -> JobResponse:
    """Return current job state by identifier."""
    job = _get_job_or_404(job_id)
    logger.info("job_requested", job_id=str(job_id), status=job["status"])
    return JobResponse.model_validate(_build_job_response_payload(job))


@router.get("/jobs/{job_id}/preview")
async def get_job_preview_endpoint(job_id: UUID) -> dict[str, str]:
    """Return preview asset URLs for a completed job.

    In the current fallback implementation preview assets are only available if
    they have been attached manually to the in-memory job result payload.
    """
    job = _get_job_or_404(job_id)

    if job["status"] != JobStatus.COMPLETED.value:
        _raise_http(
            status.HTTP_409_CONFLICT,
            "JOB_NOT_COMPLETED",
            "Превью доступно только после завершения задачи.",
        )

    result = job.get("result") or {}
    before_file_id = result.get("before_file_id")
    after_file_id = result.get("after_file_id")
    thumbnail_file_id = result.get("thumbnail_file_id")

    if not before_file_id or not after_file_id or not thumbnail_file_id:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Превью-артефакты для задачи не найдены.",
        )

    return {
        "before_url": f"/api/v1/results/media/{before_file_id}",
        "after_url": f"/api/v1/results/media/{after_file_id}",
        "thumbnail_url": f"/api/v1/results/media/{thumbnail_file_id}",
    }


@router.delete("/jobs/{job_id}")
async def delete_job_endpoint(job_id: UUID) -> dict[str, str]:
    """Cancel or mark a job as cancelled.

    Current behavior:
    - queued/running-like jobs are marked as cancelled;
    - terminal jobs remain terminal unless they are not already cancelled;
    - returns the stable payload expected by the frontend.
    """
    job = _get_job_or_404(job_id)

    if job["status"] not in {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    }:
        job["status"] = JobStatus.CANCELLED.value
        job["current_stage"] = JobStatus.CANCELLED.value
        job["updated_at"] = _iso_now()
        job["error_code"] = "JOB_CANCELLED"
        job["error_message"] = "Задача отменена пользователем."
        _record_job_event(
            job_id,
            status_value=JobStatus.CANCELLED.value,
            current_stage=JobStatus.CANCELLED.value,
            progress_percent=int(job["progress_percent"]),
            message="Задача отменена.",
        )
    elif job["status"] == JobStatus.CANCELLED.value:
        pass

    logger.info("job_cancelled_or_deleted", job_id=str(job_id), status=job["status"])
    return {
        "job_id": str(job_id),
        "status": job["status"],
    }


@router.get("/jobs/{job_id}/download")
async def download_result(job_id: UUID) -> FileResponse:
    """Download the final rendered video for a completed job."""
    job = _get_job_or_404(job_id)

    if job["status"] != JobStatus.COMPLETED.value:
        _raise_http(
            status.HTTP_409_CONFLICT,
            "JOB_NOT_COMPLETED",
            "Результат ещё не готов для скачивания.",
        )

    result = job.get("result") or {}
    output_file_id = result.get("output_file_id")
    if not output_file_id:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Итоговый файл задачи не найден.",
        )

    try:
        output_uuid = UUID(str(output_file_id))
    except ValueError:
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Некорректный идентификатор итогового файла.",
        )

    media = _get_media_or_404(output_uuid)
    path = Path(media.storage_path)
    if not path.exists():
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Итоговый файл отсутствует на диске.",
        )

    logger.info("job_result_downloaded", job_id=str(job_id), file_id=str(output_uuid))
    return FileResponse(
        path=str(path),
        media_type=media.mime_type,
        filename=media.public_name,
    )


@router.get("/results/media/{file_id}")
async def get_media(file_id: UUID) -> FileResponse:
    """Return a stored media artifact by identifier."""
    media = _get_media_or_404(file_id)
    path = Path(media.storage_path)

    if not path.exists():
        _raise_http(
            status.HTTP_404_NOT_FOUND,
            "FILE_NOT_FOUND",
            "Файл отсутствует в хранилище.",
        )

    logger.info("media_requested", file_id=str(file_id), role=media.file_role)
    return FileResponse(
        path=str(path),
        media_type=media.mime_type,
        filename=media.public_name,
    )


@router.websocket("/jobs/{job_id}/events")
async def job_events_ws(websocket: WebSocket, job_id: UUID) -> None:
    """Stream job events and heartbeat messages over WebSocket.

    Behavior:
    - if the job does not exist, close with code 1008 as required;
    - accept the connection otherwise;
    - periodically send heartbeat messages;
    - replay the latest event when available;
    - push changes if new events appear in the in-memory history.

    Since Redis pub/sub and dedicated progress services are not yet present in
    the current project snapshot, this route polls in-memory history.
    """
    if job_id not in _JOB_STORE:
        await websocket.close(code=1008, reason="JOB_NOT_FOUND")
        return

    await websocket.accept()
    logger.info("job_ws_connected", job_id=str(job_id))

    settings = get_settings()
    heartbeat_interval = max(1, int(settings.websocket_ping_interval))
    next_heartbeat_at = asyncio.get_event_loop().time() + heartbeat_interval
    last_sent_index = 0

    try:
        while True:
            if job_id not in _JOB_STORE:
                await websocket.close(code=1008, reason="JOB_NOT_FOUND")
                return

            history = _JOB_EVENT_HISTORY.get(job_id, [])
            if last_sent_index < len(history):
                for event_payload in history[last_sent_index:]:
                    await websocket.send_text(json.dumps(_json_safe(event_payload), ensure_ascii=False))
                last_sent_index = len(history)

            current_loop_time = asyncio.get_event_loop().time()
            if current_loop_time >= next_heartbeat_at:
                heartbeat_payload = {
                    "type": "heartbeat",
                    "job_id": str(job_id),
                }
                await websocket.send_text(json.dumps(heartbeat_payload, ensure_ascii=False))
                next_heartbeat_at = current_loop_time + heartbeat_interval

            # Non-blocking light sleep. We keep the connection active and allow
            # the task to be cancellable.
            await asyncio.sleep(0.5)

    except WebSocketDisconnect:
        logger.info("job_ws_disconnected", job_id=str(job_id))
    except Exception as exc:
        logger.exception("job_ws_error", job_id=str(job_id), error=str(exc))
        try:
            await websocket.close(code=1011, reason="INTERNAL_SERVER_ERROR")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Development-only helper utilities
# ---------------------------------------------------------------------------

def _attach_completed_result_for_manual_testing(
    *,
    job_id: UUID,
    source_media_id: UUID,
    output_filename: str = "autoedit-result.mp4",
) -> None:
    """Attach a fake completed output to an existing job.

    This helper is intentionally not exposed as an API route. It exists only to
    make local/manual testing easier while the worker pipeline has not yet been
    generated.

    The function copies no files and simply points the job result to an
    existing media record. It can be called from an interactive shell if needed.
    """
    job = _get_job_or_404(job_id)
    media = _get_media_or_404(source_media_id)

    output_file_id = uuid4()
    output_path = Path(media.storage_path)

    cloned_media = MediaRecord(
        file_id=output_file_id,
        file_role="output",
        storage_path=str(output_path),
        public_name=output_filename,
        mime_type=media.mime_type,
        size_bytes=media.size_bytes,
        sha256=media.sha256,
        duration_seconds=media.duration_seconds,
        width=media.width,
        height=media.height,
        fps=media.fps,
    )
    _MEDIA_STORE[output_file_id] = cloned_media

    now_iso = _iso_now()
    job["status"] = JobStatus.COMPLETED.value
    job["current_stage"] = JobStatus.COMPLETED.value
    job["progress_percent"] = 100
    job["updated_at"] = now_iso
    job["completed_at"] = now_iso
    job["result"] = {
        "output_file_id": str(output_file_id),
        "preview_file_id": None,
        "subtitle_file_id": None,
    }

    _record_job_event(
        job_id,
        status_value=JobStatus.COMPLETED.value,
        current_stage=JobStatus.COMPLETED.value,
        progress_percent=100,
        message="Задача завершена.",
    )


__all__ = [
    "router",
    "healthcheck",
    "get_presets",
    "upload_video",
    "create_job_endpoint",
    "get_job_endpoint",
    "get_job_preview_endpoint",
    "delete_job_endpoint",
    "download_result",
    "get_media",
    "job_events_ws",
]