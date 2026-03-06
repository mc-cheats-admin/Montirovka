"""Preset service for AutoEdit backend.

This module is responsible for built-in preset management and runtime settings
preparation. It provides a stable service layer used by:

- ``app.services.job_service`` during job creation;
- future API routes that expose the preset list;
- worker/pipeline code that needs a fully resolved processing configuration.

The project specification requires the following public functions:

- ``load_preset(preset_name: str) -> dict[str, Any]``
- ``list_presets() -> list[dict[str, Any]]``
- ``merge_user_settings(preset_config: dict[str, Any], user_settings: dict[str, Any]) -> dict[str, Any]``
- ``normalize_runtime_settings(preset_name: str, merged: dict[str, Any]) -> dict[str, Any]``

Design goals of this implementation:
- work with the current consolidated project structure already present in the repository;
- remain compatible with the future planned structure;
- be strict about allowed user overrides;
- produce deterministic runtime settings without surprising ``None`` values
  in user-facing runtime fields;
- gracefully fall back to embedded presets if JSON files are not present yet.

The merge strategy intentionally exposes only the documented user-overridable
fields:

- target_fps
- cut_aggressiveness
- noise_reduction_enabled
- subtitles_enabled
- output_aspect_ratio
- codec
- zoom_scale

Internal binary paths, storage paths and other sensitive runtime configuration
are not accepted from user input.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_PRESET_NAMES = ("gaming", "tutorial", "cinematic")
SUPPORTED_TARGET_FPS = {24, 30, 60, 120}
SUPPORTED_ASPECT_RATIOS = {"16:9", "21:9", "9:16"}
SUPPORTED_CODECS = {"h264", "h265"}

# Only these top-level fields may be overridden by the user.
USER_OVERRIDE_FIELDS = {
    "target_fps",
    "cut_aggressiveness",
    "noise_reduction_enabled",
    "subtitles_enabled",
    "output_aspect_ratio",
    "codec",
    "zoom_scale",
}

# Some runtime fields are not present in raw preset JSON but are useful for a
# fully deterministic pipeline configuration.
DEFAULT_RUNTIME_VALUES: dict[str, Any] = {
    "codec": "h264",
    "zoom_scale": 1.0,
    "cut_aggressiveness": 0.5,
    "noise_reduction_enabled": False,
    "subtitles_enabled": False,
    "output_aspect_ratio": "16:9",
}

# Embedded presets are used as a reliable fallback when preset JSON files have
# not been generated yet or are unavailable in the current environment.
EMBEDDED_PRESETS: dict[str, dict[str, Any]] = {
    "gaming": {
        "name": "gaming",
        "display_name": "Gaming / Highlight",
        "target_fps": 120,
        "interpolation_engine": "rife",
        "enable_motion_blur": True,
        "highlight_detection": {
            "enabled": True,
            "audio_energy_threshold": 0.7,
            "min_gap_seconds": 2.0,
        },
        "crosshair_tracking": {
            "enabled": True,
            "roi_size": 200,
            "template_match_threshold": 0.6,
            "crop_scale": 0.7,
            "zoom_in_scale": 1.3,
            "zoom_in_duration_seconds": 0.5,
            "zoom_hold_seconds": 2.0,
            "easing": "ease_in_out",
        },
        "cutting": {
            "remove_silence": True,
            "remove_dead_segments": True,
            "aggressiveness": 0.7,
        },
        "audio": {
            "noise_reduction": True,
            "highpass_hz": 80,
            "presence_boost_db": 3,
            "presence_band_hz": 3500,
            "mud_cut_db": -2,
            "mud_band_hz": 300,
            "compressor_ratio": 4.0,
            "compressor_threshold_db": -18,
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
        "background_blur": {
            "enabled": True,
            "face_scale_factor": 1.1,
            "min_neighbors": 5,
            "gaussian_kernel": 51,
        },
        "active_area_zoom": {
            "enabled": True,
            "max_zoom": 1.2,
        },
        "subtitles": {
            "enabled": True,
            "model": "small",
            "format": "srt",
        },
        "cutting": {
            "remove_silence": True,
            "aggressiveness": 0.85,
        },
        "audio_chain": {
            "noise_gate_threshold_db": -40,
            "highpass_hz": 80,
            "presence_boost_db": 3,
            "presence_band_hz": 3000,
            "mud_cut_db": -2,
            "mud_band_hz": 300,
            "compressor_ratio": 4.0,
            "compressor_threshold_db": -18,
            "deesser_band_hz_low": 6000,
            "deesser_band_hz_high": 8000,
            "limiter_ceiling_db": -1,
            "target_lufs": -14,
        },
        "stabilization": {
            "enabled": True,
            "shakiness": 10,
            "accuracy": 15,
            "smoothing": 30,
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
        "crop": {
            "enabled": True,
            "aspect_ratio_options": ["21:9", "9:16", "16:9"],
        },
        "transitions": {
            "type": "crossfade",
            "duration_ms": 300,
        },
        "stabilization": {
            "enabled": True,
            "shakiness": 8,
            "accuracy": 15,
            "smoothing": 25,
        },
        "audio": {
            "reverb": True,
            "compressor_ratio": 3.0,
            "compressor_threshold_db": -20,
            "target_lufs": -16,
        },
        "letterbox": {
            "enabled": True,
            "bar_size_percent": 10,
        },
    },
}


def _deep_copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a dictionary.

    Deep copying is important here because preset configurations are reused
    across requests. Without it, runtime merges could accidentally mutate the
    in-memory embedded preset definitions or parsed JSON results reused later.
    """
    return copy.deepcopy(value)


