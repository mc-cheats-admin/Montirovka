"""Pydantic schemas for the AutoEdit backend.

This project generation step currently uses a consolidated schema module
(`backend/app/schemas.py`) instead of the more granular future layout
(`schemas/job.py`, `schemas/upload.py`, etc.). To keep the project coherent,
this file contains all API-facing schema models required by the backend and
already expected by the frontend code.

The module is designed to integrate with:

- ``backend/app/db/models.py`` for ORM interoperability;
- existing frontend TypeScript contracts in ``frontend/lib/types.ts``;
- FastAPI route handlers that will later import these classes directly;
- worker progress events and service-layer response builders.

Important compatibility goals:
- Pydantic v2 style configuration;
- strong typing and explicit validation constraints;
- stable field names matching the REST/WebSocket contract;
- safe conversion from SQLAlchemy ORM objects to response models.

The following schema groups are included:

1. Common response models
   - ``ErrorResponse``

2. Upload models
   - ``UploadResponse``

3. Job request/response models
   - ``JobSettingsRequest``
   - ``JobCreateRequest``
   - ``JobErrorPayload``
   - ``JobResultPayload``
   - ``JobResponse``
   - ``JobProgressEvent``

4. Preset models
   - ``PresetItem``
   - ``PresetListResponse``

5. Analysis helpers
   - ``TimeSegment``
   - ``JobAnalysis``

This file intentionally contains detailed comments and docstrings because the
project configuration requests a verbose implementation style.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PresetName = Literal["gaming", "tutorial", "cinematic"]
TargetFps = Literal[24, 30, 60, 120]
OutputAspectRatio = Literal["16:9", "21:9", "9:16"]
OutputCodec = Literal["h264", "h265"]


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime for runtime defaults.

    A small helper is used instead of a module-level value so every created
    schema instance gets a fresh timestamp when required.
    """
    return datetime.now(timezone.utc)


