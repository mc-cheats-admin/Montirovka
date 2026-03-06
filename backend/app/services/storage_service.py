"""Storage service layer for AutoEdit backend.

This module provides a storage abstraction used by API routes and worker code
to manage uploaded source files and generated media artifacts.

Implemented storage backends:
- Local filesystem storage
- S3-compatible object storage (for example MinIO)

The service is intentionally designed to work with the currently existing
project structure shown in context:

- configuration is loaded from ``app.core.config.get_settings()``
- structured logging comes from ``app.core.logging.get_logger()``
- media metadata is persisted via ``app.db.models.MediaFile``

Key design decisions:
- Windows-safe and cross-platform path handling via ``pathlib.Path``
- streaming file copy for existing local files to avoid loading large artifacts
  fully into memory
- SHA-256 checksum calculation for every stored file
- best-effort media probing when future utility modules are present
- stable domain errors with error-code-like messages expected by the project

The public factory for external callers is ``get_storage_service(db)``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import MediaFile

try:
    import boto3
    from botocore.client import BaseClient
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover - boto3 may be unavailable in partial environments
    boto3 = None  # type: ignore[assignment]
    BaseClient = Any  # type: ignore[misc,assignment]
    BotoCoreError = Exception  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment]


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Optional integration imports
# ---------------------------------------------------------------------------

try:
    # Planned future location in the full project.
    from app.core.security import (  # type: ignore
        ensure_safe_path as imported_ensure_safe_path,
    )
    from app.core.security import (  # type: ignore
        generate_storage_subpath as imported_generate_storage_subpath,
    )
    from app.core.security import sanitize_filename as imported_sanitize_filename  # type: ignore
except Exception:  # pragma: no cover - fallback for the current partial project
    imported_sanitize_filename = None
    imported_ensure_safe_path = None
    imported_generate_storage_subpath = None


def _probe_media_if_available(file_path: str) -> dict[str, Any]:
    """Best-effort media probing helper.

    The current generation step does not yet include the planned ffmpeg/media
    utility modules in the actual filesystem, so the storage layer must remain
    usable without them.

    Returns an empty dictionary when no probe utility is available or probing
    fails. This keeps upload and storage flows resilient.
    """
    with suppress(Exception):
        from app.utils.ffmpeg import probe_media  # type: ignore

        result = probe_media(file_path)
        if isinstance(result, dict):
            return result

    with suppress(Exception):
        from app.utils.media import probe_media  # type: ignore

        result = probe_media(file_path)
        if isinstance(result, dict):
            return result

    return {}


# ---------------------------------------------------------------------------
# Fallback security/path helpers
# ---------------------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _fallback_sanitize_filename(filename: str) -> str:
    """Return a conservative, filesystem-safe filename."""
    raw_name = Path(filename or "").name.strip()
    if not raw_name:
        raw_name = "file"

    # Replace whitespace with underscore first for readability.
    raw_name = re.sub(r"\s+", "_", raw_name)

    safe_name = _FILENAME_SAFE_RE.sub("_", raw_name).strip("._")
    if not safe_name:
        safe_name = "file"

    # Keep filename length bounded for DB/filesystem friendliness.
    if len(safe_name) > 200:
        suffix = Path(safe_name).suffix
        stem = Path(safe_name).stem[: max(1, 200 - len(suffix))]
        safe_name = f"{stem}{suffix}"

    return safe_name


def sanitize_filename(filename: str) -> str:
    """Use project security helper when available, otherwise fallback locally."""
    if imported_sanitize_filename is not None:
        return imported_sanitize_filename(filename)
    return _fallback_sanitize_filename(filename)


def _fallback_ensure_safe_path(base_dir: str | Path, target_path: str | Path) -> str:
    """Ensure target path is inside base directory."""
    base = Path(base_dir).resolve()
    target = Path(target_path).resolve()

    try:
        target.relative_to(base)
    except ValueError as exc:  # pragma: no cover - simple defensive branch
        raise RuntimeError("FILE_SAVE_ERROR") from exc

    return str(target)


def ensure_safe_path(base_dir: str | Path, target_path: str | Path) -> str:
    """Use project security helper when available, otherwise fallback locally."""
    if imported_ensure_safe_path is not None:
        return imported_ensure_safe_path(str(base_dir), str(target_path))
    return _fallback_ensure_safe_path(base_dir, target_path)


def _role_to_directory(file_role: str) -> str:
    """Map logical file role to storage directory."""
    normalized = (file_role or "").strip().lower()

    if normalized in {"upload", "uploads", "input", "source", "original"}:
        return "uploads"
    if normalized in {"output", "outputs", "result", "render", "rendered"}:
        return "outputs"
    if normalized.startswith("preview") or normalized in {"thumbnail", "thumb"}:
        return "previews"
    if normalized in {"subtitle", "subtitles", "srt"}:
        return "subtitles"
    if normalized in {"temp", "tmp", "working"}:
        return "temp"

    # Unknown roles are still supported but isolated in their own folder.
    return normalized or "misc"


def _fallback_generate_storage_subpath(file_role: str, original_name: str) -> str:
    """Generate a unique relative path inside the configured storage root."""
    safe_name = sanitize_filename(original_name)
    ext = Path(safe_name).suffix.lower()
    stem = Path(safe_name).stem or "file"
    folder = _role_to_directory(file_role)
    unique_name = f"{stem}_{uuid4().hex}{ext}"
    return Path(folder) / unique_name


def generate_storage_subpath(file_role: str, original_name: str) -> str:
    """Use project security helper when available, otherwise fallback locally."""
    if imported_generate_storage_subpath is not None:
        return imported_generate_storage_subpath(file_role, original_name)
    return str(_fallback_generate_storage_subpath(file_role, original_name)).replace("\\", "/")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ALLOWED_FALLBACK_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".srt": "application/x-subrip",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


@dataclass(slots=True)
class FileHashAndSize:
    """Small structured helper used during hashing and copy operations."""

    sha256: str
    size_bytes: int


def _guess_mime_type(path_or_name: str, fallback: str = "application/octet-stream") -> str:
    """Best-effort MIME type detection based on file extension."""
    suffix = Path(path_or_name).suffix.lower()
    if suffix in ALLOWED_FALLBACK_MIME_TYPES:
        return ALLOWED_FALLBACK_MIME_TYPES[suffix]

    guessed, _ = mimetypes.guess_type(path_or_name)
    return guessed or fallback


def _compute_sha256_from_bytes(payload: bytes) -> str:
    """Compute SHA-256 for in-memory bytes."""
    return hashlib.sha256(payload).hexdigest()


def _compute_sha256_from_file(path: Path, chunk_size: int = 1024 * 1024) -> FileHashAndSize:
    """Compute SHA-256 and file size in one streaming pass."""
    digest = hashlib.sha256()
    total_size = 0

    with path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)
            digest.update(chunk)

    return FileHashAndSize(sha256=digest.hexdigest(), size_bytes=total_size)


def _stream_copy_with_hash(source: Path, destination: Path, chunk_size: int = 1024 * 1024) -> FileHashAndSize:
    """Copy a file in chunks while computing its SHA-256 and size.

    This helper is intentionally used for large generated artifacts to avoid
    loading the entire file into memory.
    """
    digest = hashlib.sha256()
    total_size = 0

    destination.parent.mkdir(parents=True, exist_ok=True)

    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            total_size += len(chunk)

    return FileHashAndSize(sha256=digest.hexdigest(), size_bytes=total_size)


def _extract_probe_fields(probe_result: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional probe result into DB-ready MediaFile fields."""
    fps_value = probe_result.get("fps")
    duration_value = probe_result.get("duration") or probe_result.get("duration_seconds")

    try:
        fps = float(fps_value) if fps_value is not None else None
    except (TypeError, ValueError):
        fps = None

    try:
        duration_seconds = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    width = probe_result.get("width")
    height = probe_result.get("height")

    return {
        "duration_seconds": duration_seconds if duration_seconds is None or duration_seconds >= 0 else None,
        "width": int(width) if isinstance(width, (int, float)) and int(width) > 0 else None,
        "height": int(height) if isinstance(height, (int, float)) and int(height) > 0 else None,
        "fps": fps if fps is None or fps >= 0 else None,
    }


