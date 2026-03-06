"""Central application configuration for the AutoEdit backend.

This module provides a single typed settings object used across the backend.
It is responsible for:

1. Reading configuration values from environment variables and optional `.env`.
2. Normalizing filesystem paths so they behave predictably on Windows and Linux.
3. Converting simple env string values into richer Python-friendly structures.
4. Exposing a cached `get_settings()` accessor for fast repeated use.

Design considerations for this project:
- The backend is expected to run inside Docker in production, but also locally.
- The test environment is Windows-compatible, so path handling must remain
  cross-platform and avoid Unix-only assumptions.
- Other modules already import `get_settings()` and expect attributes like
  `cors_origins`, `temp_dir`, `output_dir`, `preview_dir`, and `preset_dir`.
- `main.py` currently does `list(settings.cors_origins)`, so `cors_origins`
  must be an iterable collection of origin strings, not a raw comma string.

Pydantic v2 note:
- In Pydantic v2, `BaseSettings` is provided by `pydantic-settings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_path_string(value: str) -> str:
    """Normalize a path-like string into a stable, cross-platform string.

    The function intentionally:
    - strips surrounding whitespace;
    - expands `~` if present;
    - avoids resolving against the real filesystem, because target paths may not
      exist yet during startup;
    - returns a forward-slash representation for consistency in logs, JSON and
      cross-platform path handling.

    Args:
        value: Raw path value from environment or defaults.

    Returns:
        Normalized path string using forward slashes.

    Raises:
        ValueError: If the path is empty after stripping.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Path value cannot be empty.")

    path = Path(cleaned).expanduser()
    return path.as_posix()


def _parse_csv_string(value: str) -> list[str]:
    """Parse a comma-separated string into a clean list of unique items.

    Empty entries are ignored. Ordering is preserved.

    Args:
        value: Raw comma-separated string.

    Returns:
        List of cleaned, unique string items.
    """
    result: list[str] = []
    seen: set[str] = set()

    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)

    return result