class AutoEditSchema(BaseModel):
    """Shared base schema with project-wide Pydantic configuration.

    Notes:
    - ``from_attributes=True`` allows easy validation from SQLAlchemy ORM
      objects via ``model_validate(orm_instance)``.
    - ``populate_by_name=True`` keeps future aliasing options flexible.
    - ``extra="ignore"`` makes response parsing resilient to richer payloads.
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


class ErrorResponse(AutoEditSchema):
    """Structured API error payload.

    This matches the documented backend error format and can be reused in
    FastAPI ``responses=...`` declarations or direct error returns.
    """

    error_code: str = Field(
        ...,
        description="Stable machine-readable error code.",
        examples=["INVALID_FILE_EXTENSION", "JOB_NOT_FOUND"],
    )
    message: str = Field(
        ...,
        description="Human-readable explanation of the error.",
        examples=["Неподдерживаемое расширение файла."],
    )


class TimeSegment(AutoEditSchema):
    """Simple start/end time segment used in analysis payloads."""

    start: float = Field(..., ge=0.0, description="Segment start in seconds.")
    end: float = Field(..., ge=0.0, description="Segment end in seconds.")

    @model_validator(mode="after")
    def validate_segment_order(self) -> "TimeSegment":
        """Ensure the end timestamp is not earlier than the start."""
        if self.end < self.start:
            raise ValueError("Segment end must be greater than or equal to start.")
        return self


class UploadResponse(AutoEditSchema):
    """Response returned after successful media upload."""

    file_id: UUID = Field(..., description="Unique identifier of the stored uploaded file.")
    original_filename: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field(..., min_length=1, max_length=127)
    size_bytes: int = Field(..., gt=0, description="Uploaded file size in bytes.")
    duration_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description="Detected media duration in seconds, if available.",
    )
    width: int | None = Field(default=None, ge=1, description="Detected video width.")
    height: int | None = Field(default=None, ge=1, description="Detected video height.")
    fps: float | None = Field(default=None, ge=0.0, description="Detected frames per second.")


class JobSettingsRequest(AutoEditSchema):
    """User-adjustable job settings accepted by job creation endpoint.

    These fields intentionally mirror the frontend form and the product
    specification. Every field is optional because the preset service is
    expected to merge missing values with preset defaults.
    """

    target_fps: TargetFps | None = Field(
        default=None,
        description="Requested target output FPS.",
    )
    zoom_scale: float | None = Field(
        default=None,
        ge=1.0,
        le=2.0,
        description="Zoom intensity for supported presets and stages.",
    )
    cut_aggressiveness: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How aggressively silence/dead segments should be removed.",
    )
    noise_reduction_enabled: bool | None = Field(
        default=None,
        description="Enables or disables noise reduction in the audio chain.",
    )
    subtitles_enabled: bool | None = Field(
        default=None,
        description="Enables subtitle generation where supported.",
    )
    output_aspect_ratio: OutputAspectRatio | None = Field(
        default=None,
        description="Target aspect ratio for output framing/cropping.",
    )
    codec: OutputCodec | None = Field(
        default=None,
        description="Preferred final output codec.",
    )

    @field_validator("zoom_scale")
    @classmethod
    def validate_zoom_scale_precision(cls, value: float | None) -> float | None:
        """Reject non-finite numeric values for safer downstream processing."""
        if value is None:
            return None
        if value != value:  # NaN check without importing math
            raise ValueError("zoom_scale must be a finite number.")
        return value

    @field_validator("cut_aggressiveness")
    @classmethod
    def validate_cut_aggressiveness_precision(cls, value: float | None) -> float | None:
        """Reject NaN values that would break range-based runtime logic."""
        if value is None:
            return None
        if value != value:
            raise ValueError("cut_aggressiveness must be a finite number.")
        return value


class JobCreateRequest(AutoEditSchema):
    """Request body for creating a processing job."""

    file_id: UUID = Field(..., description="Identifier of a previously uploaded file.")
    preset_name: PresetName = Field(..., description="Built-in preset to apply.")
    settings: JobSettingsRequest = Field(
        default_factory=JobSettingsRequest,
        description="Optional user overrides merged with selected preset defaults.",
    )


class JobErrorPayload(AutoEditSchema):
    """Error object attached to a job response when processing fails."""

    error_code: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1)


class JobResultPayload(AutoEditSchema):
    """Flexible result payload for completed or partially completed jobs.

    The backend may enrich this object over time. Core IDs are explicitly typed,
    while additional metadata is allowed through ``extra="ignore"`` on the base
    schema when parsing incoming values.
    """

    output_file_id: UUID | None = Field(default=None)
    preview_file_id: UUID | None = Field(default=None)
    subtitle_file_id: UUID | None = Field(default=None)
    before_file_id: UUID | None = Field(default=None)
    after_file_id: UUID | None = Field(default=None)
    thumbnail_file_id: UUID | None = Field(default=None)
    download_url: str | None = Field(default=None)
    subtitle_url: str | None = Field(default=None)
    output_filename: str | None = Field(default=None)
    subtitle_filename: str | None = Field(default=None)


class JobAnalysis(AutoEditSchema):
    """Structured analysis payload returned by job detail endpoint.

    This schema reflects the analysis object described in the specification.
    The actual worker pipeline may gradually populate only a subset of these
    fields, so most values are optional.
    """

    fps: float | None = Field(default=None, ge=0.0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    bitrate: int | None = Field(default=None, ge=0)
    video_codec: str | None = Field(default=None)
    audio_codec: str | None = Field(default=None)

    audio_peak_db: float | None = Field(default=None)
    audio_rms_db: float | None = Field(default=None)
    estimated_noise_floor_db: float | None = Field(default=None)
    audio_clipping_ratio: float | None = Field(default=None, ge=0.0)

    silence_segments: list[TimeSegment] = Field(default_factory=list)
    scene_changes: list[float] = Field(default_factory=list)
    dead_segments: list[TimeSegment] = Field(default_factory=list)

    @field_validator("scene_changes")
    @classmethod
    def validate_scene_changes(cls, value: list[float]) -> list[float]:
        """Ensure all scene change markers are non-negative."""
        for item in value:
            if item < 0:
                raise ValueError("scene_changes values must be non-negative.")
        return value


class JobResponse(AutoEditSchema):
    """Primary job response model used by create/get job endpoints.

    This schema is aligned with the frontend ``JobResponse`` TypeScript
    interface and with the current SQLAlchemy ``Job.to_dict()`` output.
    """

    job_id: UUID = Field(..., description="Job UUID.")
    status: str = Field(..., description="Current lifecycle status.")
    current_stage: str = Field(..., description="Current processing stage name.")
    progress_percent: int = Field(..., ge=0, le=100)
    preset_name: str = Field(..., description="Selected preset name.")
    analysis: JobAnalysis | dict[str, Any] | None = Field(
        default=None,
        description="Optional analysis payload collected during processing.",
    )
    result: JobResultPayload | dict[str, Any] | None = Field(
        default=None,
        description="Optional result metadata and file identifiers.",
    )
    error: JobErrorPayload | dict[str, str] | None = Field(
        default=None,
        description="Structured error object when the job fails.",
    )

    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    original_filename: str | None = Field(default=None)

    @field_validator("status", "current_stage", "preset_name")
    @classmethod
    def validate_non_empty_strings(cls, value: str) -> str:
        """Prevent accidental blank values in key response fields."""
        if not value.strip():
            raise ValueError("Field must not be empty.")
        return value

    @classmethod
    def from_job_dict(cls, payload: dict[str, Any]) -> "JobResponse":
        """Build a job response from the current ORM ``Job.to_dict()`` format.

        The current SQLAlchemy model exports ``id`` under the API key
        ``job_id`` already, but this helper centralizes parsing logic for
        future services/routes and makes tests cleaner.

        Args:
            payload: Dictionary returned by a service or ORM helper.

        Returns:
            Parsed ``JobResponse`` instance.
        """
        return cls.model_validate(payload)


class JobProgressEvent(AutoEditSchema):
    """WebSocket progress event model for real-time status updates."""

    job_id: UUID = Field(..., description="Identifier of the job being updated.")
    status: str = Field(..., description="Current job lifecycle status.")
    current_stage: str = Field(..., description="Current pipeline stage.")
    progress_percent: int = Field(..., ge=0, le=100)
    message: str = Field(
        ...,
        description="Human-readable progress message for the frontend.",
    )
    timestamp: datetime = Field(
        default_factory=utc_now,
        description="UTC timestamp of the progress event.",
    )

    @field_validator("status", "current_stage", "message")
    @classmethod
    def validate_progress_strings(cls, value: str) -> str:
        """Ensure event text fields remain non-empty and clean."""
        if not value.strip():
            raise ValueError("Progress event string fields must not be empty.")
        return value


class PresetItem(AutoEditSchema):
    """Single preset item returned by the presets endpoint."""

    name: PresetName = Field(..., description="Stable preset identifier.")
    display_name: str = Field(..., min_length=1, description="Human-friendly preset title.")
    default_settings: dict[str, Any] = Field(
        ...,
        description="Complete preset configuration as JSON-compatible dictionary.",
    )


class PresetListResponse(AutoEditSchema):
    """Response model for ``GET /api/v1/presets``."""

    items: list[PresetItem] = Field(
        default_factory=list,
        description="List of built-in presets available to the user.",
    )


# Re-export names explicitly so future imports remain predictable.
__all__ = [
    "AutoEditSchema",
    "ErrorResponse",
    "JobAnalysis",
    "JobCreateRequest",
    "JobErrorPayload",
    "JobProgressEvent",
    "JobResponse",
    "JobResultPayload",
    "JobSettingsRequest",
    "OutputAspectRatio",
    "OutputCodec",
    "PresetItem",
    "PresetListResponse",
    "PresetName",
    "TargetFps",
    "TimeSegment",
    "UploadResponse",
    "utc_now",
]