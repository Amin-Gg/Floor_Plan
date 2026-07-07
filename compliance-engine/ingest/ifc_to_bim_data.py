"""
ingest/ifc_to_bim_data.py
=========================
WORKSTREAM B1 — Step 2 entry loader (lives in the compliance engine = Step 2).

`ifc_to_bim_data(ifc_path)` reconstructs the **exact `bim_data` dict shape** the
existing compliance agents (SpatialGraph, numeric/topology/opening/safety)
consume, reading ONLY the IFC file produced by Step 1. It is the precise inverse
of Step 1's `export/ifc_exporter.py` (§A3) and the single place the unit inverse
lives (IFC length unit → millimetres).

Each reconstructed element carries its provenance (`OriginalId`, `Source`,
`Confidence`, `NeedsReview`, `NameSource`, `ReviewReason`) so the §B2 review
pre-pass (ingest/review_prepass.py) can downgrade uncertain elements to
NEEDS_REVIEW.

This module reads the IFC; it runs no compliance logic. The contract is: Step 2
receives a path to a `plan.ifc` and nothing else — everything it needs is in the
file (IFC Interface Spec §0).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROVENANCE_PSET = "Pset_SimsysProvenance"
CONTRACT_PSET   = "Pset_SimsysContract"


# ── unit handling (the single inverse of §A1) ─────────────────────────────────
def _length_to_mm_factor(model) -> float:
    """Factor to convert the IFC length unit to millimetres."""
    try:
        proj = model.by_type("IfcProject")[0]
        for u in proj.UnitsInContext.Units:
            if getattr(u, "UnitType", None) != "LENGTHUNIT":
                continue
            if u.is_a("IfcSIUnit"):
                name = getattr(u, "Name", "METRE")
                prefix = getattr(u, "Prefix", None)
                base = {"METRE": 1000.0}.get(name, 1000.0)
                pref = {"MILLI": 0.001, "CENTI": 0.01, "DECI": 0.1,
                        None: 1.0}.get(prefix, 1.0)
                return base * pref          # e.g. METRE→1000, MILLIMETRE→1
    except Exception as exc:
        logger.debug("Unit detection failed, assuming mm: %s", exc)
    return 1.0   # default: file already in mm


# ── ifcopenshell helpers ──────────────────────────────────────────────────────
def _matrix(element):
    import ifcopenshell.util.placement as P
    try:
        return P.get_local_placement(element.ObjectPlacement)
    except Exception:
        return None


def _origin_mm(M, f: float) -> List[float]:
    return [float(M[0][3]) * f, float(M[1][3]) * f, float(M[2][3]) * f]


def _xaxis(M) -> Tuple[float, float]:
    return float(M[0][0]), float(M[1][0])


def _psets(el, qtos=False) -> Dict[str, Any]:
    import ifcopenshell.util.element as ue
    try:
        return ue.get_psets(el, qtos_only=qtos)
    except Exception:
        return {}


def _provenance(el) -> Dict[str, Any]:
    p = _psets(el).get(PROVENANCE_PSET, {}) or {}
    return {
        "id":            p.get("OriginalId"),
        "source":        p.get("Source", "default"),
        "confidence":    float(p.get("Confidence", 1.0)),
        "needs_review":  bool(p.get("NeedsReview", False)),
        "review_reason": p.get("ReviewReason", "") or "",
        "name_source":   p.get("NameSource"),
    }


def _oid(el, fallback: str) -> str:
    return _provenance(el)["id"] or fallback


def _space_polygon_mm(space, M, f: float) -> List[List[float]]:
    """Recover the space footprint as world-mm polygon from its extruded profile."""
    pts: List[List[float]] = []
    try:
        rep = space.Representation
        ox, oy = float(M[0][3]), float(M[1][3])
        for r in rep.Representations:
            for item in r.Items:
                area = getattr(item, "SweptArea", None)
                if area is None:
                    continue
                curve = getattr(area, "OuterCurve", None) or getattr(area, "Curve", None)
                coords = getattr(curve, "Points", None)
                if not coords:
                    continue
                for pt in coords:
                    c = pt.Coordinates
                    # local (relative to centroid placement) → world → mm
                    pts.append([(ox + float(c[0])) * f, (oy + float(c[1])) * f])
                if pts and pts[0] != pts[-1]:
                    pts.append(list(pts[0]))
                return pts
    except Exception as exc:
        logger.debug("Space polygon recovery failed: %s", exc)
    return pts


def _area_or_none(value: Any) -> Optional[float]:
    """Floor area as a float, or None when it is missing/unreadable/zero.

    Honest-provenance rule (review fix H1): an area we could not read is
    NOT an area of 0.0 — downstream, None means "unmeasured → NEEDS_REVIEW"
    while 0.0 means "measured and failing", which is a false claim.
    """
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0.0 else None


def _bbox_dims_mm(polygon: List[List[float]]) -> Dict[str, float]:
    """Bounding-box dimensions with a hard ordering guarantee:
    width_mm is ALWAYS the shorter side, length_mm ALWAYS the longer.

    Review fix C3 (2026-07): the previous version labelled length=X-extent
    and width=Y-extent. A room whose long axis runs along Y then reported
    its LONG side as `width_mm`, and Mabhas minimum-width clauses were
    checked against the wrong dimension — a verified false PASS (bedroom
    with a 2.1 m short side passed a "width >= 2.5 m" rule). Min-width /
    max-length semantics are orientation-independent, so the extents are
    ordered here, at the single place the dims are produced.
    """
    if not polygon:
        return {"length_mm": 0.0, "width_mm": 0.0}
    xs = [p[0] for p in polygon]; ys = [p[1] for p in polygon]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    return {"length_mm": round(max(dx, dy), 1),
            "width_mm":  round(min(dx, dy), 1)}


# ── main entry point ──────────────────────────────────────────────────────────
def ifc_to_bim_data(ifc_path: str) -> Dict[str, Any]:
    """Reconstruct the bim_data dict from an enriched IFC produced by Step 1."""
    import ifcopenshell

    model = ifcopenshell.open(ifc_path)
    f = _length_to_mm_factor(model)

    # contract version (informational; Step 2 may reject incompatible files)
    contract_version = None
    try:
        proj = model.by_type("IfcProject")[0]
        contract_version = _psets(proj).get(CONTRACT_PSET, {}).get("ContractVersion")
    except Exception:
        pass

    walls   = _read_walls(model, f)
    wall_gid_to_oid = {g: o for g, o in walls["_gid_map"]}
    walls_list = walls["walls"]

    doors   = _read_openings(model, "IfcDoor",   f, wall_gid_to_oid)
    windows = _read_openings(model, "IfcWindow", f, wall_gid_to_oid, is_window=True)
    rooms   = _read_spaces(model, f)
    stairs  = _read_simple(model, "IfcStair", f)

    # Issue 16 — versioned canonical-BIM contract. Scale (mm/pixel) is a vision
    # concept and is not carried in the IFC; geometry is authoritative in mm.
    # If Step 1 later writes a scale Pset on the project, read it here.
    scale = {"mm_per_pixel": None, "source": "ifc", "confidence": None}
    building_params: Dict[str, Any] = {}
    try:
        proj = model.by_type("IfcProject")[0]
        sp = _psets(proj).get(CONTRACT_PSET, {})
        if sp.get("ScaleMmPerPixel") is not None:
            scale = {"mm_per_pixel": float(sp["ScaleMmPerPixel"]),
                     "source": sp.get("ScaleSource", "ifc"),
                     "confidence": sp.get("ScaleConfidence")}
        # Manual 3D-modeling parameters written by Step 1's exporter
        # (Pset_SimsysContract, all in mm). Property names ARE the contract —
        # they mirror export/ifc_exporter._PARAM_PSET_MAP in Step 1.
        # WallHeightMm (finished floor level → underside of the slab above)
        # doubles as the engine's ceiling_height_mm: the value the room
        # "clear height" checks in services/numeric_checker.py measure
        # against, and the ONE parameter that cannot be recovered from the
        # 2D-derived element geometry.
        _pset_to_param = {
            "WallHeightMm":       "wall_height",
            "DoorHeightMm":       "door_height",
            "WindowHeightMm":     "window_height",
            "WindowSillHeightMm": "window_sill_height",
            "FloorThicknessMm":   "floor_thickness",
        }
        for _prop, _key in _pset_to_param.items():
            if sp.get(_prop) is not None:
                building_params[_key] = float(sp[_prop])
        if building_params.get("wall_height") is not None:
            building_params["ceiling_height_mm"] = building_params["wall_height"]
        _prov = str(sp.get("BuildingParamsProvided", "") or "")
        provided = sorted(k for k in _prov.split(",") if k.strip())
        if "wall_height" in provided:
            provided.append("ceiling_height_mm")
        building_params["_provided"] = sorted(provided)
    except Exception:
        pass

    return {
        "schema_version": "bim-canonical-v1",
        "units": {"length": "mm", "area": "m2"},
        "scale": scale,
        "building_params": building_params,
        "contract_version": contract_version,
        "coordinate_system": {"units": "millimeters",
                              "origin": [0.0, 0.0, 0.0],
                              "level_elevation": 0.0},
        "walls":   walls_list,
        "doors":   doors,
        "windows": windows,
        "rooms":   rooms,
        "stairs":  stairs,
        "slabs":   _read_simple(model, "IfcSlab", f),
    }


# ── per-type readers ──────────────────────────────────────────────────────────
def _read_walls(model, f: float) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    gid_map: List[Tuple[str, str]] = []
    for cls in ("IfcWallStandardCase", "IfcWall"):
        for w in model.by_type(cls):
            if w.is_a("IfcWallStandardCase") and cls == "IfcWall":
                continue   # avoid double-count (StandardCase is a subtype)
            prov = _provenance(w)
            oid = prov["id"] or w.GlobalId
            gid_map.append((w.GlobalId, oid))
            M = _matrix(w)
            qto = _psets(w, qtos=True).get("Qto_WallBaseQuantities", {})
            common = _psets(w).get("Pset_WallCommon", {})
            thickness = float(qto.get("Width", 200.0)) * f
            length    = float(qto.get("Length", 0.0)) * f
            height    = float(qto.get("Height", 2800.0)) * f
            if M is not None:
                start = _origin_mm(M, f)
                ux, uy = _xaxis(M)
                end = [start[0] + length * ux, start[1] + length * uy, 0.0]
            else:
                start, end = [0.0, 0.0, 0.0], [length, 0.0, 0.0]
            out.append({
                "id":          oid,
                "start_point": start,
                "end_point":   end,
                "thickness":   thickness,
                "height":      height,
                "is_exterior": bool(common.get("IsExternal", False)),
                "_provenance": prov,
            })
    return {"walls": out, "_gid_map": gid_map}


def _host_wall_oid(el, wall_gid_to_oid: Dict[str, str]) -> Optional[str]:
    """door/window → opening it fills → wall it voids → that wall's OriginalId."""
    try:
        for fills in (el.FillsVoids or []):
            opening = fills.RelatingOpeningElement
            for voids in (opening.VoidsElements or []):
                wall = voids.RelatingBuildingElement
                return wall_gid_to_oid.get(wall.GlobalId, _oid(wall, wall.GlobalId))
    except Exception:
        pass
    return None


