"""Strict parser for Manual Inputs Schema v1.0."""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .models import DefaultInputs, ElementOverrides, ManualInputs, ProjectInputs


class ManualInputsError(ValueError):
    """Client-safe validation failure at the API/pipeline boundary."""


_SCHEMA_VERSION = "1.0"
_TOP_KEYS = {
    "schema_version", "project", "defaults", "element_overrides",
    "allow_unmatched_overrides",
}
_PROJECT_RANGES = {
    "default_storey_height_mm": (500.0, 10000.0),
    "finished_floor_level_mm": (-100000.0, 100000.0),
    "floor_thickness_mm": (50.0, 1000.0),
}
_DEFAULT_RANGES = {
    "ceiling_height_mm": (2000.0, 6000.0),
    "wall_height_mm": (500.0, 6000.0),
    "door_height_mm": (1800.0, 3000.0),
    "window_width_mm": (300.0, 5000.0),
    "window_height_mm": (200.0, 3000.0),
    "window_sill_height_mm": (0.0, 2000.0),
    "floor_thickness_mm": (50.0, 600.0),
}
_ALIASES = {
    "wall_height": "wall_height_mm",
    "door_height": "door_height_mm",
    "window_width": "window_width_mm",
    "window_height": "window_height_mm",
    "window_sill_height": "window_sill_height_mm",
    "floor_thickness": "floor_thickness_mm",
}
_OVERRIDE_RANGES = {
    "windows": {
        "width_mm": (300.0, 5000.0),
        "height_mm": (200.0, 3000.0),
        "sill_height_mm": (0.0, 3000.0),
    },
    "doors": {
        "width_mm": (300.0, 5000.0),
        "height_mm": (500.0, 5000.0),
    },
    "walls": {
        "height_mm": (500.0, 10000.0),
        "thickness_mm": (20.0, 2000.0),
    },
}


def _object(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManualInputsError(f"{path} must be a JSON object")
    return dict(value)


def _number(value: Any, path: str, bounds: tuple[float, float]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ManualInputsError(f"{path} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ManualInputsError(f"{path} must be a finite number") from exc
    if not math.isfinite(number):
        raise ManualInputsError(f"{path} must be finite")
    lo, hi = bounds
    if not lo <= number <= hi:
        raise ManualInputsError(
            f"{path} must be between {lo:g} and {hi:g} mm, got {number:g}"
        )
    return number


def _reject_unknown(block: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ManualInputsError(f"Unknown key(s) in {path}: {unknown}")


def _parse_defaults(block: Mapping[str, Any], path: str = "manual_inputs.defaults") -> DefaultInputs:
    normalized: dict[str, Any] = {}
    for raw_key, value in block.items():
        key = _ALIASES.get(raw_key, raw_key)
        if key in normalized:
            raise ManualInputsError(f"Duplicate parameter after alias normalization: {key}")
        normalized[key] = value
    _reject_unknown(normalized, set(_DEFAULT_RANGES), path)
    values = {
        key: _number(value, f"{path}.{key}", _DEFAULT_RANGES[key])
        for key, value in normalized.items()
    }
    out = DefaultInputs(**values)
    wall = out.wall_height_mm or out.ceiling_height_mm
    if wall is not None:
        if (out.window_sill_height_mm is not None
                and out.window_height_mm is not None
                and out.window_sill_height_mm + out.window_height_mm > wall):
            raise ManualInputsError(
                f"{path}: window_sill_height_mm + window_height_mm exceeds wall height"
            )
        if out.door_height_mm is not None and out.door_height_mm > wall:
            raise ManualInputsError(f"{path}: door_height_mm exceeds wall height")
    return out


def _parse_overrides(block: Mapping[str, Any]) -> ElementOverrides:
    _reject_unknown(block, set(_OVERRIDE_RANGES), "manual_inputs.element_overrides")
    parsed: dict[str, dict[str, dict[str, float]]] = {
        "windows": {}, "doors": {}, "walls": {}
    }
    for collection, allowed in _OVERRIDE_RANGES.items():
        rows = _object(block.get(collection), f"manual_inputs.element_overrides.{collection}")
        for element_id, raw_values in rows.items():
            if not isinstance(element_id, str) or not element_id.strip():
                raise ManualInputsError(
                    f"manual_inputs.element_overrides.{collection} keys must be non-empty IDs"
                )
            values = _object(
                raw_values,
                f"manual_inputs.element_overrides.{collection}.{element_id}",
            )
            _reject_unknown(
                values,
                set(allowed),
                f"manual_inputs.element_overrides.{collection}.{element_id}",
            )
            if not values:
                raise ManualInputsError(
                    f"manual_inputs.element_overrides.{collection}.{element_id} cannot be empty"
                )
            parsed[collection][element_id] = {
                key: _number(
                    value,
                    f"manual_inputs.element_overrides.{collection}.{element_id}.{key}",
                    allowed[key],
                )
                for key, value in values.items()
            }
            v = parsed[collection][element_id]
            if collection == "windows" and {
                "sill_height_mm", "height_mm"
            }.issubset(v) and v["sill_height_mm"] + v["height_mm"] > 10000:
                raise ManualInputsError(
                    f"window override {element_id}: sill + height is not physically plausible"
                )
    return ElementOverrides(**parsed)


def parse_manual_inputs(raw: Any) -> ManualInputs:
    """Parse the versioned Manual Inputs Schema v1.0 contract."""
    if raw is None or raw == "" or raw == {}:
        return ManualInputs()
    if isinstance(raw, ManualInputs):
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManualInputsError(
                f"manual_inputs is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc
    if not isinstance(raw, Mapping):
        raise ManualInputsError(
            f"manual_inputs must be a JSON object, got {type(raw).__name__}"
        )
    data = dict(raw)

    _reject_unknown(data, _TOP_KEYS, "manual_inputs")
    version = str(data.get("schema_version", _SCHEMA_VERSION))
    if version != _SCHEMA_VERSION:
        raise ManualInputsError(
            f"Unsupported manual input schema_version {version!r}; supported: {_SCHEMA_VERSION}"
        )

    project_raw = _object(data.get("project"), "manual_inputs.project")
    _reject_unknown(project_raw, set(_PROJECT_RANGES), "manual_inputs.project")
    project = ProjectInputs(**{
        key: _number(value, f"manual_inputs.project.{key}", _PROJECT_RANGES[key])
        for key, value in project_raw.items()
    })

    defaults = _parse_defaults(_object(data.get("defaults"), "manual_inputs.defaults"))
    overrides = _parse_overrides(
        _object(data.get("element_overrides"), "manual_inputs.element_overrides")
    )
    allow = data.get("allow_unmatched_overrides", False)
    if not isinstance(allow, bool):
        raise ManualInputsError("manual_inputs.allow_unmatched_overrides must be boolean")

    wall = defaults.wall_height_mm or defaults.ceiling_height_mm or project.default_storey_height_mm
    if wall is not None:
        if (defaults.window_sill_height_mm is not None
                and defaults.window_height_mm is not None
                and defaults.window_sill_height_mm + defaults.window_height_mm > wall):
            raise ManualInputsError(
                "manual_inputs: default window sill + height exceeds resolved wall/storey height"
            )
        if defaults.door_height_mm is not None and defaults.door_height_mm > wall:
            raise ManualInputsError(
                "manual_inputs: default door height exceeds resolved wall/storey height"
            )

    return ManualInputs(
        schema_version=version,
        project=project,
        defaults=defaults,
        element_overrides=overrides,
        allow_unmatched_overrides=allow,
    )
