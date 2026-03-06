"""Media utility helpers for the AutoEdit backend.

This module provides a compact, production-ready media inspection layer focused
on safe probing of uploaded and generated media files. The current project
state already references ``app.utils.media.probe_media`` from the storage
service as a fallback import path, so this file is implemented to work
immediately with the existing codebase.

Primary goals:
- expose a stable ``probe_media(file_path: str) -> dict[str, Any]`` function;
- provide predictable metadata extraction from ``ffprobe`` JSON output;
- stay cross-platform and Windows-safe by using ``pathlib`` and ``subprocess``;
- avoid hard dependency on future modules that may not yet exist;
- return structured, normalized values useful for uploads and pipeline stages.

The returned dictionary intentionally uses keys compatible with the broader
project specification, including:
- duration
- duration_seconds
- width
- height
- fps
- bitrate
- codec_name
- audio_codec_name
- has_audio
- has_video
- format_name
- size_bytes

When probing fails, the module raises ``RuntimeError("FFMPEG_FAILED")`` or
``FileNotFoundError`` depending on the failure mode. This behavior is aligned
with the error vocabulary requested by the project.
"""

from __future__ import annotations

import json
import math
import mimetypes
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class MediaStreamInfo:
    """Small normalized representation of a media stream.

    This internal dataclass is used only to simplify parsing and reasoning about
    ``ffprobe`` output. The final public API still returns plain dictionaries,
    because that shape is easier to serialize and consume by the rest of the
    project.
    """

    codec_type: str
    codec_name: str | None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bit_rate: int | None = None


def _safe_float(value: Any) -> float | None:
    """Convert value to float when possible.

    Returns ``None`` instead of raising on malformed values, which makes probe
    parsing resilient to inconsistent metadata emitted by different containers
    and codecs.
    """
    if value is None:
        return None

    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None

    if isinstance(value, int):
        return float(value)

    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None

    if not math.isfinite(parsed):
        return None

    return parsed


def _safe_int(value: Any) -> int | None:
    """Convert value to int when possible, otherwise return ``None``."""
    if value is None:
        return None

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)

    raw = str(value).strip()
    if not raw:
        return None

    try:
        return int(raw)
    except ValueError:
        as_float = _safe_float(raw)
        if as_float is None:
            return None
        return int(as_float)


def _parse_fraction_to_float(value: Any) -> float | None:
    """Parse common FPS-like fraction strings such as ``30000/1001``.

    ``ffprobe`` frequently reports frame rates in fractional form via
    ``avg_frame_rate`` and ``r_frame_rate``.
    """
    if value is None:
        return None

    raw = str(value).strip()
    if not raw or raw in {"0/0", "N/A"}:
        return None

    if "/" not in raw:
        return _safe_float(raw)

    try:
        fraction = Fraction(raw)
    except (ValueError, ZeroDivisionError):
        return None

    if fraction.denominator == 0:
        return None

    numeric = float(fraction)
    if not math.isfinite(numeric) or numeric <= 0:
        return None

    return numeric


def _normalize_fps(video_stream: dict[str, Any]) -> float | None:
    """Extract the most useful FPS value from a video stream."""
    candidates = [
        video_stream.get("avg_frame_rate"),
        video_stream.get("r_frame_rate"),
        video_stream.get("codec_time_base"),
    ]

    for candidate in candidates:
        parsed = _parse_fraction_to_float(candidate)
        if parsed is not None and parsed > 0 and parsed < 1000:
            return parsed

    return None


def _normalize_duration_seconds(format_info: dict[str, Any], streams: list[dict[str, Any]]) -> float | None:
    """Resolve duration from format or stream metadata.

    Containers sometimes provide duration only at format level, while some
    formats expose it more reliably on streams. This helper checks both.
    """
    candidates: list[Any] = [format_info.get("duration")]

    for stream in streams:
        candidates.append(stream.get("duration"))
        tags = stream.get("tags")
        if isinstance(tags, dict):
            candidates.append(tags.get("DURATION"))

    for candidate in candidates:
        parsed = _safe_float(candidate)
        if parsed is not None and parsed >= 0:
            return parsed

    return None


