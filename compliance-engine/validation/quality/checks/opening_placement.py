"""QC-PLACE-001..011 opening/host-wall consistency checks."""
from __future__ import annotations

from math import hypot
from typing import Optional

from domain.elements import Door, Wall, Window
from domain.findings import Finding
from domain.geometry import Point2D
from domain.model import BuildingModel

from ..context import QualityContext, element_aliases
from ..findings import quality_finding
from ..spatial import opening_space_connectivity


def _distance_to_segment(point: Point2D, wall: Wall) -> float:
    assert wall.start is not None and wall.end is not None
    px, py = point.x, point.y
    ax, ay = wall.start.x, wall.start.y
    bx, by = wall.end.x, wall.end.y
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return hypot(px - (ax + t * dx), py - (ay + t * dy))


def _wall_length(wall: Wall) -> float:
    if wall.start is None or wall.end is None:
        return 0.0
    return hypot(wall.end.x - wall.start.x, wall.end.y - wall.start.y)


def _projection_mm(wall: Wall, point: Point2D) -> Optional[float]:
    if wall.start is None or wall.end is None:
        return None
    ax, ay = wall.start.x, wall.start.y
    dx, dy = wall.end.x - ax, wall.end.y - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return None
    return ((point.x - ax) * dx + (point.y - ay) * dy) / (length2 ** 0.5)


def _display_id(element: Door | Window | Wall) -> str:
    identity = element.identity
    return identity.source_id or identity.ifc_guid or identity.internal_id


def _space_ids(spaces) -> set[str]:
    return {
        space.identity.source_id or space.identity.ifc_guid or space.identity.internal_id
        for space in spaces
    }


