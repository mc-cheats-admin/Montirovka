"""Initial database schema for AutoEdit.

This migration creates the first production-ready PostgreSQL schema required by
the current backend architecture:

- media_files
- jobs
- preset_snapshots

Key design decisions:
- PostgreSQL UUID columns are used directly for primary and foreign keys.
- JSONB is used for flexible runtime settings and analysis payloads.
- Foreign keys between jobs and media_files are intentionally separated by role
  (input/output/preview/subtitle) to support the processing pipeline.
- Indexes are created for the most common lookup patterns, especially job
  status tracking and foreign key access.

The revision identifier is intentionally human-readable to match the filename and
keep the project easy to inspect in development and production environments.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# Revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


JOB_STATUS_VALUES = (
    "uploaded",
    "queued",
    "analyzing",
    "cutting",
    "enhancing",
    "interpolating",
    "processing_audio",
    "generating_subtitles",
    "rendering",
    "generating_preview",
    "completed",
    "failed",
    "cancelled",
)


def upgrade() -> None:
    """Apply the initial schema.

    The schema is optimized for PostgreSQL because the project explicitly uses
    PostgreSQL 16 and JSONB-based metadata storage.
    """
    job_status_enum = postgresql.ENUM(
        *JOB_STATUS_VALUES,
        name="job_status_enum",
        create_type=False,
    )
    job_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "media_files",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("file_role", sa.String(length=32), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("public_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_seconds", sa.Float(precision=53), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(precision=53), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
    )

    op.create_index(
        "ix_media_files_file_role",
        "media_files",
        ["file_role"],
        unique=False,
    )
    op.create_index(
        "ix_media_files_sha256",
        "media_files",
        ["sha256"],
        unique=False,
    )

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "status",
            job_status_enum,
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("preset_name", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "input_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "output_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "preview_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subtitle_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "settings_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "analysis_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "progress_percent",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "current_stage",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_jobs_progress_percent_range",
        ),
    )

    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)
    op.create_index("ix_jobs_preset_name", "jobs", ["preset_name"], unique=False)
    op.create_index("ix_jobs_input_file_id", "jobs", ["input_file_id"], unique=False)
    op.create_index("ix_jobs_output_file_id", "jobs", ["output_file_id"], unique=False)
    op.create_index("ix_jobs_preview_file_id", "jobs", ["preview_file_id"], unique=False)
    op.create_index("ix_jobs_subtitle_file_id", "jobs", ["subtitle_file_id"], unique=False)
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"], unique=False)

    op.create_table(
        "preset_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preset_name", sa.String(length=32), nullable=False),
        sa.Column(
            "config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
        sa.UniqueConstraint("job_id", name="uq_preset_snapshots_job_id"),
    )

    op.create_index(
        "ix_preset_snapshots_preset_name",
        "preset_snapshots",
        ["preset_name"],
        unique=False,
    )


def downgrade() -> None:
    """Rollback the initial schema in reverse dependency order."""
    op.drop_index("ix_preset_snapshots_preset_name", table_name="preset_snapshots")
    op.drop_table("preset_snapshots")

    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_subtitle_file_id", table_name="jobs")
    op.drop_index("ix_jobs_preview_file_id", table_name="jobs")
    op.drop_index("ix_jobs_output_file_id", table_name="jobs")
    op.drop_index("ix_jobs_input_file_id", table_name="jobs")
    op.drop_index("ix_jobs_preset_name", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_media_files_sha256", table_name="media_files")
    op.drop_index("ix_media_files_file_role", table_name="media_files")
    op.drop_table("media_files")

    job_status_enum = postgresql.ENUM(
        *JOB_STATUS_VALUES,
        name="job_status_enum",
        create_type=False,
    )
    job_status_enum.drop(op.get_bind(), checkfirst=True)