def _validate_preset_name_or_raise(preset_name: str) -> str:
    """Validate a preset name and return its normalized form.

    Raises:
        ValueError: When the preset name is missing or unsupported.
    """
    normalized = (preset_name or "").strip().lower()
    if normalized not in SUPPORTED_PRESET_NAMES:
        raise ValueError("INVALID_PRESET")
    return normalized


def _preset_dir() -> Path:
    """Resolve the preset directory from application settings.

    The settings module is expected to normalize paths already, but this helper
    still uses ``pathlib.Path`` to keep all filesystem operations
    cross-platform and Windows-safe.
    """
    settings = get_settings()
    preset_dir = Path(settings.preset_dir)
    return preset_dir


def _preset_file_path(preset_name: str) -> Path:
    """Build the expected JSON file path for a built-in preset."""
    return _preset_dir() / f"{preset_name}.json"


def _load_preset_from_disk(preset_name: str) -> dict[str, Any] | None:
    """Try to load a preset JSON file from disk.

    Returns:
        Parsed preset dictionary when successful, otherwise ``None``.

    Notes:
    - Invalid JSON or unreadable files are logged and treated as missing.
    - This is intentionally non-fatal because embedded presets provide a
      reliable fallback.
    """
    path = _preset_file_path(preset_name)
    if not path.exists() or not path.is_file():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "preset_file_load_failed",
            preset_name=preset_name,
            preset_path=str(path),
            error=str(exc),
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "preset_file_invalid_format",
            preset_name=preset_name,
            preset_path=str(path),
        )
        return None

    return payload


def _get_embedded_preset(preset_name: str) -> dict[str, Any]:
    """Return an embedded preset copy or raise when missing."""
    if preset_name not in EMBEDDED_PRESETS:
        raise ValueError("INVALID_PRESET")
    return _deep_copy_dict(EMBEDDED_PRESETS[preset_name])


def _set_nested(mapping: dict[str, Any], path: list[str], value: Any) -> None:
    """Set a nested value inside a dictionary, creating intermediate dicts.

    Example:
        ``_set_nested(config, ["audio", "noise_reduction"], True)``
    """
    current = mapping
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _get_nested(mapping: dict[str, Any], path: list[str], default: Any = None) -> Any:
    """Safely fetch a nested dictionary value."""
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce a value to boolean in a predictable way."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_float(value: Any, *, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    """Convert a value to float with optional range clamping."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default

    if minimum is not None and numeric < minimum:
        numeric = minimum
    if maximum is not None and numeric > maximum:
        numeric = maximum

    return numeric