class Settings(BaseSettings):
    """Typed runtime settings for the AutoEdit backend.

    The class is intentionally verbose and explicit because it forms the public
    configuration contract for the rest of the project.

    Every attribute below maps to an environment variable with the same name in
    uppercase due to Pydantic Settings conventions.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Core application settings
    # -------------------------------------------------------------------------
    app_name: str = Field(default="AutoEdit")
    app_env: str = Field(default="development")
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # -------------------------------------------------------------------------
    # Data stores and queue
    # -------------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://autoedit:autoedit@postgres:5432/autoedit"
    )
    redis_url: str = Field(default="redis://redis:6379/0")

    # -------------------------------------------------------------------------
    # Storage configuration
    # -------------------------------------------------------------------------
    storage_mode: Literal["local", "s3"] = Field(default="local")
    storage_local_root: str = Field(default="/data/storage")
    storage_s3_endpoint: str = Field(default="http://minio:9000")
    storage_s3_access_key: str = Field(default="minioadmin")
    storage_s3_secret_key: str = Field(default="minioadmin")
    storage_s3_bucket: str = Field(default="autoedit")
    storage_s3_secure: bool = Field(default=False)

    # -------------------------------------------------------------------------
    # Upload validation
    # -------------------------------------------------------------------------
    upload_max_size_bytes: int = Field(default=2_147_483_648)
    allowed_video_extensions: str = Field(default=".mp4,.mov,.avi,.mkv")

    # -------------------------------------------------------------------------
    # Runtime directories
    # -------------------------------------------------------------------------
    temp_dir: str = Field(default="/data/tmp")
    output_dir: str = Field(default="/data/output")
    preview_dir: str = Field(default="/data/previews")
    models_dir: str = Field(default="/data/models")

    # -------------------------------------------------------------------------
    # External tools / binaries
    # -------------------------------------------------------------------------
    ffmpeg_binary: str = Field(default="ffmpeg")
    ffprobe_binary: str = Field(default="ffprobe")
    sox_binary: str = Field(default="sox")
    rnnoise_binary: str = Field(default="/usr/local/bin/rnnoise_demo")
    rife_binary: str = Field(default="python")
    rife_script: str = Field(default="/opt/models/rife/inference_video.py")
    realesrgan_binary: str = Field(default="/usr/local/bin/realesrgan-ncnn-vulkan")
    whisper_model: str = Field(default="small")
    enable_gpu: bool = Field(default=True)

    # -------------------------------------------------------------------------
    # Logging / API / housekeeping
    # -------------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    job_retention_hours: int = Field(default=48)
    websocket_ping_interval: int = Field(default=20)
    preset_dir: str = Field(default="/app/app/presets")

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------
    @field_validator("app_name", "app_env", "api_host", "database_url", "redis_url", mode="before")
    @classmethod
    def _strip_non_empty_text(cls, value: object) -> str:
        """Ensure generic text settings are non-empty strings."""
        if value is None:
            raise ValueError("Configuration value cannot be null.")

        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Configuration value cannot be empty.")
        return cleaned

    @field_validator(
        "storage_local_root",
        "temp_dir",
        "output_dir",
        "preview_dir",
        "models_dir",
        "rife_script",
        "preset_dir",
        mode="before",
    )
    @classmethod
    def _normalize_path_fields(cls, value: object) -> str:
        """Normalize path-like configuration fields."""
        if value is None:
            raise ValueError("Path configuration value cannot be null.")
        return _normalize_path_string(str(value))

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> list[str]:
        """Convert CORS env input into a normalized list of origins.

        Supported input forms:
        - comma-separated string:
          "http://localhost:3000,http://127.0.0.1:3000"
        - JSON-like list already parsed by Pydantic/settings integrations
        - tuple/set/list of values

        Returns:
            List of origin strings.
        """
        if value is None:
            return ["http://localhost:3000"]

        if isinstance(value, str):
            items = _parse_csv_string(value)
            return items or ["http://localhost:3000"]

        if isinstance(value, (list, tuple, set)):
            result: list[str] = []
            seen: set[str] = set()

            for item in value:
                cleaned = str(item).strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                result.append(cleaned)

            return result or ["http://localhost:3000"]

        cleaned_single = str(value).strip()
        return [cleaned_single] if cleaned_single else ["http://localhost:3000"]

    @field_validator("allowed_video_extensions", mode="before")
    @classmethod
    def _normalize_allowed_video_extensions(cls, value: object) -> str:
        """Normalize configured allowed file extensions.

        The canonical storage format remains a comma-separated string because
        that matches the external `.env` contract, but the value is cleaned so
        the rest of the application can parse it reliably.

        Examples:
            ".mp4,.mov,.avi,.mkv"
            "mp4, mov, AVI, .MKV"

        Returns:
            Canonical comma-separated lowercase extension list:
            ".mp4,.mov,.avi,.mkv"
        """
        if value is None:
            return ".mp4,.mov,.avi,.mkv"

        if isinstance(value, str):
            raw_items = _parse_csv_string(value)
        elif isinstance(value, (list, tuple, set)):
            raw_items = [str(item).strip() for item in value if str(item).strip()]
        else:
            raw_items = _parse_csv_string(str(value))

        normalized_items: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            cleaned = item.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            if cleaned in seen:
                continue
            seen.add(cleaned)
            normalized_items.append(cleaned)

        return ",".join(normalized_items or [".mp4", ".mov", ".avi", ".mkv"])

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        """Normalize log level to uppercase."""
        if value is None:
            return "INFO"

        cleaned = str(value).strip().upper()
        return cleaned or "INFO"

    @field_validator("api_port", "upload_max_size_bytes", "job_retention_hours", "websocket_ping_interval")
    @classmethod
    def _validate_positive_integers(cls, value: int) -> int:
        """Ensure selected integer settings are positive."""
        if value <= 0:
            raise ValueError("Configuration integer value must be greater than zero.")
        return value

    # -------------------------------------------------------------------------
    # Convenience properties
    # -------------------------------------------------------------------------
    @property
    def allowed_video_extensions_list(self) -> list[str]:
        """Return allowed upload extensions as a normalized list.

        Returns:
            Example:
                [".mp4", ".mov", ".avi", ".mkv"]
        """
        return _parse_csv_string(self.allowed_video_extensions)

    @property
    def api_base_url(self) -> str:
        """Build a local API base URL string from host and port.

        This helper is mainly useful for diagnostics and tests. It intentionally
        preserves configured host/port without trying to infer external proxies.
        """
        return f"http://{self.api_host}:{self.api_port}"

    @property
    def is_development(self) -> bool:
        """Whether the application currently runs in development mode."""
        return self.app_env.lower() == "development"

    @property
    def is_production(self) -> bool:
        """Whether the application currently runs in production mode."""
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached application settings instance.

    Using `lru_cache` ensures:
    - environment parsing happens once per process;
    - repeated imports remain cheap;
    - all modules observe the same configuration snapshot.

    Returns:
        Initialized `Settings` instance.
    """
    return Settings()