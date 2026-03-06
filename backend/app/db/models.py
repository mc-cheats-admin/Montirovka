"""SQLAlchemy ORM models for the AutoEdit backend.

This module intentionally contains the complete database model layer in a
single file because the current generation step explicitly requests only
`backend/app/db/models.py`.

The file provides:

- a shared SQLAlchemy declarative `Base`;
- timestamp and UUID helpers;
- the `JobStatus` enum used by the jobs table;
- ORM models:
  - `MediaFile`
  - `Job`
  - `PresetSnapshot`

Design goals:
- PostgreSQL-friendly schema with JSONB support where appropriate;
- SQLAlchemy 2.0 typed ORM style;
- explicit relationships for readable service-layer code;
- compatibility with FastAPI/Pydantic serialization use cases;
- cross-platform safe Python implementation.

Even though PostgreSQL is the target runtime, the code keeps sane fallbacks
for generic SQLAlchemy behavior where possible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import BIGINT, DOUBLE_PRECISION, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Using a callable keeps SQLAlchemy defaults dynamic and easy to test.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class JobStatus(str, Enum):
    """Lifecycle status values for video processing jobs."""

    UPLOADED = "uploaded"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    CUTTING = "cutting"
    ENHANCING = "enhancing"
    INTERPOLATING = "interpolating"
    PROCESSING_AUDIO = "processing_audio"
    GENERATING_SUBTITLES = "generating_subtitles"
    RENDERING = "rendering"
    GENERATING_PREVIEW = "generating_preview"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MediaFile(Base):
    """Stored media file metadata.

    This table tracks both uploaded source files and generated artifacts such as:
    - final rendered video;
    - preview clips;
    - thumbnails;
    - subtitle sidecars.

    The actual bytes live in local or S3-compatible storage. This model stores
    the metadata necessary to find and validate those files.
    """

    __tablename__ = "media_files"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    file_role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    public_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    duration_seconds: Mapped[float | None] = mapped_column(DOUBLE_PRECISION, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(DOUBLE_PRECISION, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    # Relationships to Job through explicit foreign keys.
    jobs_as_input: Mapped[list["Job"]] = relationship(
        back_populates="input_file",
        foreign_keys="Job.input_file_id",
    )
    jobs_as_output: Mapped[list["Job"]] = relationship(
        back_populates="output_file",
        foreign_keys="Job.output_file_id",
    )
    jobs_as_preview: Mapped[list["Job"]] = relationship(
        back_populates="preview_file",
        foreign_keys="Job.preview_file_id",
    )
    jobs_as_subtitle: Mapped[list["Job"]] = relationship(
        back_populates="subtitle_file",
        foreign_keys="Job.subtitle_file_id",
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation suitable for responses."""
        return {
            "id": str(self.id),
            "file_role": self.file_role,
            "storage_path": self.storage_path,
            "public_name": self.public_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Job(Base):
    """Processing job metadata.

    A job references:
    - one uploaded input file;
    - optional output file;
    - optional preview media file;
    - optional subtitle sidecar file;
    - an optional preset snapshot row storing the merged runtime config.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    status: Mapped[JobStatus] = mapped_column(
        SQLEnum(JobStatus, name="job_status_enum"),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    preset_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    input_file_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_files.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    output_file_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    preview_file_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subtitle_file_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    input_file: Mapped[MediaFile] = relationship(
        back_populates="jobs_as_input",
        foreign_keys=[input_file_id],
    )
    output_file: Mapped[MediaFile | None] = relationship(
        back_populates="jobs_as_output",
        foreign_keys=[output_file_id],
    )
    preview_file: Mapped[MediaFile | None] = relationship(
        back_populates="jobs_as_preview",
        foreign_keys=[preview_file_id],
    )
    subtitle_file: Mapped[MediaFile | None] = relationship(
        back_populates="jobs_as_subtitle",
        foreign_keys=[subtitle_file_id],
    )

    preset_snapshot: Mapped["PresetSnapshot | None"] = relationship(
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def mark_failed(self, error_code: str, error_message: str) -> None:
        """Convenience helper for worker/service code."""
        self.status = JobStatus.FAILED
        self.current_stage = JobStatus.FAILED.value
        self.error_code = error_code
        self.error_message = error_message
        self.updated_at = utc_now()

    def mark_completed(self) -> None:
        """Convenience helper for successful completion."""
        now = utc_now()
        self.status = JobStatus.COMPLETED
        self.current_stage = JobStatus.COMPLETED.value
        self.progress_percent = 100
        self.completed_at = now
        self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        """Return a response-friendly dictionary.

        The keys intentionally align with the API contract used by the frontend.
        """
        error_payload: dict[str, str] | None = None
        if self.error_code or self.error_message:
            error_payload = {
                "error_code": self.error_code or "INTERNAL_SERVER_ERROR",
                "message": self.error_message or "Unknown job error.",
            }

        result_payload: dict[str, Any] | None = None
        if self.output_file_id or self.preview_file_id or self.subtitle_file_id:
            result_payload = {
                "output_file_id": str(self.output_file_id) if self.output_file_id else None,
                "preview_file_id": str(self.preview_file_id) if self.preview_file_id else None,
                "subtitle_file_id": str(self.subtitle_file_id) if self.subtitle_file_id else None,
            }

        return {
            "job_id": str(self.id),
            "status": self.status.value if isinstance(self.status, JobStatus) else str(self.status),
            "current_stage": self.current_stage,
            "progress_percent": self.progress_percent,
            "preset_name": self.preset_name,
            "analysis": self.analysis_json,
            "result": result_payload,
            "error": error_payload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "original_filename": self.original_filename,
        }


class PresetSnapshot(Base):
    """Immutable merged preset configuration saved for each job.

    The service layer is expected to store the final runtime settings here after:
    - loading the selected built-in preset;
    - merging user overrides;
    - normalizing runtime values.

    This guarantees reproducibility and easier debugging.
    """

    __tablename__ = "preset_snapshots"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_preset_snapshots_job_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    preset_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    job: Mapped[Job] = relationship(back_populates="preset_snapshot")

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "preset_name": self.preset_name,
            "config_json": self.config_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "Base",
    "Job",
    "JobStatus",
    "MediaFile",
    "PresetSnapshot",
    "utc_now",
]