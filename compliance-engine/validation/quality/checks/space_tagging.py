"""Room/Space completeness and topology quality checks.

Legacy category findings are retained for compatibility while Phase 4 adds the
full QC-SPACE-001..010 contract.
"""
from __future__ import annotations

from typing import Any, Iterable

from domain.elements import Space
from domain.findings import Finding, FindingSeverity
from domain.geometry import Polygon2D
from domain.model import BuildingModel

from ..context import QualityContext, display_element_id, element_aliases
from ..findings import quality_finding
from ..spatial import opening_space_connectivity, wall_index


def _blocks(*capabilities: str) -> dict[str, Any]:
    return {"blocks_capabilities": list(capabilities)}


def _space_id(space: Space) -> str:
    return display_element_id(space)


def _expected_regions(model: BuildingModel) -> Iterable[dict[str, Any]]:
    for key in ("_enclosed_regions", "enclosed_regions", "detected_regions"):
        value = model.extras.get(key)
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    yield row
            return


def _region_is_tagged(region: Polygon2D, spaces: list[Space], tolerance: float) -> bool:
    centroid = region.centroid()
    for space in spaces:
        boundary = space.boundary
        if boundary is None or boundary.validation_errors():
            continue
        if centroid is not None and boundary.contains_or_touches(centroid, tolerance):
            return True
        ratio = region.coverage_ratio(boundary)
        if ratio is not None and ratio >= 0.90:
            return True
    return False


