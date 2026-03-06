"""Main FastAPI application entrypoint for the AutoEdit backend.

This module is responsible for:

1. Creating and configuring the FastAPI application.
2. Wiring shared middleware such as request logging and timing headers.
3. Registering global exception handlers that convert unexpected failures into
   stable JSON responses.
4. Initializing runtime directories during startup so the service behaves
   predictably in both local and containerized environments.
5. Including the versioned API router tree from ``app.api.routes``.

The implementation is intentionally production-oriented while remaining
compatible with the simplified file plan used in this generation step.

Important design notes:
- The code is Windows-friendly and uses ``pathlib.Path`` for path operations.
- Logging is delegated to ``app.core.logging``.
- Environment and runtime settings are delegated to ``app.core.config``.
- API endpoints are expected to be exposed by ``app.api.routes.router``.
"""

from __future__ import annotations

import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

try:
    # Planned project import.
    from app.api.routes import router as api_router
except Exception:  # pragma: no cover - defensive fallback for partial generation steps
    api_router = None  # type: ignore[assignment]


REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach per-request metadata and emit structured request logs.

    Responsibilities:
    - Generate a stable request identifier for each incoming request.
    - Measure execution time.
    - Add timing and request ID headers to responses.
    - Log request start and finish events in a structured way.

    This middleware intentionally does not log request bodies to avoid leaking
    large uploads, secrets, or binary data.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        logger = get_logger(__name__)
        request_id = request.headers.get(REQUEST_ID_HEADER, str(uuid4()))
        started_at = time.perf_counter()

        # Store request-scoped metadata for handlers and downstream code.
        request.state.request_id = request_id

        logger.info(
            "request_started",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            query=str(request.url.query) if request.url.query else "",
            client_host=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.exception(
                "request_failed_unhandled",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Process-Time-Ms"] = str(duration_ms)

        logger.info(
            "request_finished",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        return response


def _safe_str(value: object | None) -> str | None:
    """Return a string representation safe for JSON responses."""
    if value is None:
        return None
    return str(value)


def _build_error_response(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Create a standardized JSON error response.

    The backend specification requires structured errors with:
    - ``error_code``
    - ``message``

    Extra debugging metadata is limited and safe by default.
    """
    payload: dict[str, Any] = {
        "error_code": error_code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }

    if extra:
        payload.update(extra)

    return JSONResponse(status_code=status_code, content=payload)


def _ensure_runtime_directories() -> None:
    """Create configured runtime directories if they do not exist.

    The service relies on several directories for uploads, temporary files,
    rendered outputs, previews, and local models.

    This helper is intentionally tolerant:
    - it creates missing directories;
    - it uses cross-platform ``pathlib`` operations;
    - it does not fail if directories already exist.
    """
    settings = get_settings()
    logger = get_logger(__name__)

    candidate_dirs = [
        settings.temp_dir,
        settings.output_dir,
        settings.preview_dir,
        settings.models_dir,
    ]

    # Storage local root is relevant only for local storage mode, but creating
    # it eagerly is harmless and simplifies local/dev behavior.
    if getattr(settings, "storage_local_root", None):
        candidate_dirs.append(settings.storage_local_root)

    # Preset directory may be mounted from the image or repository.
    if getattr(settings, "preset_dir", None):
        candidate_dirs.append(settings.preset_dir)

    for raw_dir in candidate_dirs:
        path = Path(raw_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        logger.info("runtime_directory_ready", path=str(path))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    Startup actions:
    - configure structured logging;
    - ensure runtime directories exist;
    - log application boot metadata.

    Shutdown actions:
    - emit a clean shutdown log record.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger(__name__)

    logger.info(
        "application_starting",
        app_name=settings.app_name,
        app_env=settings.app_env,
        api_host=settings.api_host,
        api_port=settings.api_port,
    )

    _ensure_runtime_directories()

    app.state.settings = settings
    app.state.started_at = time.time()

    logger.info("application_started", app_name=settings.app_name)
    try:
        yield
    finally:
        logger.info("application_stopping", app_name=settings.app_name)


def _install_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers for the API application."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Convert FastAPI/Starlette HTTP exceptions to stable JSON payloads."""
        logger = get_logger(__name__)

        detail = exc.detail
        message = "HTTP error"
        error_code = "INTERNAL_SERVER_ERROR"

        if isinstance(detail, dict):
            error_code = _safe_str(detail.get("error_code")) or error_code
            message = _safe_str(detail.get("message")) or _safe_str(detail.get("detail")) or message
        elif isinstance(detail, str):
            message = detail

            detail_upper = detail.strip().upper().replace(" ", "_")
            if detail_upper in {
                "INVALID_FILE_EXTENSION",
                "INVALID_FILE_SIZE",
                "INVALID_MIME_TYPE",
                "FILE_SAVE_ERROR",
                "FILE_NOT_FOUND",
                "INVALID_PRESET",
                "INVALID_SETTINGS",
                "JOB_NOT_FOUND",
                "JOB_NOT_COMPLETED",
                "COMMAND_TIMEOUT",
                "FFMPEG_FAILED",
                "OPENCV_FAILED",
                "RIFE_NOT_AVAILABLE",
                "REALESRGAN_NOT_AVAILABLE",
                "WHISPER_MODEL_MISSING",
                "STORAGE_PERMISSION_ERROR",
                "JOB_CANCELLED",
                "INTERNAL_SERVER_ERROR",
            }:
                error_code = detail_upper

        logger.warning(
            "http_exception_handled",
            request_id=getattr(request.state, "request_id", None),
            method=request.method,
            path=request.url.path,
            status_code=exc.status_code,
            error_code=error_code,
            message=message,
        )

        return _build_error_response(
            request=request,
            status_code=exc.status_code,
            error_code=error_code,
            message=message,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Return request validation errors in the project's structured format."""
        logger = get_logger(__name__)

        logger.warning(
            "request_validation_failed",
            request_id=getattr(request.state, "request_id", None),
            method=request.method,
            path=request.url.path,
            errors=exc.errors(),
        )

        return _build_error_response(
            request=request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="INVALID_SETTINGS",
            message="Запрос не прошёл валидацию.",
            extra={"details": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch all unexpected exceptions and return a safe JSON error."""
        logger = get_logger(__name__)

        logger.error(
            "unhandled_exception",
            request_id=getattr(request.state, "request_id", None),
            method=request.method,
            path=request.url.path,
            exception_type=exc.__class__.__name__,
            exception_message=str(exc),
            traceback=traceback.format_exc(),
        )

        return _build_error_response(
            request=request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code="INTERNAL_SERVER_ERROR",
            message="Внутренняя ошибка сервера.",
        )


def create_application() -> FastAPI:
    """Application factory for AutoEdit.

    Returns:
        Configured ``FastAPI`` application instance.

    The app is created through a factory instead of a module-level singleton
    because this approach is friendlier for:
    - testing;
    - future dependency overrides;
    - multi-process startup patterns;
    - explicit lifecycle control.
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "AutoEdit backend API — self-hosted сервис автоматического "
            "видеомонтажа на FastAPI, Celery, FFmpeg и локальных ML-моделях."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, "X-Process-Time-Ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    _install_exception_handlers(app)

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        """Redirect the service root to interactive API docs."""
        return RedirectResponse(url="/docs", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Container/platform-level lightweight health endpoint."""
        return {
            "status": "ok",
            "app_name": settings.app_name,
        }

    if api_router is not None:
        app.include_router(api_router, prefix="/api/v1")
    else:
        logger = get_logger(__name__)
        logger.warning(
            "api_router_not_available",
            message=(
                "app.api.routes.router could not be imported during this generation "
                "step. The application will start with only root and healthz routes."
            ),
        )

    return app


app = create_application()