# ---------------------------------------------------------------------------
# Public abstraction
# ---------------------------------------------------------------------------

class StorageService(ABC):
    """Abstract storage service contract.

    Every implementation stores bytes/artifacts and creates matching
    ``MediaFile`` rows in the database.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.logger = get_logger(self.__class__.__name__)

    @abstractmethod
    def save_upload(self, file_bytes: bytes, original_filename: str, mime_type: str) -> MediaFile:
        """Persist an uploaded raw file and return its ``MediaFile`` metadata row."""

    @abstractmethod
    def save_local_file(
        self,
        source_path: str,
        file_role: str,
        public_name: str,
        mime_type: str,
    ) -> MediaFile:
        """Persist an existing local file into configured storage."""

    @abstractmethod
    def open_file(self, file_id: UUID) -> tuple[str, str]:
        """Return a local accessible path and MIME type for a stored file."""

    @abstractmethod
    def get_path(self, file_id: UUID) -> str:
        """Return a local or resolved storage path for a stored file."""

    @abstractmethod
    def delete_file(self, file_id: UUID) -> None:
        """Delete file bytes and metadata row."""

    @abstractmethod
    def exists(self, file_id: UUID) -> bool:
        """Return True when file metadata exists and underlying bytes are accessible."""

    # ------------------------------------------------------------------
    # Shared ORM helpers
    # ------------------------------------------------------------------

    def _get_media_row(self, file_id: UUID) -> MediaFile:
        """Load a MediaFile row or raise FileNotFoundError."""
        media = self.db.get(MediaFile, file_id)
        if media is None:
            raise FileNotFoundError("FILE_NOT_FOUND")
        return media

    def _create_media_record(
        self,
        *,
        file_role: str,
        storage_path: str,
        public_name: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        probe_data: dict[str, Any] | None = None,
    ) -> MediaFile:
        """Insert and return a ``MediaFile`` metadata row."""
        probe_fields = _extract_probe_fields(probe_data or {})
        media = MediaFile(
            file_role=file_role,
            storage_path=storage_path,
            public_name=public_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            duration_seconds=probe_fields["duration_seconds"],
            width=probe_fields["width"],
            height=probe_fields["height"],
            fps=probe_fields["fps"],
        )

        try:
            self.db.add(media)
            self.db.commit()
            self.db.refresh(media)
        except Exception:
            self.db.rollback()
            raise

        self.logger.info(
            "media_record_created",
            file_id=str(media.id),
            file_role=file_role,
            storage_path=storage_path,
            size_bytes=size_bytes,
        )
        return media


# ---------------------------------------------------------------------------
# Local filesystem implementation
# ---------------------------------------------------------------------------

class LocalStorageService(StorageService):
    """Filesystem-backed storage implementation.

    Files are stored under the configured local storage root using relative
    storage paths persisted in the database.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self.root = Path(self.settings.storage_local_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_upload(self, file_bytes: bytes, original_filename: str, mime_type: str) -> MediaFile:
        """Store uploaded in-memory bytes as a source media file."""
        safe_name = sanitize_filename(original_filename)
        relative_path = generate_storage_subpath("upload", safe_name)
        destination = self.root / relative_path

        self.logger.info(
            "save_upload_started",
            original_filename=safe_name,
            mime_type=mime_type,
            destination=str(destination),
        )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            safe_destination = Path(ensure_safe_path(self.root, destination))

            with safe_destination.open("wb") as output_file:
                output_file.write(file_bytes)

            file_size = len(file_bytes)
            sha256 = _compute_sha256_from_bytes(file_bytes)
            probe_data = _probe_media_if_available(str(safe_destination))
        except PermissionError as exc:
            self.db.rollback()
            self.logger.exception(
                "save_upload_permission_error",
                original_filename=safe_name,
                destination=str(destination),
            )
            raise RuntimeError("STORAGE_PERMISSION_ERROR") from exc
        except Exception as exc:
            self.db.rollback()
            self.logger.exception(
                "save_upload_failed",
                original_filename=safe_name,
                destination=str(destination),
            )
            raise RuntimeError("FILE_SAVE_ERROR") from exc

        return self._create_media_record(
            file_role="upload",
            storage_path=relative_path.replace("\\", "/"),
            public_name=safe_name,
            mime_type=mime_type or _guess_mime_type(safe_name),
            size_bytes=file_size,
            sha256=sha256,
            probe_data=probe_data,
        )

    def save_local_file(
        self,
        source_path: str,
        file_role: str,
        public_name: str,
        mime_type: str,
    ) -> MediaFile:
        """Store an existing local file using streaming copy.

        This method is intended for large worker-generated artifacts such as:
        - final outputs
        - previews
        - subtitles
        """
        source = Path(source_path).expanduser()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError("FILE_NOT_FOUND")

        safe_public_name = sanitize_filename(public_name or source.name)
        relative_path = generate_storage_subpath(file_role, safe_public_name)
        destination = self.root / relative_path

        self.logger.info(
            "save_local_file_started",
            source_path=str(source),
            file_role=file_role,
            public_name=safe_public_name,
            destination=str(destination),
        )

        try:
            safe_destination = Path(ensure_safe_path(self.root, destination))
            copied = _stream_copy_with_hash(source, safe_destination)
            probe_data = _probe_media_if_available(str(safe_destination))
        except PermissionError as exc:
            self.db.rollback()
            self.logger.exception(
                "save_local_file_permission_error",
                source_path=str(source),
                destination=str(destination),
            )
            raise RuntimeError("STORAGE_PERMISSION_ERROR") from exc
        except Exception as exc:
            self.db.rollback()
            self.logger.exception(
                "save_local_file_failed",
                source_path=str(source),
                destination=str(destination),
            )
            raise RuntimeError("FILE_SAVE_ERROR") from exc

        return self._create_media_record(
            file_role=file_role,
            storage_path=relative_path.replace("\\", "/"),
            public_name=safe_public_name,
            mime_type=mime_type or _guess_mime_type(safe_public_name),
            size_bytes=copied.size_bytes,
            sha256=copied.sha256,
            probe_data=probe_data,
        )

    def open_file(self, file_id: UUID) -> tuple[str, str]:
        """Return absolute local path and MIME type for a stored file."""
        media = self._get_media_row(file_id)
        path = Path(self.get_path(file_id))

        if not path.exists():
            raise FileNotFoundError("FILE_NOT_FOUND")

        return str(path), media.mime_type

    def get_path(self, file_id: UUID) -> str:
        """Resolve a stored relative path into an absolute filesystem path."""
        media = self._get_media_row(file_id)

        stored_path = media.storage_path.replace("\\", "/")
        candidate = (self.root / stored_path).resolve()
        ensure_safe_path(self.root, candidate)

        return str(candidate)

    def delete_file(self, file_id: UUID) -> None:
        """Delete the local file and corresponding metadata row."""
        media = self._get_media_row(file_id)

        file_path = None
        with suppress(Exception):
            file_path = Path(self.get_path(file_id))

        try:
            if file_path is not None and file_path.exists():
                file_path.unlink()

                # Best-effort directory cleanup for now-empty parent folders.
                parent = file_path.parent
                while parent != self.root:
                    with suppress(OSError):
                        parent.rmdir()
                    parent = parent.parent

            self.db.delete(media)
            self.db.commit()
            self.logger.info(
                "file_deleted",
                file_id=str(file_id),
                storage_path=media.storage_path,
            )
        except PermissionError as exc:
            self.db.rollback()
            self.logger.exception(
                "delete_file_permission_error",
                file_id=str(file_id),
                storage_path=media.storage_path,
            )
            raise RuntimeError("STORAGE_PERMISSION_ERROR") from exc
        except FileNotFoundError:
            # If metadata exists but underlying file is gone, remove DB row anyway.
            self.db.delete(media)
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.logger.exception(
                "delete_file_failed",
                file_id=str(file_id),
                storage_path=media.storage_path,
            )
            raise

    def exists(self, file_id: UUID) -> bool:
        """Check both metadata presence and physical file existence."""
        media = self.db.get(MediaFile, file_id)
        if media is None:
            return False

        try:
            return Path(self.get_path(file_id)).exists()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# S3-compatible implementation
# ---------------------------------------------------------------------------

class S3StorageService(StorageService):
    """S3-compatible storage implementation.

    The database stores object keys in ``storage_path``.
    ``open_file`` downloads the object into a temporary local file and returns
    its path plus MIME type so FastAPI endpoints can serve it.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(db)

        if boto3 is None:  # pragma: no cover - environment-dependent
            raise RuntimeError("FILE_SAVE_ERROR")

        self.bucket = self.settings.storage_s3_bucket
        self.client: BaseClient = boto3.client(
            "s3",
            endpoint_url=self.settings.storage_s3_endpoint,
            aws_access_key_id=self.settings.storage_s3_access_key,
            aws_secret_access_key=self.settings.storage_s3_secret_key,
            use_ssl=bool(self.settings.storage_s3_secure),
        )
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self) -> None:
        """Create the configured S3 bucket if it does not exist yet."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception:
                # Bucket may already exist or creation may be managed elsewhere.
                self.logger.warning("s3_bucket_create_skipped", bucket=self.bucket)

    def save_upload(self, file_bytes: bytes, original_filename: str, mime_type: str) -> MediaFile:
        """Store uploaded bytes as an S3 object."""
        safe_name = sanitize_filename(original_filename)
        key = generate_storage_subpath("upload", safe_name)

        self.logger.info(
            "s3_save_upload_started",
            original_filename=safe_name,
            key=key,
            mime_type=mime_type,
        )

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=file_bytes,
                ContentType=mime_type or _guess_mime_type(safe_name),
            )
            probe_data = self._probe_uploaded_object(key)
        except PermissionError as exc:  # pragma: no cover - unlikely direct branch
            self.db.rollback()
            raise RuntimeError("STORAGE_PERMISSION_ERROR") from exc
        except (BotoCoreError, ClientError) as exc:
            self.db.rollback()
            self.logger.exception("s3_save_upload_failed", key=key)
            raise RuntimeError("FILE_SAVE_ERROR") from exc

        return self._create_media_record(
            file_role="upload",
            storage_path=key,
            public_name=safe_name,
            mime_type=mime_type or _guess_mime_type(safe_name),
            size_bytes=len(file_bytes),
            sha256=_compute_sha256_from_bytes(file_bytes),
            probe_data=probe_data,
        )

    def save_local_file(
        self,
        source_path: str,
        file_role: str,
        public_name: str,
        mime_type: str,
    ) -> MediaFile:
        """Upload an existing local file to S3 without loading it fully into memory."""
        source = Path(source_path).expanduser()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError("FILE_NOT_FOUND")

        safe_public_name = sanitize_filename(public_name or source.name)
        key = generate_storage_subpath(file_role, safe_public_name)
        checksum = _compute_sha256_from_file(source)

        self.logger.info(
            "s3_save_local_file_started",
            source_path=str(source),
            file_role=file_role,
            key=key,
        )

        try:
            with source.open("rb") as source_file:
                self.client.upload_fileobj(
                    Fileobj=source_file,
                    Bucket=self.bucket,
                    Key=key,
                    ExtraArgs={"ContentType": mime_type or _guess_mime_type(safe_public_name)},
                )
            probe_data = self._probe_uploaded_object(key, suffix=source.suffix)
        except PermissionError as exc:  # pragma: no cover
            self.db.rollback()
            raise RuntimeError("STORAGE_PERMISSION_ERROR") from exc
        except (BotoCoreError, ClientError) as exc:
            self.db.rollback()
            self.logger.exception("s3_save_local_file_failed", key=key)
            raise RuntimeError("FILE_SAVE_ERROR") from exc

        return self._create_media_record(
            file_role=file_role,
            storage_path=key,
            public_name=safe_public_name,
            mime_type=mime_type or _guess_mime_type(safe_public_name),
            size_bytes=checksum.size_bytes,
            sha256=checksum.sha256,
            probe_data=probe_data,
        )

    def _probe_uploaded_object(self, key: str, suffix: str | None = None) -> dict[str, Any]:
        """Download an object temporarily and probe it, best-effort."""
        temp_path: Path | None = None
        try:
            fd, raw_temp_path = tempfile.mkstemp(suffix=suffix or Path(key).suffix)
            os.close(fd)
            temp_path = Path(raw_temp_path)

            self.client.download_file(self.bucket, key, str(temp_path))
            return _probe_media_if_available(str(temp_path))
        except Exception:
            return {}
        finally:
            if temp_path is not None:
                with suppress(FileNotFoundError):
                    temp_path.unlink()

    def open_file(self, file_id: UUID) -> tuple[str, str]:
        """Download an S3 object into a temp file and return its local path + MIME."""
        media = self._get_media_row(file_id)
        key = media.storage_path

        fd, raw_temp_path = tempfile.mkstemp(suffix=Path(media.public_name).suffix or ".bin")
        os.close(fd)
        temp_path = Path(raw_temp_path)

        try:
            self.client.download_file(self.bucket, key, str(temp_path))
        except (BotoCoreError, ClientError) as exc:
            with suppress(FileNotFoundError):
                temp_path.unlink()
            raise FileNotFoundError("FILE_NOT_FOUND") from exc

        return str(temp_path), media.mime_type

    def get_path(self, file_id: UUID) -> str:
        """Return an S3 URI-like descriptor for logging/debugging purposes."""
        media = self._get_media_row(file_id)
        return f"s3://{self.bucket}/{media.storage_path}"

    def delete_file(self, file_id: UUID) -> None:
        """Delete S3 object and corresponding metadata row."""
        media = self._get_media_row(file_id)

        try:
            self.client.delete_object(Bucket=self.bucket, Key=media.storage_path)
            self.db.delete(media)
            self.db.commit()
            self.logger.info(
                "s3_file_deleted",
                file_id=str(file_id),
                key=media.storage_path,
            )
        except (BotoCoreError, ClientError) as exc:
            self.db.rollback()
            self.logger.exception(
                "s3_delete_failed",
                file_id=str(file_id),
                key=media.storage_path,
            )
            raise RuntimeError("FILE_SAVE_ERROR") from exc

    def exists(self, file_id: UUID) -> bool:
        """Check metadata presence and S3 object existence."""
        media = self.db.get(MediaFile, file_id)
        if media is None:
            return False

        try:
            self.client.head_object(Bucket=self.bucket, Key=media.storage_path)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_storage_service(db: Session) -> StorageService:
    """Create the configured storage service implementation.

    Args:
        db: Active SQLAlchemy session.

    Returns:
        A storage service instance for the currently configured storage mode.
    """
    settings = get_settings()
    mode = (settings.storage_mode or "local").strip().lower()

    logger.info("storage_service_requested", storage_mode=mode)

    if mode == "s3":
        return S3StorageService(db)

    return LocalStorageService(db)


__all__ = [
    "StorageService",
    "LocalStorageService",
    "S3StorageService",
    "get_storage_service",
    "sanitize_filename",
    "ensure_safe_path",
    "generate_storage_subpath",
]