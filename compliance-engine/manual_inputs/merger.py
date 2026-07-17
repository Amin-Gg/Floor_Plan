"""Merge manual inputs into the canonical BuildingModel before Quality checks."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from domain.findings import Finding, FindingSeverity, FindingStage, Verdict
from domain.model import BuildingModel

from .models import ManualInputs, ManualMergeResult, ResolvedValue
from .parser import ManualInputsError

_CANONICAL_ALIASES = {
    "wall_height_mm": ("wall_height_mm", "wall_height"),
    "ceiling_height_mm": ("ceiling_height_mm",),
    "door_height_mm": ("door_height_mm", "door_height"),
    "window_width_mm": ("window_width_mm", "window_width"),
    "window_height_mm": ("window_height_mm", "window_height"),
    "window_sill_height_mm": ("window_sill_height_mm", "window_sill_height"),
    "floor_thickness_mm": ("floor_thickness_mm", "floor_thickness"),
}


def _identity_keys(element: Any) -> set[str]:
    identity = element.identity
    return {
        str(value)
        for value in (
            identity.internal_id,
            identity.ifc_guid,
            identity.source_id,
        )
        if value
    }


def _resolved_bucket(element: Any) -> dict[str, Any]:
    return element.provenance.setdefault("resolved_values", {})


def _record(element: Any, field: str, resolved: ResolvedValue) -> None:
    _resolved_bucket(element)[field] = resolved.to_dict()


def _current_parameter(values: dict[str, Any], provided: set[str],
                       canonical: str) -> tuple[Any, str | None]:
    aliases = _CANONICAL_ALIASES[canonical]
    for alias in aliases:
        if alias in values and values[alias] is not None:
            source = "model_property" if alias in provided else "system_fallback"
            return values[alias], source
    return None, None


def _set_parameter(model: BuildingModel, canonical: str, resolved: ResolvedValue) -> None:
    aliases = _CANONICAL_ALIASES[canonical]
    # Preserve the legacy vocabulary required by the deterministic checker.
    primary = aliases[-1] if canonical == "wall_height_mm" else aliases[0]
    model.parameters.values[primary] = resolved.value
    model.parameters.values[canonical] = resolved.value
    asserted = resolved.source in {
        "model_property", "operator_default", "element_override"
    }
    if asserted:
        model.parameters.provided.add(primary)
        model.parameters.provided.add(canonical)
    if canonical == "wall_height_mm":
        model.parameters.values["ceiling_height_mm"] = resolved.value
        if asserted:
            model.parameters.provided.add("ceiling_height_mm")
    model.parameters.provided_marker_present = True
    meta = model.extras.setdefault("_manual_input_resolution", {})
    meta.setdefault("building_parameters", {})[canonical] = resolved.to_dict()


def _quality_finding(model: BuildingModel, code: str, message: str, *, element: Any = None,
                     expected: Any = None, actual: Any = None) -> Finding:
    identity = getattr(element, "identity", None)
    return Finding(
        article_id=code,
        verdict=Verdict.NOT_EVALUATED,
        message=message,
        object=type(element).__name__.lower() if element is not None else "manual_inputs",
        element_id=(identity.source_id or identity.ifc_guid or identity.internal_id)
        if identity else None,
        category=FindingStage.QUALITY.value,
        code=code,
        severity=FindingSeverity.ALERT,
        element_internal_id=identity.internal_id if identity else None,
        element_ifc_guid=identity.ifc_guid if identity else None,
        element_type=type(element).__name__ if element is not None else None,
        model_name=model.provenance.model_name,
        model_fingerprint=model.provenance.model_fingerprint,
        expected=expected,
        actual=actual,
        source="manual_inputs",
    )


def _apply_field(element: Any, attr: str, default_value: Any, override_value: Any) -> None:
    current = getattr(element, attr)
    if override_value is not None:
        resolved = ResolvedValue(override_value, "mm", "element_override", 1.0)
        setattr(element, attr, override_value)
        _record(element, attr, resolved)
    elif current is not None:
        _record(element, attr, ResolvedValue(current, "mm", "model_property", 1.0))
    elif default_value is not None:
        resolved = ResolvedValue(default_value, "mm", "operator_default", 1.0)
        setattr(element, attr, default_value)
        _record(element, attr, resolved)
    else:
        _record(element, attr, ResolvedValue(None, "mm", "system_fallback", None))


def _match(collection: Iterable[Any], override_id: str) -> Any | None:
    matches = [element for element in collection if override_id in _identity_keys(element)]
    if len(matches) > 1:
        raise ManualInputsError(
            f"Override ID {override_id!r} is ambiguous and matches {len(matches)} elements"
        )
    return matches[0] if matches else None


def merge_manual_inputs(model: BuildingModel, manual: ManualInputs) -> ManualMergeResult:
    """Return a deep-copied, enriched model and any non-blocking Quality findings."""
    merged = deepcopy(model)
    findings: list[Finding] = []
    metadata = {
        "schema_version": manual.schema_version,
        "unmatched_overrides": [],
    }

    defaults = manual.defaults.as_dict()
    if ("wall_height_mm" not in defaults
            and manual.project.default_storey_height_mm is not None):
        defaults["wall_height_mm"] = manual.project.default_storey_height_mm
    if ("floor_thickness_mm" not in defaults
            and manual.project.floor_thickness_mm is not None):
        defaults["floor_thickness_mm"] = manual.project.floor_thickness_mm

    # v1.0 precedence: trusted model property > operator default > fallback.
    original_values = dict(merged.parameters.values)
    original_provided = set(merged.parameters.provided)
    for canonical in _CANONICAL_ALIASES:
        model_value, model_source = _current_parameter(
            original_values, original_provided, canonical
        )
        operator_value = defaults.get(canonical)
        if model_value is not None and model_source == "model_property":
            resolved = ResolvedValue(model_value, "mm", "model_property", 1.0)
        elif operator_value is not None:
            resolved = ResolvedValue(operator_value, "mm", "operator_default", 1.0)
        elif model_value is not None:
            resolved = ResolvedValue(model_value, "mm", model_source or "system_fallback", None)
        else:
            continue
        _set_parameter(merged, canonical, resolved)

    if manual.project.finished_floor_level_mm is not None:
        merged.extras.setdefault("_manual_input_resolution", {})["finished_floor_level_mm"] = (
            ResolvedValue(
                manual.project.finished_floor_level_mm,
                "mm",
                "operator_default",
                1.0,
            ).to_dict()
        )
        # Current single-storey scope: fill only missing elevations.
        for storey in merged.storeys:
            if storey.elevation_mm is None:
                storey.elevation_mm = manual.project.finished_floor_level_mm
                _record(
                    storey,
                    "elevation_mm",
                    ResolvedValue(storey.elevation_mm, "mm", "operator_default", 1.0),
                )

    collections = {
        "windows": merged.windows,
        "doors": merged.doors,
        "walls": merged.walls,
    }
    defaults_by_collection = {
        "windows": {
            "width_mm": defaults.get("window_width_mm"),
            "height_mm": defaults.get("window_height_mm"),
            "sill_height_mm": defaults.get("window_sill_height_mm"),
        },
        "doors": {
            "width_mm": None,
            "height_mm": defaults.get("door_height_mm"),
        },
        "walls": {
            "height_mm": defaults.get("wall_height_mm"),
            "thickness_mm": None,
        },
    }

    override_maps = {
        "windows": manual.element_overrides.windows,
        "doors": manual.element_overrides.doors,
        "walls": manual.element_overrides.walls,
    }

    matched: dict[str, set[str]] = {k: set() for k in collections}
    for collection_name, elements in collections.items():
        override_map = override_maps[collection_name]
        for element in elements:
            matching_ids = [oid for oid in override_map if oid in _identity_keys(element)]
            if len(matching_ids) > 1:
                raise ManualInputsError(
                    f"Multiple override keys refer to the same {collection_name[:-1]}: {matching_ids}"
                )
            override = override_map[matching_ids[0]] if matching_ids else {}
            if matching_ids:
                matched[collection_name].add(matching_ids[0])
            for field, default_value in defaults_by_collection[collection_name].items():
                _apply_field(element, field, default_value, override.get(field))

        for override_id in override_map:
            if override_id in matched[collection_name]:
                continue
            if not manual.allow_unmatched_overrides:
                raise ManualInputsError(
                    f"Override ID {override_id!r} does not match any {collection_name[:-1]}"
                )
            metadata["unmatched_overrides"].append({
                "collection": collection_name,
                "element_id": override_id,
            })
            findings.append(_quality_finding(
                merged,
                "QC-MANUAL-OVERRIDE-001",
                f"Manual override {override_id!r} did not match any {collection_name[:-1]}; "
                "the override was ignored",
                expected=f"existing {collection_name[:-1]} identity",
                actual=override_id,
            ))

    # Cross-field checks after host-specific values have resolved.
    wall_index = {
        key: wall
        for wall in merged.walls
        for key in _identity_keys(wall)
    }
    for door in merged.doors:
        host = wall_index.get(str(door.host_wall_id)) if door.host_wall_id else None
        if host and host.height_mm is not None and door.height_mm is not None \
                and door.height_mm > host.height_mm:
            findings.append(_quality_finding(
                merged,
                "QC-MANUAL-FIT-001",
                "Resolved door height exceeds its host wall height",
                element=door,
                expected=f"<= {host.height_mm:g} mm",
                actual=door.height_mm,
            ))
    for window in merged.windows:
        host = wall_index.get(str(window.host_wall_id)) if window.host_wall_id else None
        if (host and host.height_mm is not None and window.height_mm is not None
                and window.sill_height_mm is not None
                and window.sill_height_mm + window.height_mm > host.height_mm):
            findings.append(_quality_finding(
                merged,
                "QC-MANUAL-FIT-002",
                "Resolved window sill plus height exceeds its host wall height",
                element=window,
                expected=f"<= {host.height_mm:g} mm",
                actual=window.sill_height_mm + window.height_mm,
            ))

    merged.extras.setdefault("_manual_inputs", {}).update(metadata)
    return ManualMergeResult(model=merged, findings=findings, metadata=metadata)