def _read_openings(model, cls: str, f: float, wall_gid_to_oid: Dict[str, str],
                   is_window: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for el in model.by_type(cls):
        prov = _provenance(el)
        oid = prov["id"] or el.GlobalId
        M = _matrix(el)
        ip = _origin_mm(M, f) if M is not None else [0.0, 0.0, 0.0]
        width  = float(getattr(el, "OverallWidth",  0.0) or 0.0) * f
        height = float(getattr(el, "OverallHeight", 0.0) or 0.0) * f
        rec = {
            "id":           oid,
            "insertion_point": ip,
            "host_wall_id": _host_wall_oid(el, wall_gid_to_oid),
            "width":        width,
            "height":       height,
            "_provenance":  prov,
        }
        if is_window:
            rec["sill_height"] = ip[2]   # placement Z carries the sill height
            # Manual-override provenance: "user" when the operator asserted
            # this window's width (apply_window_overrides), else "measured".
            rec["width_source"] = str(
                _psets(el).get("Pset_SimsysProvenance", {})
                .get("WidthSource", "measured"))
            rec["is_exterior"] = bool(
                _psets(el).get("Pset_WindowCommon", {}).get("IsExternal", False))
        out.append(rec)
    return out


def _read_spaces(model, f: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for sp in model.by_type("IfcSpace"):
        prov = _provenance(sp)
        oid = prov["id"] or sp.GlobalId
        M = _matrix(sp)
        common = _psets(sp).get("Pset_SpaceCommon", {})
        qto = _psets(sp, qtos=True).get("Qto_SpaceBaseQuantities", {})
        polygon = _space_polygon_mm(sp, M, f) if M is not None else []
        out.append({
            "id":         oid,
            "name":       sp.Name or "Room",
            "local_name": sp.LongName or "",
            "category":   common.get("Category", "Unknown") or "Unknown",
            # Review fix H1 (2026-07): a missing/zero floor area used to be
            # coerced to 0.0, which the agents read as a MEASUREMENT — the
            # numeric checker emitted FAIL "area = 0.0" for rooms that were
            # never measured, <=-comparator (max-area) clauses false-PASSed,
            # and glazing ratios false-FAILed. Missing is now None; every
            # agent already routes an unmeasurable value to NEEDS_REVIEW.
            # (0.0 is treated as missing: exporters write 0.0 for "unknown",
            # and a genuinely zero-area room does not exist.)
            "area_m2":    _area_or_none(qto.get("NetFloorArea",
                                                common.get("GrossFloorArea"))),
            "polygon":    polygon,
            "dimensions": _bbox_dims_mm(polygon),
            "centroid_mm": [float(M[0][3]) * f, float(M[1][3]) * f] if M is not None else [0.0, 0.0],
            "name_source": prov["name_source"] or "none",
            "needs_review": prov["needs_review"],
            "_provenance": prov,
        })
    return out


def _read_simple(model, cls: str, f: float) -> List[Dict[str, Any]]:
    """Stairs/slabs: presence + provenance + placement (cold path on 4-class model)."""
    out: List[Dict[str, Any]] = []
    for el in model.by_type(cls):
        prov = _provenance(el)
        M = _matrix(el)
        out.append({
            "id": prov["id"] or el.GlobalId,
            "centroid_mm": [float(M[0][3]) * f, float(M[1][3]) * f] if M is not None else [0.0, 0.0],
            "_provenance": prov,
        })
    return out