class OpeningPlacementCheck:
    code_prefix = "QC-PLACE"
    codes = tuple(f"QC-PLACE-{index:03d}" for index in range(1, 12))
    name = "opening_placement"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(model.doors or model.windows)

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        tolerances = context.tolerances

        wall_index: dict[str, Wall] = {}
        for wall in model.walls:
            for alias in element_aliases(wall):
                wall_index[alias] = wall

        per_wall_widths: dict[str, float] = {}
        per_wall_children: dict[str, list[str]] = {}
        per_wall_spans: dict[str, list[tuple[Door | Window, float, float]]] = {}

        openings: list[tuple[str, Door | Window]] = [
            *(("door", item) for item in model.doors),
            *(("window", item) for item in model.windows),
        ]

        for kind, opening in openings:
            eid = _display_id(opening)
            host_id = str(opening.host_wall_id) if opening.host_wall_id else None
            width = float(opening.width_mm or 0.0)

            if not host_id:
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-001",
                    f"{kind} {eid}: no host wall binding — the opening cannot "
                    "be anchored; wall-related clauses for it are unreliable",
                    element=opening,
                    object_type=kind,
                    expected="host_wall_id set",
                    actual="None",
                    source="quality.opening_placement",
                    details={"blocks_capabilities": ["opening_host_geometry"]},
                ))
                continue

            wall = wall_index.get(host_id)
            if wall is None:
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-002",
                    f"{kind} {eid}: host_wall_id '{host_id}' does not match any "
                    "wall in the model — dangling reference",
                    element=opening,
                    object_type=kind,
                    expected="host_wall_id resolves to a wall",
                    actual=host_id,
                    source="quality.opening_placement",
                    details={"blocks_capabilities": ["opening_host_geometry"]},
                ))
                continue

            wall_len = _wall_length(wall)
            wall_id = _display_id(wall)
            valid_wall_geometry = (
                wall.start is not None and wall.end is not None and wall_len > 1e-9
            )
            if not valid_wall_geometry:
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-008",
                    f"{kind} {eid}: host wall {wall_id} has missing or degenerate "
                    "geometry; placement cannot be evaluated.",
                    element=opening,
                    object_type=kind,
                    expected="host wall with two distinct finite endpoints",
                    actual={
                        "start": wall.start.to_dict() if wall.start else None,
                        "end": wall.end.to_dict() if wall.end else None,
                    },
                    source="quality.opening_placement",
                    details={"blocks_capabilities": ["opening_host_geometry"]},
                ))
            elif width > 0 and width > wall_len + tolerances["place_len_tol_mm"]:
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-003",
                    f"{kind} {eid}: width {width:.0f} mm exceeds host wall "
                    f"{wall_id} length {wall_len:.0f} mm — geometrically "
                    "impossible; fix the model or the scale",
                    element=opening,
                    object_type=kind,
                    expected=f"width <= {wall_len:.0f} mm",
                    actual=f"{width:.0f} mm",
                    source="quality.opening_placement",
                ))

            if opening.insertion_point is not None and valid_wall_geometry:
                distance = _distance_to_segment(opening.insertion_point, wall)
                allowed = float(wall.thickness_mm or 0.0) / 2.0 \
                    + tolerances["place_axis_tol_mm"]
                if distance > allowed:
                    findings.append(quality_finding(
                        model,
                        "QC-PLACE-004",
                        f"{kind} {eid}: insertion point is {distance:.0f} mm "
                        f"from host wall {wall_id} axis (allowed {allowed:.0f} "
                        "mm) — the opening is not on its wall",
                        element=opening,
                        object_type=kind,
                        expected=f"distance <= {allowed:.0f} mm",
                        actual=f"{distance:.0f} mm",
                        source="quality.opening_placement",
                    ))

                if width > 0:
                    position = (
                        opening.placement.center_offset_mm
                        if opening.placement is not None
                        else _projection_mm(wall, opening.insertion_point)
                    )
                    if position is not None:
                        per_wall_spans.setdefault(wall.identity.internal_id, []).append(
                            (opening, position, width)
                        )
                        start_offset = position - width / 2.0
                        end_offset = position + width / 2.0
                        endpoint_tol = float(tolerances["place_endpoint_tol_mm"])
                        if start_offset < -endpoint_tol or end_offset > wall_len + endpoint_tol:
                            findings.append(quality_finding(
                                model,
                                "QC-PLACE-007",
                                f"{kind} {eid}: opening span "
                                f"[{start_offset:.0f}, {end_offset:.0f}] mm "
                                f"extends beyond host wall {wall_id} endpoints "
                                f"[0, {wall_len:.0f}] mm.",
                                element=opening,
                                object_type=kind,
                                expected=(
                                    f"span within wall endpoints ±{endpoint_tol:.0f} mm"
                                ),
                                actual={
                                    "start_offset_mm": start_offset,
                                    "end_offset_mm": end_offset,
                                    "wall_length_mm": wall_len,
                                    "insertion_convention": (
                                        opening.placement.source_convention
                                        if opening.placement is not None else "center"
                                    ),
                                },
                                source="quality.opening_placement",
                            ))
            elif opening.insertion_point is None:
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-008",
                    f"{kind} {eid}: insertion point is missing; placement and "
                    "endpoint extent cannot be evaluated.",
                    element=opening,
                    object_type=kind,
                    expected="finite insertion point",
                    actual=None,
                    source="quality.opening_placement",
                    details={"blocks_capabilities": ["opening_position"]},
                ))

            if width > 0:
                key = wall.identity.internal_id
                per_wall_widths[key] = per_wall_widths.get(key, 0.0) + width
                per_wall_children.setdefault(key, []).append(eid)

            connectivity = opening_space_connectivity(
                model,
                opening,
                tolerance_mm=float(tolerances["space_connectivity_tol_mm"]),
            )
            explicit_ids = _space_ids(connectivity.explicit)
            derived_ids = _space_ids(connectivity.derived)

            if isinstance(opening, Window):
                internal_allowed = bool(
                    opening.extras.get("allow_internal")
                    or opening.properties.get("allow_internal")
                    or opening.properties.get("AllowInternal")
                )
                if len(connectivity.derived) >= 2 and not bool(opening.is_exterior)                         and not internal_allowed:
                    findings.append(quality_finding(
                        model,
                        "QC-PLACE-009",
                        f"window {eid}: geometry connects two internal spaces "
                        "but the window is not marked as an approved internal "
                        "opening.",
                        element=opening,
                        object_type="window",
                        expected="window connects one space to exterior",
                        actual=sorted(derived_ids),
                        source="quality.opening_placement",
                    ))
            else:
                if explicit_ids and derived_ids and explicit_ids != derived_ids:
                    findings.append(quality_finding(
                        model,
                        "QC-PLACE-010",
                        f"door {eid}: declared connected spaces disagree with "
                        "geometry-derived connectivity.",
                        element=opening,
                        object_type="door",
                        expected=sorted(derived_ids),
                        actual=sorted(explicit_ids),
                        source="quality.opening_placement",
                    ))

            vertical_tol = float(tolerances["place_vertical_tol_mm"])
            if wall.height_mm is not None and wall.height_mm > 0:
                if opening.height_mm is not None and opening.height_mm > wall.height_mm + vertical_tol:
                    findings.append(quality_finding(
                        model,
                        "QC-PLACE-011",
                        f"{kind} {eid}: height {opening.height_mm:.0f} mm exceeds "
                        f"host wall height {wall.height_mm:.0f} mm.",
                        element=opening,
                        object_type=kind,
                        expected=f"height <= {wall.height_mm:.0f} mm",
                        actual=f"{opening.height_mm:.0f} mm",
                        source="quality.opening_placement",
                    ))
                if isinstance(opening, Window) and opening.height_mm is not None \
                        and opening.sill_height_mm is not None:
                    top = opening.sill_height_mm + opening.height_mm
                    if top > wall.height_mm + vertical_tol:
                        findings.append(quality_finding(
                            model,
                            "QC-PLACE-011",
                            f"window {eid}: sill + height = {top:.0f} mm exceeds "
                            f"host wall height {wall.height_mm:.0f} mm.",
                            element=opening,
                            object_type="window",
                            expected=f"sill_height + height <= {wall.height_mm:.0f} mm",
                            actual=f"{top:.0f} mm",
                            source="quality.opening_placement",
                        ))

        walls_by_internal = {wall.identity.internal_id: wall for wall in model.walls}

        for wall_key, spans in per_wall_spans.items():
            spans.sort(key=lambda item: item[1])
            for (first, t1, w1), (second, t2, w2) in zip(spans, spans[1:]):
                # Preserve Stage-8's convention-agnostic guaranteed-overlap rule.
                gaps = [(t2 - t1) - w1 + alpha * (w2 - w1) for alpha in (0.0, -1.0)]
                guaranteed_overlap = -max(gaps)
                if guaranteed_overlap > tolerances["place_overlap_tol_mm"]:
                    wall = walls_by_internal[wall_key]
                    first_id, second_id = _display_id(first), _display_id(second)
                    findings.append(quality_finding(
                        model,
                        "QC-PLACE-006",
                        f"wall {_display_id(wall)}: openings {first_id} and "
                        f"{second_id} overlap by at least "
                        f"{guaranteed_overlap:.0f} mm under every insertion "
                        "convention — physically impossible; fix their "
                        "positions or widths",
                        element=wall,
                        object_type="wall",
                        expected="no guaranteed overlap between openings",
                        actual=(f">= {guaranteed_overlap:.0f} mm overlap "
                                f"({first_id} vs {second_id})"),
                        source="quality.opening_placement",
                    ))

        for wall_key, total in per_wall_widths.items():
            wall = walls_by_internal[wall_key]
            wall_len = _wall_length(wall)
            if wall_len > 0 and total > wall_len + tolerances["place_len_tol_mm"]:
                children = ", ".join(per_wall_children[wall_key])
                findings.append(quality_finding(
                    model,
                    "QC-PLACE-005",
                    f"wall {_display_id(wall)}: combined opening widths "
                    f"{total:.0f} mm exceed wall length {wall_len:.0f} mm "
                    f"(openings: {children}) — the openings cannot all fit; "
                    "fix the model or the scale",
                    element=wall,
                    object_type="wall",
                    expected=f"sum(widths) <= {wall_len:.0f} mm",
                    actual=f"{total:.0f} mm",
                    source="quality.opening_placement",
                ))

        return findings