def _coerce_int(value: Any, *, default: int) -> int:
    """Convert a value to int or return the default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_preset(preset_name: str) -> dict[str, Any]:
    """Load one built-in preset configuration.

    The function first tries the JSON file from ``settings.preset_dir`` and
    falls back to embedded defaults when the file is unavailable.

    Args:
        preset_name: One of ``gaming``, ``tutorial`` or ``cinematic``.

    Returns:
        Full preset configuration dictionary.

    Raises:
        ValueError: If the preset name is invalid.
    """
    normalized_name = _validate_preset_name_or_raise(preset_name)

    disk_payload = _load_preset_from_disk(normalized_name)
    if disk_payload is not None:
        logger.info(
            "preset_loaded_from_disk",
            preset_name=normalized_name,
            preset_path=str(_preset_file_path(normalized_name)),
        )
        return _deep_copy_dict(disk_payload)

    logger.info(
        "preset_loaded_from_embedded_defaults",
        preset_name=normalized_name,
    )
    return _get_embedded_preset(normalized_name)


def list_presets() -> list[dict[str, Any]]:
    """Return the list of available built-in presets.

    Response shape is intentionally aligned with the API schema expectations:
    every item contains:
    - ``name``
    - ``display_name``
    - ``default_settings``

    Returns:
        List of preset metadata dictionaries in stable display order.
    """
    items: list[dict[str, Any]] = []

    for preset_name in SUPPORTED_PRESET_NAMES:
        preset = load_preset(preset_name)
        items.append(
            {
                "name": preset_name,
                "display_name": str(preset.get("display_name", preset_name)),
                "default_settings": preset,
            }
        )

    return items


def merge_user_settings(preset_config: dict[str, Any], user_settings: dict[str, Any]) -> dict[str, Any]:
    """Merge allowed user overrides into a preset configuration.

    This function does **not** allow users to replace internal binary paths,
    storage configuration or arbitrary nested internals. Only the documented
    public runtime settings are accepted.

    Merge behavior:
    - ``None`` values are ignored;
    - unsupported keys are ignored;
    - raw top-level preset structure is preserved;
    - normalized helper runtime fields are added later by
      ``normalize_runtime_settings``.

    Args:
        preset_config: Base preset configuration, usually loaded from JSON.
        user_settings: User-provided partial override mapping.

    Returns:
        Merged configuration dictionary.
    """
    base = _deep_copy_dict(preset_config)
    safe_user_settings = {
        key: value
        for key, value in dict(user_settings or {}).items()
        if key in USER_OVERRIDE_FIELDS and value is not None
    }

    # Straight top-level overrides.
    for key in (
        "target_fps",
        "output_aspect_ratio",
        "codec",
        "zoom_scale",
        "cut_aggressiveness",
        "noise_reduction_enabled",
        "subtitles_enabled",
    ):
        if key in safe_user_settings:
            base[key] = safe_user_settings[key]

    # Mirror key runtime overrides into nested preset structures where the
    # worker pipeline is likely to read them from.
    if "cut_aggressiveness" in safe_user_settings:
        _set_nested(base, ["cutting", "aggressiveness"], safe_user_settings["cut_aggressiveness"])

    if "noise_reduction_enabled" in safe_user_settings:
        noise_enabled = safe_user_settings["noise_reduction_enabled"]
        _set_nested(base, ["audio", "noise_reduction"], noise_enabled)

        # Tutorial uses "audio_chain" in its preset structure, so keep parity.
        if isinstance(base.get("audio_chain"), dict):
            base["audio_chain"]["noise_reduction"] = noise_enabled

    if "subtitles_enabled" in safe_user_settings:
        subtitles_enabled = safe_user_settings["subtitles_enabled"]
        _set_nested(base, ["subtitles", "enabled"], subtitles_enabled)

    if "zoom_scale" in safe_user_settings:
        zoom_scale = safe_user_settings["zoom_scale"]
        _set_nested(base, ["crosshair_tracking", "zoom_in_scale"], zoom_scale)

        if isinstance(base.get("active_area_zoom"), dict):
            base["active_area_zoom"]["max_zoom"] = zoom_scale

    return base


def normalize_runtime_settings(preset_name: str, merged: dict[str, Any]) -> dict[str, Any]:
    """Normalize a merged preset into a deterministic runtime configuration.

    The goal is to produce a single JSON snapshot that worker and pipeline code
    can use without guessing defaults for common user-facing runtime keys.

    Normalized fields guaranteed at the top level:
    - name
    - preset_name
    - display_name
    - target_fps
    - codec
    - zoom_scale
    - cut_aggressiveness
    - noise_reduction_enabled
    - subtitles_enabled
    - output_aspect_ratio
    - interpolation_engine

    The function also keeps nested preset structure intact and updates nested
    values to stay consistent with normalized top-level fields.

    Args:
        preset_name: Built-in preset name.
        merged: Result of ``merge_user_settings``.

    Returns:
        Fully normalized runtime dictionary.

    Raises:
        ValueError: If ``preset_name`` or critical runtime fields are invalid.
    """
    normalized_preset_name = _validate_preset_name_or_raise(preset_name)
    runtime = _deep_copy_dict(merged)

    runtime["name"] = normalized_preset_name
    runtime["preset_name"] = normalized_preset_name
    runtime["display_name"] = str(runtime.get("display_name", normalized_preset_name))

    # Target FPS
    raw_target_fps = runtime.get("target_fps", DEFAULT_RUNTIME_VALUES["target_fps"] if "target_fps" in DEFAULT_RUNTIME_VALUES else None)
    target_fps = _coerce_int(raw_target_fps, default={"gaming": 120, "tutorial": 60, "cinematic": 24}[normalized_preset_name])
    if target_fps not in SUPPORTED_TARGET_FPS:
        # Clamp to a preset-specific sane fallback instead of propagating a bad value.
        target_fps = {"gaming": 120, "tutorial": 60, "cinematic": 24}[normalized_preset_name]
    runtime["target_fps"] = target_fps

    # Codec
    codec = str(runtime.get("codec", DEFAULT_RUNTIME_VALUES["codec"])).strip().lower()
    if codec not in SUPPORTED_CODECS:
        codec = "h265" if normalized_preset_name == "cinematic" else "h264"
    runtime["codec"] = codec

    # Output aspect ratio
    default_aspect_ratio = "21:9" if normalized_preset_name == "cinematic" else "16:9"
    output_aspect_ratio = str(
        runtime.get("output_aspect_ratio", default_aspect_ratio)
    ).strip()
    if output_aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
        output_aspect_ratio = default_aspect_ratio
    runtime["output_aspect_ratio"] = output_aspect_ratio

    # Zoom scale
    default_zoom = {
        "gaming": 1.3,
        "tutorial": 1.1,
        "cinematic": 1.0,
    }[normalized_preset_name]
    zoom_scale = _coerce_float(runtime.get("zoom_scale"), default=default_zoom, minimum=1.0, maximum=2.0)
    runtime["zoom_scale"] = zoom_scale

    # Cutting aggressiveness
    default_cut_aggressiveness = {
        "gaming": 0.7,
        "tutorial": 0.85,
        "cinematic": 0.35,
    }[normalized_preset_name]
    cut_aggressiveness = _coerce_float(
        runtime.get("cut_aggressiveness", _get_nested(runtime, ["cutting", "aggressiveness"], default_cut_aggressiveness)),
        default=default_cut_aggressiveness,
        minimum=0.0,
        maximum=1.0,
    )
    runtime["cut_aggressiveness"] = cut_aggressiveness
    _set_nested(runtime, ["cutting", "aggressiveness"], cut_aggressiveness)

    # Noise reduction
    nested_noise_reduction = _get_nested(runtime, ["audio", "noise_reduction"], None)
    if nested_noise_reduction is None:
        nested_noise_reduction = _get_nested(runtime, ["audio_chain", "noise_reduction"], None)

    default_noise_reduction = normalized_preset_name in {"gaming", "tutorial"}
    noise_reduction_enabled = _coerce_bool(
        runtime.get("noise_reduction_enabled", nested_noise_reduction),
        default=default_noise_reduction,
    )
    runtime["noise_reduction_enabled"] = noise_reduction_enabled
    if isinstance(runtime.get("audio"), dict):
        runtime["audio"]["noise_reduction"] = noise_reduction_enabled
    if isinstance(runtime.get("audio_chain"), dict):
        runtime["audio_chain"]["noise_reduction"] = noise_reduction_enabled

    # Subtitles
    nested_subtitles_enabled = _get_nested(runtime, ["subtitles", "enabled"], None)
    default_subtitles_enabled = normalized_preset_name == "tutorial"
    subtitles_enabled = _coerce_bool(
        runtime.get("subtitles_enabled", nested_subtitles_enabled),
        default=default_subtitles_enabled,
    )
    runtime["subtitles_enabled"] = subtitles_enabled
    _set_nested(runtime, ["subtitles", "enabled"], subtitles_enabled)

    if normalized_preset_name == "tutorial":
        _set_nested(
            runtime,
            ["subtitles", "model"],
            str(_get_nested(runtime, ["subtitles", "model"], get_settings().whisper_model)),
        )
        _set_nested(runtime, ["subtitles", "format"], str(_get_nested(runtime, ["subtitles", "format"], "srt")))

    # Interpolation engine defaults and alignment with target FPS / preset rules.
    interpolation_engine = str(runtime.get("interpolation_engine", "")).strip().lower()
    if not interpolation_engine:
        interpolation_engine = {
            "gaming": "rife",
            "tutorial": "minterpolate",
            "cinematic": "none",
        }[normalized_preset_name]

    if normalized_preset_name == "cinematic":
        interpolation_engine = "none"
        runtime["target_fps"] = 24
    elif normalized_preset_name == "tutorial" and runtime["target_fps"] == 24:
        # Tutorial is primarily designed around smoother presentation. If user
        # selected 24 fps, keep the value but use a compatible interpolation mode.
        interpolation_engine = "minterpolate"
    elif normalized_preset_name == "gaming" and runtime["target_fps"] in {60, 120}:
        interpolation_engine = "rife"

    runtime["interpolation_engine"] = interpolation_engine

    # Mirror zoom into nested preset sections so downstream stages can rely on
    # either top-level or nested values.
    if isinstance(runtime.get("crosshair_tracking"), dict):
        runtime["crosshair_tracking"]["zoom_in_scale"] = zoom_scale

    if isinstance(runtime.get("active_area_zoom"), dict):
        # Do not decrease preset max_zoom below 1.0.
        runtime["active_area_zoom"]["max_zoom"] = max(1.0, zoom_scale)

    # Helpful fully-defined booleans for runtime consumers.
    runtime["enable_motion_blur"] = _coerce_bool(
        runtime.get("enable_motion_blur"),
        default=normalized_preset_name == "gaming",
    )
    runtime["remove_fillers"] = _coerce_bool(
        runtime.get("remove_fillers"),
        default=normalized_preset_name == "tutorial",
    )

    # Ensure common nested sections exist as dictionaries where worker code may
    # expect them.
    for section_name in (
        "cutting",
        "audio",
        "audio_chain",
        "subtitles",
        "transitions",
        "stabilization",
        "crosshair_tracking",
        "highlight_detection",
        "background_blur",
        "active_area_zoom",
        "color",
        "crop",
        "letterbox",
    ):
        if section_name not in runtime or runtime[section_name] is None:
            runtime[section_name] = {}
        elif not isinstance(runtime[section_name], dict):
            runtime[section_name] = {}

    # Preset-specific safety defaults.
    if normalized_preset_name == "gaming":
        runtime["transitions"].setdefault("type", "whip")
        runtime["transitions"].setdefault("duration_ms", 180)
        runtime["highlight_detection"].setdefault("enabled", True)
        runtime["highlight_detection"].setdefault("audio_energy_threshold", 0.7)
        runtime["highlight_detection"].setdefault("min_gap_seconds", 2.0)
        runtime["crosshair_tracking"].setdefault("enabled", True)
        runtime["crosshair_tracking"].setdefault("roi_size", 200)
        runtime["crosshair_tracking"].setdefault("template_match_threshold", 0.6)
        runtime["crosshair_tracking"].setdefault("crop_scale", 0.7)
        runtime["crosshair_tracking"].setdefault("zoom_in_duration_seconds", 0.5)
        runtime["crosshair_tracking"].setdefault("zoom_hold_seconds", 2.0)
        runtime["crosshair_tracking"].setdefault("easing", "ease_in_out")
        runtime["audio"].setdefault("target_lufs", -14)

    elif normalized_preset_name == "tutorial":
        runtime["background_blur"].setdefault("enabled", True)
        runtime["background_blur"].setdefault("face_scale_factor", 1.1)
        runtime["background_blur"].setdefault("min_neighbors", 5)
        runtime["background_blur"].setdefault("gaussian_kernel", 51)
        runtime["active_area_zoom"].setdefault("enabled", True)
        runtime["active_area_zoom"].setdefault("max_zoom", max(1.0, zoom_scale))
        runtime["stabilization"].setdefault("enabled", True)
        runtime["stabilization"].setdefault("shakiness", 10)
        runtime["stabilization"].setdefault("accuracy", 15)
        runtime["stabilization"].setdefault("smoothing", 30)
        runtime["audio_chain"].setdefault("target_lufs", -14)

    elif normalized_preset_name == "cinematic":
        runtime["transitions"].setdefault("type", "crossfade")
        runtime["transitions"].setdefault("duration_ms", 300)
        runtime["stabilization"].setdefault("enabled", True)
        runtime["stabilization"].setdefault("shakiness", 8)
        runtime["stabilization"].setdefault("accuracy", 15)
        runtime["stabilization"].setdefault("smoothing", 25)
        runtime["letterbox"].setdefault("enabled", True)
        runtime["letterbox"].setdefault("bar_size_percent", 10)
        runtime["color"].setdefault("lut_name", "teal_orange")
        runtime["color"].setdefault("contrast", 1.08)
        runtime["color"].setdefault("saturation", 1.12)
        runtime["audio"].setdefault("target_lufs", -16)
        runtime["audio"].setdefault("reverb", True)

    # Guarantee JSON-serializable plain structure by round-tripping through
    # ``json``. This protects the job snapshot stored by the service layer from
    # accidental non-serializable values introduced by future changes.
    try:
        return json.loads(json.dumps(runtime, ensure_ascii=False))
    except Exception as exc:  # pragma: no cover - defensive branch
        logger.error(
            "preset_runtime_serialization_failed",
            preset_name=normalized_preset_name,
            error=str(exc),
        )
        raise ValueError("INVALID_SETTINGS") from exc