def _normalize_bitrate(format_info: dict[str, Any], streams: list[dict[str, Any]]) -> int | None:
    """Resolve bitrate from format or streams."""
    candidates: list[Any] = [format_info.get("bit_rate")]

    for stream in streams:
        candidates.append(stream.get("bit_rate"))

    for candidate in candidates:
        parsed = _safe_int(candidate)
        if parsed is not None and parsed >= 0:
            return parsed

    return None


def _pick_video_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first video stream from ffprobe output."""
    for stream in streams:
        if str(stream.get("codec_type") or "").lower() == "video":
            return stream
    return None


def _pick_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first audio stream from ffprobe output."""
    for stream in streams:
        if str(stream.get("codec_type") or "").lower() == "audio":
            return stream
    return None


def _normalize_stream(stream: dict[str, Any]) -> MediaStreamInfo:
    """Convert raw ffprobe stream payload into a normalized dataclass."""
    codec_type = str(stream.get("codec_type") or "").lower()
    codec_name = str(stream.get("codec_name")).strip() if stream.get("codec_name") else None

    width = _safe_int(stream.get("width"))
    height = _safe_int(stream.get("height"))
    fps = _normalize_fps(stream) if codec_type == "video" else None
    bit_rate = _safe_int(stream.get("bit_rate"))

    return MediaStreamInfo(
        codec_type=codec_type,
        codec_name=codec_name,
        width=width,
        height=height,
        fps=fps,
        bit_rate=bit_rate,
    )


def _run_subprocess(command: list[str], timeout_seconds: int = 60) -> tuple[int, str, str]:
    """Execute a subprocess command and return ``(returncode, stdout, stderr)``.

    The implementation is intentionally local to keep this module independent
    from future utility modules that may not yet exist in the repository.
    """
    logger.info(
        "media_command_start",
        command=command,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "media_command_timeout",
            command=command,
            timeout_seconds=timeout_seconds,
        )
        raise RuntimeError("COMMAND_TIMEOUT") from exc
    except FileNotFoundError as exc:
        logger.error(
            "media_command_binary_missing",
            command=command,
            error=str(exc),
        )
        raise RuntimeError("FFMPEG_FAILED") from exc
    except OSError as exc:
        logger.error(
            "media_command_os_error",
            command=command,
            error=str(exc),
        )
        raise RuntimeError("FFMPEG_FAILED") from exc

    logger.info(
        "media_command_finish",
        command=command,
        returncode=completed.returncode,
    )

    return completed.returncode, completed.stdout, completed.stderr


def _build_ffprobe_command(file_path: Path) -> list[str]:
    """Build the ffprobe command used for structured JSON probing."""
    settings = get_settings()
    return [
        settings.ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]


def _ensure_existing_file(file_path: str | Path) -> Path:
    """Validate that a probe target exists and is a regular file."""
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        logger.warning(
            "media_file_not_found",
            file_path=str(path),
        )
        raise FileNotFoundError(str(path))

    if not path.is_file():
        logger.warning(
            "media_not_a_file",
            file_path=str(path),
        )
        raise FileNotFoundError(str(path))

    return path


