"""
validation/bim_checks.py
========================
PRE-EXPORT validator. Runs on the `bim_data` dict (the millimetre vector model
produced by /analyze) BEFORE it is handed to the IFC exporter.

Covers three of the four requested layers at the data level:
  • Geometric sanity   (no zero-length/zero-thickness, rooms closed, openings on host wall)
  • BIM completeness    (heights/thicknesses/widths present, host walls resolve, unique ids)
  • Code-readiness      (rooms typed + area, door widths, ceiling height — what the
                         Mabhas checker needs to run room/opening/safety rules)

IFC4-schema validity is checked separately in ifc_checks.py (it needs the .ifc).

Pure Python + math only — no model/ifcopenshell dependency, so it is cheap and
safe to call from anywhere (export gate today; /analyze later if desired).

bim_data shape (from services/bim_builder.py):
  walls[]   : {id, start_point[x,y,z], end_point[x,y,z], thickness, height, type, is_exterior}
  doors[]   : {id, host_wall_id, insertion_point[x,y,z], width, height, ...}
  windows[] : {id, host_wall_id, insertion_point[x,y,z], width, height, sill_height, ...}
  rooms[]   : {id, name, category, name_source, needs_review, polygon[[x,y]...],
               area_m2, perimeter_m, dimensions{length_mm,width_mm}, ...}
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .report import (
    ValidationReport, Severity,
    LAYER_GEOMETRY, LAYER_COMPLETENESS, LAYER_CODE_READINESS,
)

# ── Plausibility ranges (mm, and m² for area). Tune these to your stock. ──────
WALL_THICKNESS_MM = (50.0, 600.0)
DOOR_WIDTH_MM     = (500.0, 2000.0)
WINDOW_WIDTH_MM   = (300.0, 4000.0)
ROOM_AREA_M2      = (0.5, 500.0)

# Geometric tolerances
EPS_MM            = 1.0     # anything shorter than this counts as "zero"
ON_WALL_TOL_MM    = 150.0   # how far an opening centre may sit off the wall centerline
PARAM_TOL         = 0.02    # allowed overshoot of the [0,1] projection parameter


# ── small geometry helpers ───────────────────────────────────────────────────
def _xy(p) -> Optional[Tuple[float, float]]:
    try:
        return float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _project_param(pt, a, b) -> Tuple[float, float]:
    """Return (t, perpendicular_distance) of pt onto segment a→b.
    t is the parameter along the segment (0 at a, 1 at b), clamped-free."""
    ax, ay = a; bx, by = b; px, py = pt
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0:
        return 0.0, _dist(pt, a)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    fx, fy = ax + t * dx, ay + t * dy
    return t, math.hypot(px - fx, py - fy)


def _polygon_area_m2_from_mm(poly: List) -> float:
    """Shoelace area; polygon points are in mm, result in m²."""
    pts = [_xy(p) for p in poly]
    pts = [p for p in pts if p is not None]
    if len(pts) < 3:
        return 0.0
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    s = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]; x2, y2 = pts[i + 1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0 / 1_000_000.0   # mm² → m²


# ── main entry point ──────────────────────────────────────────────────────────
def validate_bim_data(bim_data: Dict[str, Any],
                      building_params: Optional[Dict[str, Any]] = None) -> ValidationReport:
    """Validate the bim_data dict. Returns a ValidationReport (stage='pre_export')."""
    r = ValidationReport(stage="pre_export")

    walls   = bim_data.get("walls",   []) or []
    doors   = bim_data.get("doors",   []) or []
    windows = bim_data.get("windows", []) or []
    rooms   = bim_data.get("rooms",   []) or []
    r.checked = {"walls": len(walls), "doors": len(doors),
                 "windows": len(windows), "rooms": len(rooms)}

    # index walls by id, capture endpoints + length
    wall_index: Dict[Any, Dict[str, Any]] = {}
    for w in walls:
        wid = w.get("id")
        a = _xy(w.get("start_point")); b = _xy(w.get("end_point"))
        wall_index[wid] = {"raw": w, "a": a, "b": b,
                           "len": (_dist(a, b) if a and b else 0.0)}

    _check_units(bim_data, r)
    _check_walls(walls, wall_index, r)
    _check_openings(doors, "Door", DOOR_WIDTH_MM, wall_index, r)
    _check_openings(windows, "Window", WINDOW_WIDTH_MM, wall_index, r)
    _check_rooms(rooms, r)
    return r


# ── per-section checks ────────────────────────────────────────────────────────
def _check_units(bim_data: Dict[str, Any], r: ValidationReport) -> None:
    units = (bim_data.get("coordinate_system") or {}).get("units")
    if units is None:
        r.warn("COMPLETE.UNITS.MISSING", LAYER_COMPLETENESS,
               "coordinate_system.units is missing; downstream tools assume mm.")
    elif str(units).lower() not in ("millimeters", "millimetre", "millimeter", "mm"):
        r.warn("COMPLETE.UNITS.UNEXPECTED", LAYER_COMPLETENESS,
               f"coordinate_system.units is '{units}', expected millimeters.")


def _check_walls(walls: List[Dict[str, Any]],
                 wall_index: Dict[Any, Dict[str, Any]], r: ValidationReport) -> None:
    if not walls:
        r.critical("COMPLETE.WALL.NONE", LAYER_COMPLETENESS,
                   "Model has no walls; an empty shell cannot be code-checked.")
        return

    seen_ids = set()
    endpoints: List[Tuple[Tuple[float, float], Any]] = []

    for w in walls:
        wid = w.get("id")
        eid = str(wid)

        # unique id
        if wid in seen_ids:
            r.warn("COMPLETE.WALL.DUP_ID", LAYER_COMPLETENESS,
                   f"Duplicate wall id '{wid}'.", element=eid)
        seen_ids.add(wid)

        # thickness present + plausible
        th = w.get("thickness")
        if th is None or not isinstance(th, (int, float)) or th <= 0:
            r.critical("COMPLETE.WALL.NO_THICKNESS", LAYER_COMPLETENESS,
                       f"Wall '{wid}' has no positive thickness.", element=eid)
        elif not (WALL_THICKNESS_MM[0] <= th <= WALL_THICKNESS_MM[1]):
            r.warn("GEOM.WALL.THICKNESS_RANGE", LAYER_GEOMETRY,
                   f"Wall '{wid}' thickness {th:.0f}mm is outside the plausible "
                   f"{int(WALL_THICKNESS_MM[0])}–{int(WALL_THICKNESS_MM[1])}mm "
                   f"range (possible scale error).", element=eid)

        # height present (code-readiness: habitable-room clear height)
        h = w.get("height")
        if h is None or not isinstance(h, (int, float)) or h <= 0:
            r.critical("COMPLETE.WALL.NO_HEIGHT", LAYER_COMPLETENESS,
                       f"Wall '{wid}' has no positive height.", element=eid)

        # geometry: zero-length
        wi = wall_index.get(wid, {})
        a, b, length = wi.get("a"), wi.get("b"), wi.get("len", 0.0)
        if a is None or b is None:
            r.critical("GEOM.WALL.BAD_POINTS", LAYER_GEOMETRY,
                       f"Wall '{wid}' has malformed start/end points.", element=eid)
        elif length < EPS_MM:
            r.critical("GEOM.WALL.ZERO_LENGTH", LAYER_GEOMETRY,
                       f"Wall '{wid}' is zero-length ({length:.1f}mm).", element=eid)
        else:
            endpoints.append((a, wid)); endpoints.append((b, wid))

    # connectivity: each wall endpoint should be near another wall's endpoint.
    # A floating wall (neither end touches anything) is suspicious → warning.
    JOIN_TOL = 200.0  # mm
    for w in walls:
        wid = w.get("id")
        wi = wall_index.get(wid, {})
        a, b = wi.get("a"), wi.get("b")
        if a is None or b is None:
            continue
        touched = False
        for (pt, other) in endpoints:
            if other == wid:
                continue
            if _dist(a, pt) <= JOIN_TOL or _dist(b, pt) <= JOIN_TOL:
                touched = True
                break
        if not touched and len(walls) > 1:
            r.warn("GEOM.WALL.FLOATING", LAYER_GEOMETRY,
                   f"Wall '{wid}' does not connect to any other wall "
                   f"(within {int(JOIN_TOL)}mm); model may be fragmented.",
                   element=str(wid))


def _check_openings(items: List[Dict[str, Any]], kind: str,
                    width_range: Tuple[float, float],
                    wall_index: Dict[Any, Dict[str, Any]], r: ValidationReport) -> None:
    seen_ids = set()
    for it in items:
        oid = it.get("id")
        eid = str(oid)

        if oid in seen_ids:
            r.warn(f"COMPLETE.{kind.upper()}.DUP_ID", LAYER_COMPLETENESS,
                   f"Duplicate {kind.lower()} id '{oid}'.", element=eid)
        seen_ids.add(oid)

        # width + height present and positive
        width = it.get("width")
        height = it.get("height")
        if width is None or not isinstance(width, (int, float)) or width <= 0:
            r.critical(f"COMPLETE.{kind.upper()}.NO_WIDTH", LAYER_COMPLETENESS,
                       f"{kind} '{oid}' has no positive width "
                       f"(needed for clear-width / egress checks).", element=eid)
            width = None
        elif not (width_range[0] <= width <= width_range[1]):
            r.warn(f"GEOM.{kind.upper()}.WIDTH_RANGE", LAYER_GEOMETRY,
                   f"{kind} '{oid}' width {width:.0f}mm is outside the plausible "
                   f"{int(width_range[0])}–{int(width_range[1])}mm range.", element=eid)
        if height is None or not isinstance(height, (int, float)) or height <= 0:
            r.critical(f"COMPLETE.{kind.upper()}.NO_HEIGHT", LAYER_COMPLETENESS,
                       f"{kind} '{oid}' has no positive height.", element=eid)

        # host wall resolves
        host = it.get("host_wall_id")
        if host is None:
            r.critical(f"COMPLETE.{kind.upper()}.NO_HOST", LAYER_COMPLETENESS,
                       f"{kind} '{oid}' has no host_wall_id; it cannot void a wall "
                       f"and will float free in the IFC.", element=eid)
            continue
        wi = wall_index.get(host)
        if wi is None:
            r.critical(f"COMPLETE.{kind.upper()}.HOST_MISSING", LAYER_COMPLETENESS,
                       f"{kind} '{oid}' references host wall '{host}' which does "
                       f"not exist.", element=eid)
            continue

        # geometry: opening centre lies ON its host wall, and fits within it
        a, b, length = wi.get("a"), wi.get("b"), wi.get("len", 0.0)
        ip = _xy(it.get("insertion_point"))
        if ip is None or a is None or b is None:
            r.warn(f"GEOM.{kind.upper()}.NO_PLACEMENT", LAYER_GEOMETRY,
                   f"{kind} '{oid}' has no usable insertion point to verify "
                   f"against its host wall.", element=eid)
            continue
        t, perp = _project_param(ip, a, b)
        if perp > ON_WALL_TOL_MM:
            r.critical(f"GEOM.{kind.upper()}.OFF_WALL", LAYER_GEOMETRY,
                       f"{kind} '{oid}' sits {perp:.0f}mm off the centerline of "
                       f"host wall '{host}' (>{int(ON_WALL_TOL_MM)}mm); the opening "
                       f"would be cut in the wrong place.", element=eid)
        elif not (-PARAM_TOL <= t <= 1 + PARAM_TOL):
            r.critical(f"GEOM.{kind.upper()}.PAST_END", LAYER_GEOMETRY,
                       f"{kind} '{oid}' projects beyond the ends of host wall "
                       f"'{host}' (t={t:.2f}).", element=eid)
        elif width and length > 0 and width > length:
            r.warn(f"GEOM.{kind.upper()}.WIDER_THAN_WALL", LAYER_GEOMETRY,
                   f"{kind} '{oid}' is wider ({width:.0f}mm) than its host wall "
                   f"({length:.0f}mm).", element=eid)


def _check_rooms(rooms: List[Dict[str, Any]], r: ValidationReport) -> None:
    if not rooms:
        r.warn("CODE.ROOM.NONE", LAYER_CODE_READINESS,
               "No rooms were extracted; area/room-type rules cannot be applied.")
        return

    seen_ids = set()
    for room in rooms:
        rid = room.get("id")
        eid = str(rid)

        if rid in seen_ids:
            r.warn("COMPLETE.ROOM.DUP_ID", LAYER_COMPLETENESS,
                   f"Duplicate room id '{rid}'.", element=eid)
        seen_ids.add(rid)

        # geometry: closed, valid polygon
        poly = room.get("polygon") or []
        pts = [p for p in (_xy(q) for q in poly) if p is not None]
        if len(pts) < 3:
            r.critical("GEOM.ROOM.DEGENERATE", LAYER_GEOMETRY,
                       f"Room '{rid}' polygon has fewer than 3 vertices.", element=eid)
            continue
        if pts[0] != pts[-1]:
            r.warn("GEOM.ROOM.NOT_CLOSED", LAYER_GEOMETRY,
                   f"Room '{rid}' polygon is not explicitly closed "
                   f"(first vertex != last).", element=eid)
        area = room.get("area_m2")
        if not isinstance(area, (int, float)) or area <= 0:
            # fall back to computing it so we don't false-alarm on a missing field
            area = _polygon_area_m2_from_mm(poly)
        if area <= 0:
            r.critical("GEOM.ROOM.ZERO_AREA", LAYER_GEOMETRY,
                       f"Room '{rid}' has zero area.", element=eid)
        elif not (ROOM_AREA_M2[0] <= area <= ROOM_AREA_M2[1]):
            r.warn("GEOM.ROOM.AREA_RANGE", LAYER_GEOMETRY,
                   f"Room '{rid}' area {area:.1f}m² is outside the plausible "
                   f"{ROOM_AREA_M2[0]}–{int(ROOM_AREA_M2[1])}m² range "
                   f"(possible scale error).", element=eid)

        # code-readiness: room must be typed for type-specific Mabhas rules
        needs_review = room.get("needs_review")
        category = (room.get("category") or "").strip().lower()
        if needs_review is True or category in ("", "unknown"):
            r.warn("CODE.ROOM.UNTYPED", LAYER_CODE_READINESS,
                   f"Room '{rid}' has no recognised type (OCR did not bind a "
                   f"name). Room-type rules (min bedroom area, kitchen/bath, "
                   f"corridor width) must be deferred to NEEDS_REVIEW.", element=eid)
