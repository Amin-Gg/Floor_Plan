"""Strict Manual Inputs v1 adapter for the Stage-1 exporter boundary."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Mapping


class ManualInputsError(ValueError):
    pass


SCHEMA_VERSION = "1.0"
SYSTEM_DEFAULTS = {
    "wall_height_mm": 2800.0,
    "door_height_mm": 2100.0,
    "window_height_mm": 1200.0,
    "window_sill_height_mm": 900.0,
    "floor_thickness_mm": 200.0,
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
_TOP_KEYS = {"schema_version", "project", "defaults", "element_overrides", "allow_unmatched_overrides"}


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManualInputsError("manual_inputs contains a non-finite number")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(_normalise(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _obj(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManualInputsError(f"{path} must be a JSON object")
    return dict(value)


def _reject_unknown(block: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ManualInputsError(f"Unknown key(s) in {path}: {unknown}")


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
        raise ManualInputsError(f"{path} must be between {lo:g} and {hi:g} mm, got {number:g}")
    return number


def parse_manual_inputs(raw: Any) -> dict[str, Any]:
    """Return a strict, canonical, JSON-safe Manual Inputs v1 payload."""
    if raw is None or raw == "" or raw == {}:
        raw = {"schema_version": SCHEMA_VERSION}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManualInputsError(
                f"manual_inputs is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from exc
    data = _obj(raw, "manual_inputs")
    _reject_unknown(data, _TOP_KEYS, "manual_inputs")
    version = str(data.get("schema_version", SCHEMA_VERSION))
    if version != SCHEMA_VERSION:
        raise ManualInputsError(f"Unsupported manual input schema_version {version!r}; supported: {SCHEMA_VERSION}")

    project_raw = _obj(data.get("project"), "manual_inputs.project")
    _reject_unknown(project_raw, set(_PROJECT_RANGES), "manual_inputs.project")
    project = {k: _number(v, f"manual_inputs.project.{k}", _PROJECT_RANGES[k])
               for k, v in project_raw.items()}

    defaults_raw = _obj(data.get("defaults"), "manual_inputs.defaults")
    _reject_unknown(defaults_raw, set(_DEFAULT_RANGES), "manual_inputs.defaults")
    defaults = {k: _number(v, f"manual_inputs.defaults.{k}", _DEFAULT_RANGES[k])
                for k, v in defaults_raw.items()}

    overrides_raw = _obj(data.get("element_overrides"), "manual_inputs.element_overrides")
    _reject_unknown(overrides_raw, set(_OVERRIDE_RANGES), "manual_inputs.element_overrides")
    overrides: dict[str, dict[str, dict[str, float]]] = {"windows": {}, "doors": {}, "walls": {}}
    for collection, rules in _OVERRIDE_RANGES.items():
        rows = _obj(overrides_raw.get(collection), f"manual_inputs.element_overrides.{collection}")
        for element_id, values_raw in rows.items():
            if not isinstance(element_id, str) or not element_id.strip():
                raise ManualInputsError(f"manual_inputs.element_overrides.{collection} keys must be non-empty IDs")
            values = _obj(values_raw, f"manual_inputs.element_overrides.{collection}.{element_id}")
            _reject_unknown(values, set(rules), f"manual_inputs.element_overrides.{collection}.{element_id}")
            if not values:
                raise ManualInputsError(f"manual_inputs.element_overrides.{collection}.{element_id} cannot be empty")
            overrides[collection][element_id] = {
                key: _number(value, f"manual_inputs.element_overrides.{collection}.{element_id}.{key}", rules[key])
                for key, value in values.items()
            }

    allow = data.get("allow_unmatched_overrides", False)
    if not isinstance(allow, bool):
        raise ManualInputsError("manual_inputs.allow_unmatched_overrides must be boolean")

    wall = defaults.get("wall_height_mm") or defaults.get("ceiling_height_mm") or project.get("default_storey_height_mm")
    if wall is not None:
        sill = defaults.get("window_sill_height_mm")
        height = defaults.get("window_height_mm")
        if sill is not None and height is not None and sill + height > wall:
            raise ManualInputsError("manual_inputs: default window sill + height exceeds resolved wall/storey height")
        if defaults.get("door_height_mm") is not None and defaults["door_height_mm"] > wall:
            raise ManualInputsError("manual_inputs: default door height exceeds resolved wall/storey height")

    return {
        "schema_version": version,
        "project": project,
        "defaults": defaults,
        "element_overrides": overrides,
        "allow_unmatched_overrides": allow,
    }


def _identity_keys(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for key in ("id", "source_id", "ifc_guid", "internal_id"):
        value = row.get(key)
        if value is not None and str(value):
            keys.add(str(value))
    identity = row.get("_identity")
    if isinstance(identity, Mapping):
        for value in identity.values():
            if value is not None and str(value):
                keys.add(str(value))
    return keys


def _record(row: dict[str, Any], field: str, value: float, source: str,
            confidence: float | None, *, override_id: str | None = None) -> None:
    resolution = row.setdefault("_manual_input_resolution", {})
    history = list(resolution.get(field, {}).get("override_history", []))
    if override_id:
        history.append({"source": "element_override", "override_id": override_id, "value": value, "unit": "mm"})
    resolution[field] = {
        "value": float(value), "unit": "mm", "source": source,
        "confidence": confidence, "override_history": history,
    }


def resolve_manual_inputs(bim_data: Mapping[str, Any], raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve Manual Inputs into a deep-copied BIM model before IFC export.

    Official Stage-1 precedence is element override > operator default > existing
    measured property > system fallback. Every resolution is stamped so the IFC
    and engine can prove which values were asserted rather than measured.
    """
    manual = parse_manual_inputs(raw)
    out = deepcopy(dict(bim_data))
    source_payload_sha256 = canonical_json_sha256(out)
    defaults = dict(manual["defaults"])
    project = dict(manual["project"])
    if "wall_height_mm" not in defaults and "ceiling_height_mm" in defaults:
        defaults["wall_height_mm"] = defaults["ceiling_height_mm"]
    if "wall_height_mm" not in defaults and project.get("default_storey_height_mm") is not None:
        defaults["wall_height_mm"] = project["default_storey_height_mm"]
    if "floor_thickness_mm" not in defaults and project.get("floor_thickness_mm") is not None:
        defaults["floor_thickness_mm"] = project["floor_thickness_mm"]

    provided_map = {
        "wall_height_mm": "wall_height", "door_height_mm": "door_height",
        "window_height_mm": "window_height", "window_sill_height_mm": "window_sill_height",
        "floor_thickness_mm": "floor_thickness",
    }
    legacy = dict(out.get("building_params") or {})
    resolved_defaults: dict[str, float] = {}
    sources: dict[str, str] = {}
    for canonical, fallback in SYSTEM_DEFAULTS.items():
        legacy_key = provided_map[canonical]
        if canonical in defaults:
            value, source, confidence = defaults[canonical], "operator_default", 1.0
        elif legacy.get(legacy_key) is not None:
            value = float(legacy[legacy_key])
            source = "model_property" if legacy_key in set(legacy.get("_provided") or []) else "system_fallback"
            confidence = 1.0 if source == "model_property" else None
        else:
            value, source, confidence = fallback, "system_fallback", None
        resolved_defaults[canonical] = float(value)
        sources[canonical] = source

    provided = sorted(
        provided_map[k]
        for k in provided_map
        if sources[k] in {"operator_default", "model_property"}
    )
    out["building_params"] = {
        "wall_height": resolved_defaults["wall_height_mm"],
        "door_height": resolved_defaults["door_height_mm"],
        "window_height": resolved_defaults["window_height_mm"],
        "window_sill_height": resolved_defaults["window_sill_height_mm"],
        "floor_thickness": resolved_defaults["floor_thickness_mm"],
        "_provided": provided,
    }
    if project.get("finished_floor_level_mm") is not None:
        out.setdefault("coordinate_system", {})["level_elevation"] = project["finished_floor_level_mm"]

    collection_config = {
        "walls": {
            "height": ("height_mm", "wall_height_mm"),
            "thickness": ("thickness_mm", None),
        },
        "doors": {
            "width": ("width_mm", None),
            "height": ("height_mm", "door_height_mm"),
        },
        "windows": {
            "width": ("width_mm", "window_width_mm"),
            "height": ("height_mm", "window_height_mm"),
            "sill_height": ("sill_height_mm", "window_sill_height_mm"),
        },
    }
    unmatched: list[dict[str, str]] = []
    override_maps = manual["element_overrides"]
    for collection, fields in collection_config.items():
        rows = list(out.get(collection) or [])
        matched: set[str] = set()
        for row in rows:
            keys = _identity_keys(row)
            matching = [oid for oid in override_maps[collection] if oid in keys]
            if len(matching) > 1:
                raise ManualInputsError(f"Multiple override keys refer to the same {collection[:-1]}: {matching}")
            override_id = matching[0] if matching else None
            override = override_maps[collection].get(override_id, {}) if override_id else {}
            if override_id:
                matched.add(override_id)
            for target, (override_field, default_field) in fields.items():
                if override_field in override:
                    value, source, confidence = override[override_field], "element_override", 1.0
                elif default_field and default_field in defaults:
                    value, source, confidence = defaults[default_field], "operator_default", 1.0
                elif row.get(target) is not None:
                    value, source, confidence = float(row[target]), "model_property", 1.0
                elif default_field and default_field in resolved_defaults:
                    value, source, confidence = resolved_defaults[default_field], sources[default_field], None
                else:
                    continue
                row[target] = float(value)
                _record(row, target, float(value), source, confidence, override_id=override_id)
            if collection == "windows" and override_id and "width_mm" in override:
                row["width_source"] = "user"
            if collection == "doors" and override_id and "width_mm" in override:
                row["width_source"] = "user"

        for override_id in override_maps[collection]:
            if override_id not in matched:
                if not manual["allow_unmatched_overrides"]:
                    raise ManualInputsError(f"Override ID {override_id!r} does not match any {collection[:-1]}")
                unmatched.append({"collection": collection, "element_id": override_id})

    wall_index = {key: wall for wall in out.get("walls", []) for key in _identity_keys(wall)}
    for door in out.get("doors", []):
        host = wall_index.get(str(door.get("host_wall_id")))
        if host and float(door.get("height", 0)) > float(host.get("height", 0)):
            raise ManualInputsError(f"Resolved door {door.get('id')!r} height exceeds host wall height")
    for window in out.get("windows", []):
        host = wall_index.get(str(window.get("host_wall_id")))
        head = float(window.get("sill_height", 0)) + float(window.get("height", 0))
        if host and head > float(host.get("height", 0)):
            raise ManualInputsError(f"Resolved window {window.get('id')!r} sill + height exceeds host wall height")

    input_sha = canonical_json_sha256(manual)
    resolved_manifest = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "resolved_defaults": resolved_defaults,
        "sources": sources,
        "element_overrides": override_maps,
        "unmatched_overrides": unmatched,
    }
    resolved_sha = canonical_json_sha256(resolved_manifest)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "input_sha256": input_sha,
        "resolved_sha256": resolved_sha,
        "source_payload_sha256": source_payload_sha256,
        "provided": provided,
        "unmatched_overrides": unmatched,
        "resolved": resolved_manifest,
    }
    out["manual_inputs"] = metadata
    return out, metadata