def _parse_ffprobe_json(stdout: str) -> dict[str, Any]:
    """Parse ffprobe JSON output into a dictionary."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.error("media_probe_invalid_json", error=str(exc))
        raise RuntimeError("FFMPEG_FAILED") from exc

    if not isinstance(payload, dict):
        logger.error("media_probe_invalid_payload_type")
        raise RuntimeError("FFMPEG_FAILED")

    return payload


def _guess_mime_type(file_path: Path) -> str:
    """Best-effort MIME type detection from extension."""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return mime_type or "application/octet-stream"


def probe_media(file_path: str) -> dict[str, Any]:
    """Probe a media file using ffprobe and return normalized metadata.

    Args:
        file_path: Path to an existing local media file.

    Returns:
        Dictionary with normalized metadata fields, including both generic and
        project-specific aliases.

    Raises:
        FileNotFoundError: If the file does not exist.
        RuntimeError: With message ``COMMAND_TIMEOUT`` or ``FFMPEG_FAILED``.
    """
    path = _ensure_existing_file(file_path)
    command = _build_ffprobe_command(path)
    returncode, stdout, stderr = _run_subprocess(command, timeout_seconds=60)

    if returncode != 0:
        logger.error(
            "media_probe_failed",
            file_path=str(path),
            returncode=returncode,
            stderr=stderr.strip()[:4000],
        )
        raise RuntimeError("FFMPEG_FAILED")

    payload = _parse_ffprobe_json(stdout)

    raw_streams = payload.get("streams")
    raw_format = payload.get("format")

    streams = raw_streams if isinstance(raw_streams, list) else []
    format_info = raw_format if isinstance(raw_format, dict) else {}

    video_stream_raw = _pick_video_stream(streams)
    audio_stream_raw = _pick_audio_stream(streams)

    video_stream = _normalize_stream(video_stream_raw) if video_stream_raw else None
    audio_stream = _normalize_stream(audio_stream_raw) if audio_stream_raw else None

    duration_seconds = _normalize_duration_seconds(format_info, streams)
    bitrate = _normalize_bitrate(format_info, streams)
    size_bytes = _safe_int(format_info.get("size")) or path.stat().st_size

    result: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "mime_type": _guess_mime_type(path),
        "format_name": str(format_info.get("format_name") or "").strip() or None,
        "duration": duration_seconds,
        "duration_seconds": duration_seconds,
        "size_bytes": size_bytes,
        "bitrate": bitrate,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "codec_name": video_stream.codec_name if video_stream else None,
        "audio_codec_name": audio_stream.codec_name if audio_stream else None,
        "width": video_stream.width if video_stream else None,
        "height": video_stream.height if video_stream else None,
        "fps": video_stream.fps if video_stream else None,
        "video_bit_rate": video_stream.bit_rate if video_stream else None,
        "audio_bit_rate": audio_stream.bit_rate if audio_stream else None,
    }

    logger.info(
        "media_probe_succeeded",
        file_path=str(path),
        duration_seconds=result.get("duration_seconds"),
        width=result.get("width"),
        height=result.get("height"),
        fps=result.get("fps"),
        has_audio=result.get("has_audio"),
        has_video=result.get("has_video"),
    )

    return result


def get_media_duration_seconds(file_path: str) -> float | None:
    """Convenience wrapper returning only the media duration."""
    metadata = probe_media(file_path)
    duration = metadata.get("duration_seconds")
    return _safe_float(duration)


def get_video_dimensions(file_path: str) -> tuple[int | None, int | None]:
    """Convenience wrapper returning ``(width, height)`` for a media file."""
    metadata = probe_media(file_path)
    return _safe_int(metadata.get("width")), _safe_int(metadata.get("height"))


def get_video_fps(file_path: str) -> float | None:
    """Convenience wrapper returning the detected FPS."""
    metadata = probe_media(file_path)
    return _safe_float(metadata.get("fps"))


def has_audio_stream(file_path: str) -> bool:
    """Return whether the probed media file contains an audio stream."""
    metadata = probe_media(file_path)
    return bool(metadata.get("has_audio"))


def ensure_probeable_media(file_path: str) -> dict[str, Any]:
    """Validate that a file is probeable and return its metadata.

    This helper is useful as a semantic alias in API routes and services where
    an explicit validation step reads better than a raw call to ``probe_media``.
    """
    return probe_media(file_path)