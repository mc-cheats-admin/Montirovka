from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _import_module(module_name: str) -> Any:
    """Import helper used to keep tests explicit and readable."""
    return importlib.import_module(module_name)


def _get_fastapi_app() -> Any:
    """Resolve the FastAPI application from app.main.

    The project may expose either:
    - a module-level ``app`` instance;
    - a ``create_app()`` factory.

    This helper supports both shapes to keep the tests stable while the
    repository evolves.
    """
    main_module = _import_module("app.main")

    app = getattr(main_module, "app", None)
    if app is not None:
        return app

    create_app = getattr(main_module, "create_app", None)
    if callable(create_app):
        return create_app()

    raise AssertionError("FastAPI application was not found in app.main.")


def _patch_settings_provider(monkeypatch: pytest.MonkeyPatch, module: Any, settings: Any) -> None:
    """Monkeypatch settings accessor on a target module when available."""
    if hasattr(module, "get_settings"):
        monkeypatch.setattr(module, "get_settings", lambda: settings)


def _build_temp_preset_dir(tmp_path: Path) -> Path:
    """Create a temporary preset directory with the three required preset files."""
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir(parents=True, exist_ok=True)

    fixtures: dict[str, dict[str, Any]] = {
        "gaming.json": {
            "name": "gaming",
            "display_name": "Gaming / Highlight",
            "target_fps": 120,
            "cutting": {"aggressiveness": 0.7},
            "audio": {"noise_reduction": True},
        },
        "tutorial.json": {
            "name": "tutorial",
            "display_name": "Tutorial / Обучение",
            "target_fps": 60,
            "subtitles": {"enabled": True},
            "cutting": {"aggressiveness": 0.85},
        },
        "cinematic.json": {
            "name": "cinematic",
            "display_name": "Cinematic / Контент",
            "target_fps": 24,
            "letterbox": {"enabled": True},
            "color": {"contrast": 1.08},
        },
    }

    for filename, payload in fixtures.items():
        (preset_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return preset_dir


def test_health_api_returns_ok() -> None:
    """The public health endpoint should return a stable readiness payload."""
    app = _get_fastapi_app()

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "ok"
    assert isinstance(payload.get("app_name"), str)
    assert payload["app_name"].strip() != ""


def test_preset_service_lists_and_loads_presets_from_configured_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preset service should discover and load built-in preset JSON files."""
    preset_service = _import_module("app.services.preset_service")
    preset_dir = _build_temp_preset_dir(tmp_path)

    fake_settings = SimpleNamespace(preset_dir=str(preset_dir))
    _patch_settings_provider(monkeypatch, preset_service, fake_settings)

    items = preset_service.list_presets()
    assert isinstance(items, list)
    assert len(items) == 3

    names = {item["name"] for item in items}
    assert names == {"gaming", "tutorial", "cinematic"}

    gaming = preset_service.load_preset("gaming")
    assert gaming["name"] == "gaming"
    assert gaming["display_name"] == "Gaming / Highlight"
    assert gaming["target_fps"] == 120


def test_preset_service_merges_user_settings_without_dropping_base_values() -> None:
    """User overrides should update allowed runtime keys while preserving preset data."""
    preset_service = _import_module("app.services.preset_service")

    preset_config = {
        "name": "tutorial",
        "display_name": "Tutorial / Обучение",
        "target_fps": 60,
        "cutting": {"aggressiveness": 0.85},
        "audio_chain": {"target_lufs": -14},
        "codec": "h264",
        "subtitles_enabled": True,
    }
    user_settings = {
        "target_fps": 30,
        "cut_aggressiveness": 0.4,
        "noise_reduction_enabled": False,
        "subtitles_enabled": False,
        "output_aspect_ratio": "16:9",
        "codec": "h265",
        "zoom_scale": 1.2,
    }

    merged = preset_service.merge_user_settings(preset_config, user_settings)

    assert merged["name"] == "tutorial"
    assert merged["display_name"] == "Tutorial / Обучение"
    assert merged["target_fps"] == 30
    assert merged["codec"] == "h265"
    assert merged["subtitles_enabled"] is False
    assert merged["output_aspect_ratio"] == "16:9"
    assert merged["zoom_scale"] == 1.2

    cutting = merged.get("cutting", {})
    assert isinstance(cutting, dict)
    assert "aggressiveness" in cutting


def test_progress_channel_uses_expected_job_prefix() -> None:
    """Redis/pubsub channel naming must stay stable for API and WebSocket integration."""
    progress_service = _import_module("app.services.progress_service")
    job_id = str(uuid4())

    channel = progress_service.progress_channel(job_id)

    assert channel == f"job_progress:{job_id}"


def test_publish_progress_uses_json_payload_and_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish_progress should serialize payload and send it to the correct channel."""
    progress_service = _import_module("app.services.progress_service")
    job_id = str(uuid4())
    payload = {
        "job_id": job_id,
        "status": "analyzing",
        "current_stage": "analyzing",
        "progress_percent": 12,
        "message": "Analyzing input media",
    }

    published_calls: list[tuple[str, str]] = []

    class FakeRedisClient:
        def publish(self, channel: str, message: str) -> None:
            published_calls.append((channel, message))

    if hasattr(progress_service, "_get_redis_client"):
        monkeypatch.setattr(progress_service, "_get_redis_client", lambda: FakeRedisClient())
    elif hasattr(progress_service, "get_redis_client"):
        monkeypatch.setattr(progress_service, "get_redis_client", lambda: FakeRedisClient())
    elif hasattr(progress_service, "redis_client"):
        monkeypatch.setattr(progress_service, "redis_client", FakeRedisClient())
    else:
        pytest.skip("progress_service does not expose a patchable Redis client accessor.")

    progress_service.publish_progress(job_id, payload)

    assert len(published_calls) == 1
    channel, raw_message = published_calls[0]

    assert channel == f"job_progress:{job_id}"

    decoded_message = json.loads(raw_message)
    assert decoded_message["job_id"] == job_id
    assert decoded_message["status"] == "analyzing"
    assert decoded_message["progress_percent"] == 12


def test_pipeline_publish_stage_updates_job_and_emits_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish_stage should coordinate DB status update and progress broadcast."""
    pipeline = _import_module("app.workers.pipeline")
    job_id = str(uuid4())

    captured: dict[str, Any] = {
        "db_updates": [],
        "progress_payloads": [],
    }

    @contextmanager
    def fake_db_session_scope() -> Any:
        yield SimpleNamespace(name="fake-db")

    def fake_update_job_status(
        db: Any,
        target_job_id: str,
        *,
        status: str,
        current_stage: str,
        progress_percent: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "db": db,
            "job_id": target_job_id,
            "status": status,
            "current_stage": current_stage,
            "progress_percent": progress_percent,
            "error_code": error_code,
            "error_message": error_message,
        }
        captured["db_updates"].append(record)
        return record

    def fake_publish_progress(target_job_id: str, payload: dict[str, Any]) -> None:
        captured["progress_payloads"].append(
            {
                "job_id": target_job_id,
                "payload": payload,
            }
        )

    monkeypatch.setattr(pipeline, "_db_session_scope", fake_db_session_scope)
    monkeypatch.setattr(pipeline, "_update_job_status", fake_update_job_status)
    monkeypatch.setattr(pipeline, "_publish_progress", fake_publish_progress, raising=False)

    if not hasattr(pipeline, "_publish_progress"):
        progress_service = _import_module("app.services.progress_service")
        monkeypatch.setattr(progress_service, "publish_progress", fake_publish_progress)
    else:
        monkeypatch.setattr(pipeline, "_publish_progress", fake_publish_progress)

    pipeline.publish_stage(
        job_id=job_id,
        status="enhancing",
        current_stage="enhancing",
        progress_percent=48,
        message="Applying stabilization pass",
    )

    assert len(captured["db_updates"]) == 1
    db_update = captured["db_updates"][0]
    assert db_update["job_id"] == job_id
    assert db_update["status"] == "enhancing"
    assert db_update["current_stage"] == "enhancing"
    assert db_update["progress_percent"] == 48

    assert len(captured["progress_payloads"]) == 1
    published = captured["progress_payloads"][0]
    assert published["job_id"] == job_id
    assert published["payload"]["job_id"] == job_id
    assert published["payload"]["status"] == "enhancing"
    assert published["payload"]["current_stage"] == "enhancing"
    assert published["payload"]["progress_percent"] == 48
    assert published["payload"]["message"] == "Applying stabilization pass"
    assert "timestamp" in published["payload"]


def test_pipeline_assert_not_cancelled_allows_non_cancelled_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assert_not_cancelled should not raise when the job status is active."""
    pipeline = _import_module("app.workers.pipeline")
    job_id = str(uuid4())

    @contextmanager
    def fake_db_session_scope() -> Any:
        yield SimpleNamespace(name="fake-db")

    fake_job = SimpleNamespace(status="queued")

    monkeypatch.setattr(pipeline, "_db_session_scope", fake_db_session_scope)
    monkeypatch.setattr(pipeline, "_get_job_or_raise", lambda db, value: fake_job)

    pipeline.assert_not_cancelled(job_id)


def test_pipeline_assert_not_cancelled_raises_for_cancelled_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assert_not_cancelled should raise a domain error for cancelled jobs."""
    pipeline = _import_module("app.workers.pipeline")
    job_id = str(uuid4())

    @contextmanager
    def fake_db_session_scope() -> Any:
        yield SimpleNamespace(name="fake-db")

    fake_job = SimpleNamespace(status="cancelled")

    monkeypatch.setattr(pipeline, "_db_session_scope", fake_db_session_scope)
    monkeypatch.setattr(pipeline, "_get_job_or_raise", lambda db, value: fake_job)

    with pytest.raises(RuntimeError, match="JOB_CANCELLED"):
        pipeline.assert_not_cancelled(job_id)