class SpaceTaggingCheck:
    code_prefix = "QC-SPACE"
    codes = (
        # Preserved Stage-8 category codes.
        "QC-SPACE-TAG-001",
        "QC-SPACE-TAG-002",
        # Phase-4 room/space completeness codes.
        "QC-SPACE-001",
        "QC-SPACE-002",
        "QC-SPACE-003",
        "QC-SPACE-004",
        "QC-SPACE-005",
        "QC-SPACE-006",
        "QC-SPACE-007",
        "QC-SPACE-008",
        "QC-SPACE-009",
        "QC-SPACE-010",
    )
    name = "space_tagging"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(
            model.spaces
            or model.doors
            or model.walls
            or any(True for _ in _expected_regions(model))
        )

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        threshold = context.threshold
        area_abs_tol = float(context.tolerances["space_area_abs_tol_m2"])
        area_rel_tol = float(context.tolerances["space_area_rel_tol"])
        boundary_tol = float(context.tolerances["space_boundary_tol_mm"])
        overlap_abs_tol = float(context.tolerances["space_overlap_abs_tol_m2"])
        connectivity_tol = float(context.tolerances["space_connectivity_tol_mm"])

        if not model.spaces and model.walls:
            findings.append(quality_finding(
                model,
                "QC-SPACE-001",
                "The model contains walls but no Space representation; room-"
                "specific and area-dependent clauses cannot be evaluated.",
                object_type="space",
                expected="at least one Space for enclosed rooms",
                actual="0 spaces",
                source="quality.room_space_tagging",
                severity=FindingSeverity.FAIL,
                details=_blocks("space_rules", "room_area", "room_topology"),
            ))

        storey_aliases = {
            alias
            for storey in model.storeys
            for alias in element_aliases(storey)
        }

        valid_spaces: list[Space] = []
        for space in model.spaces:
            sid = _space_id(space)

            if not space.identity.internal_id or (
                model.provenance.source_type == "ifc" and not space.identity.ifc_guid
            ):
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-002",
                    f"Space {sid}: stable identity is incomplete; IFC-sourced "
                    "spaces require both internal_id and IFC GlobalId.",
                    element=space,
                    object_type="space",
                    expected="stable internal_id and IFC GlobalId when applicable",
                    actual={
                        "internal_id": space.identity.internal_id or None,
                        "ifc_guid": space.identity.ifc_guid,
                    },
                    source="quality.room_space_tagging",
                    severity=FindingSeverity.FAIL,
                ))

            if not (space.name or space.local_name):
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-003",
                    f"Space {sid}: no human-readable name/tag is available.",
                    element=space,
                    object_type="space",
                    expected="space name or local name",
                    actual=None,
                    source="quality.room_space_tagging",
                    details=_blocks("named_space_rules"),
                ))

            source = space.category_source
            confidence = space.category_confidence
            raw = space.raw_type or ""
            if source == "unmapped" or not space.canonical_type:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-TAG-001",
                    f"Room {sid}: category '{raw}' is not in the canonical room "
                    "vocabulary — tag it (or add an alias) so its clauses can "
                    "be checked",
                    element=space,
                    object_type="room",
                    expected="canonical room_* category",
                    actual=raw or "(empty)",
                    source="quality.room_space_tagging",
                    details=_blocks("type_specific_room_rules"),
                ))
            elif source is not None and confidence is not None \
                    and float(confidence) < threshold:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-TAG-002",
                    f"Room {sid}: category '{raw}' mapped via '{source}' with "
                    f"confidence {float(confidence):.2f} < {threshold:.2f} — "
                    "confirm the tag",
                    element=space,
                    object_type="room",
                    expected=f"category_confidence >= {threshold:.2f}",
                    actual=f"{float(confidence):.2f}",
                    source="quality.room_space_tagging",
                    details=_blocks("type_specific_room_rules"),
                ))

            if space.area_m2 is None or space.area_m2 <= 0:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-004",
                    f"Space {sid}: floor area is missing or non-positive; "
                    "area-dependent clauses are not evaluable.",
                    element=space,
                    object_type="space",
                    expected="area_m2 > 0",
                    actual=space.area_m2,
                    source="quality.room_space_tagging",
                    details=_blocks("room_area", "area_ratio"),
                ))

            if space.boundary is None:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-005",
                    f"Space {sid}: boundary polygon is missing; spatial and "
                    "adjacency checks are not evaluable.",
                    element=space,
                    object_type="space",
                    expected="closed boundary polygon",
                    actual=None,
                    source="quality.room_space_tagging",
                    severity=FindingSeverity.FAIL,
                    details=_blocks("room_topology", "adjacency", "egress_geometry"),
                ))
            else:
                errors = space.boundary.validation_errors(boundary_tol)
                if errors:
                    findings.append(quality_finding(
                        model,
                        "QC-SPACE-006",
                        f"Space {sid}: boundary polygon is invalid "
                        f"({', '.join(errors)}).",
                        element=space,
                        object_type="space",
                        expected="finite, closed, non-self-intersecting polygon",
                        actual=errors,
                        source="quality.room_space_tagging",
                        severity=FindingSeverity.FAIL,
                        details={
                            **_blocks("room_topology", "adjacency", "egress_geometry"),
                            "geometry_errors": errors,
                        },
                    ))
                else:
                    valid_spaces.append(space)
                    derived = space.boundary.derived_area_m2()
                    if space.area_m2 is not None and space.area_m2 > 0:
                        delta = abs(derived - space.area_m2)
                        allowed = max(area_abs_tol, area_rel_tol * space.area_m2)
                        if delta > allowed:
                            findings.append(quality_finding(
                                model,
                                "QC-SPACE-004",
                                f"Space {sid}: declared area {space.area_m2:.3f} m² "
                                f"differs from boundary-derived area {derived:.3f} "
                                f"m² by {delta:.3f} m².",
                                element=space,
                                object_type="space",
                                expected=f"area delta <= {allowed:.3f} m²",
                                actual=f"{delta:.3f} m²",
                                source="quality.room_space_tagging",
                                details={
                                    **_blocks("room_area", "area_ratio"),
                                    "declared_area_m2": space.area_m2,
                                    "derived_area_m2": derived,
                                },
                            ))

            if not space.storey_id or space.storey_id not in storey_aliases:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-007",
                    f"Space {sid}: no valid storey assignment is available.",
                    element=space,
                    object_type="space",
                    expected="storey_id resolves to a model storey",
                    actual=space.storey_id,
                    source="quality.room_space_tagging",
                    details=_blocks("storey_specific_rules"),
                ))

        for row in _expected_regions(model):
            polygon = Polygon2D.from_value(row.get("polygon") or row.get("boundary"))
            if polygon is None or polygon.validation_errors(boundary_tol):
                continue
            if not _region_is_tagged(polygon, model.spaces, boundary_tol):
                region_id = row.get("id") or row.get("region_id") or "unidentified"
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-008",
                    f"Enclosed region {region_id} is not represented by a Space.",
                    element_id=str(region_id),
                    object_type="enclosed_region",
                    expected="region covered by a tagged Space",
                    actual="untagged",
                    source="quality.room_space_tagging",
                    details=_blocks("space_rules", "room_area", "room_topology"),
                ))

        walls = wall_index(model)
        for door in model.doors:
            connectivity = opening_space_connectivity(
                model, door, tolerance_mm=connectivity_tol
            )
            effective = connectivity.effective
            host = walls.get(str(door.host_wall_id)) if door.host_wall_id else None
            exterior_host = bool(host and host.is_exterior)
            invalid = bool(connectivity.unresolved_explicit_ids)
            invalid = invalid or len(effective) == 0 or len(effective) > 2
            invalid = invalid or (len(effective) == 1 and not exterior_host)
            if invalid:
                findings.append(quality_finding(
                    model,
                    "QC-SPACE-009",
                    f"Door {display_element_id(door)}: space connectivity is "
                    "missing, unresolved, or ambiguous.",
                    element=door,
                    object_type="door",
                    expected="two spaces, or one space plus an exterior boundary",
                    actual={
                        "explicit": [display_element_id(x) for x in connectivity.explicit],
                        "derived": [display_element_id(x) for x in connectivity.derived],
                        "unresolved": list(connectivity.unresolved_explicit_ids),
                        "host_is_exterior": exterior_host,
                    },
                    source="quality.room_space_tagging",
                    details=_blocks("door_adjacency", "egress_connectivity"),
                ))

        for index, first in enumerate(valid_spaces):
            for second in valid_spaces[index + 1:]:
                overlap = first.boundary.overlap_area_mm2(second.boundary) \
                    if first.boundary and second.boundary else None
                if overlap is None:
                    continue
                overlap_m2 = overlap / 1_000_000.0
                if overlap_m2 > overlap_abs_tol:
                    findings.append(quality_finding(
                        model,
                        "QC-SPACE-010",
                        f"Spaces {_space_id(first)} and {_space_id(second)} "
                        f"overlap by {overlap_m2:.3f} m².",
                        element=first,
                        object_type="space",
                        expected=f"overlap <= {overlap_abs_tol:.3f} m²",
                        actual=f"{overlap_m2:.3f} m² with {_space_id(second)}",
                        source="quality.room_space_tagging",
                        details={
                            **_blocks("room_topology", "area_allocation"),
                            "other_space_id": _space_id(second),
                            "overlap_area_m2": overlap_m2,
                        },
                    ))

        return findings
