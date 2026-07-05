"""
export/ifc_exporter.py
======================
Converts the bim_data JSON produced by the FloorPlanTo3D API into a
valid IFC4 file using the ifcopenshell high-level Python API.

The generated .ifc can be opened directly in:
    Revit, ArchiCAD, Tekla, FreeCAD / Bonsai, BIM 360,
    Solibri, BIMvision, and any other IFC4-compliant viewer.

Public API
----------
    bim_json_to_ifc(bim_data, building_params, output_path) -> str
        Takes the bim_data dict, optional height/project parameters,
        and writes an IFC4 file to output_path.

Building Parameters (all heights in millimetres)
-------------------------------------------------
    project_name          str     "Floor Plan Project"
    project_address       str     ""
    building_name         str     "Building"
    storey_name           str     "Ground Floor"
    storey_elevation      float   0.0
    wall_height           float   2800.0   ← clear wall height
    floor_thickness       float   200.0    ← concrete slab thickness
    door_height           float   2100.0   ← clear opening height
    window_sill_height    float   900.0    ← floor to bottom of window
    window_height         float   1200.0   ← opening height
"""

import math
import hashlib
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Contract constants (IFC Interface Spec) ──────────────────────────────────
# Bump CONTRACT_VERSION when the IFC interface changes in a way Step 2 must
# reject. Step 2 reads Pset_SimsysContract.ContractVersion on the IfcProject.
CONTRACT_VERSION = "1.0"
PROVENANCE_PSET  = "Pset_SimsysProvenance"   # §A4 — one per element, no nulls
CONTRACT_PSET    = "Pset_SimsysContract"     # §4 file-level — ContractVersion

# ── Default parameters ───────────────────────────────────────────────────────
DEFAULTS: Dict = {
    "project_name":       "Floor Plan Project",
    "project_address":    "",
    "building_name":      "Building",
    "storey_name":        "Ground Floor",
    "storey_elevation":   0.0,
    "wall_height":        2800.0,
    "floor_thickness":    200.0,
    "door_height":        2100.0,
    "window_sill_height": 900.0,
    "window_height":      1200.0,
}

# Manual 3D-modeling parameters carried in Pset_SimsysContract (all in mm).
# These values shape the exported geometry AND are read back by the compliance
# engine's ingest (Step 2), which cannot recover the wall/ceiling height from
# 2D-derived geometry alone. Property names are the contract; do not rename
# without updating compliance-engine/ingest/ifc_to_bim_data.py.
_PARAM_PSET_MAP: Dict[str, str] = {
    "wall_height":        "WallHeightMm",         # FFL → underside of slab above
    "door_height":        "DoorHeightMm",
    "window_height":      "WindowHeightMm",
    "window_sill_height": "WindowSillHeightMm",   # FFL → bottom of window
    "floor_thickness":    "FloorThicknessMm",
}


# ─────────────────────────────────────────────────────────────────────────────
# Contract helpers (deterministic ids + provenance) — IFC Interface Spec §A2/§A4
# ─────────────────────────────────────────────────────────────────────────────
def _stable_guid(kind: str, original_id) -> str:
    """Deterministic 22-char IFC GlobalId from (kind, original_id).

    §A2: re-exporting the same bim_data must produce the same ids. We hash a
    stable string and compress it to the IFC base64 GlobalId form, so GUIDs are
    reproducible across runs instead of random each time.
    """
    import ifcopenshell.guid
    digest = hashlib.md5(f"{kind}:{original_id}".encode("utf-8")).hexdigest()
    return ifcopenshell.guid.compress(digest)


def _assign_guid(element, kind: str, original_id) -> None:
    try:
        element.GlobalId = _stable_guid(kind, original_id)
    except Exception as exc:   # never fail the export over a GUID
        logger.debug("Could not set deterministic GlobalId for %s:%s (%s)",
                     kind, original_id, exc)


def _add_provenance(model, product, *, original_id, source: str,
                    detector_class: str = "", confidence: float = 1.0,
                    needs_review: bool = False, review_reason: str = "",
                    name_source: Optional[str] = None,
                    width_source: Optional[str] = None) -> None:
    """Attach Pset_SimsysProvenance to an element with NO null fields (§A4).

    Step 2 reads this to downgrade uncertain elements to NEEDS_REVIEW instead of
    silently mis-verdicting them. All fields are always written with defaults.
    """
    import ifcopenshell.api.pset
    props = {
        "OriginalId":    str(original_id),
        "Source":        str(source or "default"),
        "DetectorClass": str(detector_class or ""),
        "Confidence":    float(confidence if confidence is not None else 1.0),
        "NeedsReview":   bool(needs_review),
        "ReviewReason":  str(review_reason or ""),
    }
    if name_source is not None:               # spaces only
        props["NameSource"] = str(name_source)
    if width_source is not None:               # windows only — "measured" | "user"
        props["WidthSource"] = str(width_source)
    pset = ifcopenshell.api.pset.add_pset(model, product=product, name=PROVENANCE_PSET)
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=props)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def bim_json_to_ifc(bim_data: dict,
                    building_params: Optional[dict] = None,
                    output_path: Optional[str] = None) -> str:
    """
    Convert bim_data JSON to a valid IFC4 file.

    Parameters
    ----------
    bim_data : dict
        The ``bim_data`` key from the /analyze API response.
        Must contain at minimum a "walls" list.

    building_params : dict, optional
        Override any key from DEFAULTS.  Only specify what you want to change.
        Example:
            {"wall_height": 3000, "project_name": "Block 4 - Unit 12"}

    output_path : str, optional
        Full path for the output .ifc file.
        If None, a temporary file is created and its path is returned.

    Returns
    -------
    str
        Absolute path to the generated .ifc file.
    """
    try:
        import ifcopenshell
        import ifcopenshell.api
        import ifcopenshell.api.root
        import ifcopenshell.api.unit
        import ifcopenshell.api.context
        import ifcopenshell.api.project
        import ifcopenshell.api.geometry
        import ifcopenshell.api.aggregate
        import ifcopenshell.api.spatial
        import ifcopenshell.api.pset
    except ImportError as e:
        raise ImportError(
            "ifcopenshell is not installed.\n"
            "Run: pip install ifcopenshell"
        ) from e

    # Merge params — precedence: explicit building_params arg > the
    # building_params block embedded in bim_data by BimDataBuilder > DEFAULTS.
    # "_provided" tracks which values the OPERATOR asserted (vs defaults) so
    # the IFC contract can carry honest provenance to the compliance engine.
    _embedded = dict(bim_data.get("building_params") or {})
    _provided = set(_embedded.pop("_provided", []) or [])
    p: Dict = {**DEFAULTS}
    p.update({k: v for k, v in _embedded.items() if k in DEFAULTS and v is not None})
    if building_params:
        _explicit = {k: v for k, v in building_params.items() if v is not None}
        p.update(_explicit)
        _provided |= {k for k in _explicit if k in _PARAM_PSET_MAP}
    _provided &= set(_PARAM_PSET_MAP)  # only geometric params carry provenance

    walls   = bim_data.get("walls",   [])
    doors   = bim_data.get("doors",   [])
    windows = bim_data.get("windows", [])
    rooms   = bim_data.get("rooms",   [])
    stairs  = bim_data.get("stairs",  [])
    slabs   = bim_data.get("slabs",   [])

    logger.info(
        f"IFC export: {len(walls)} walls, {len(doors)} doors, "
        f"{len(windows)} windows, {len(rooms)} rooms"
    )

    # ── 1. Create IFC4 file and project skeleton ─────────────────────────────
    model = ifcopenshell.api.project.create_file(version="IFC4")

    project = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcProject", name=p["project_name"]
    )
    _assign_guid(project, "IfcProject", p["project_name"])

    # Units (§A1, §9.1 decision): keep MILLIMETRE for length so all our mm
    # coordinates stay exact (no geometry rescaling), and declare an explicit
    # SQUARE_METRE area unit so Qto_SpaceBaseQuantities.NetFloorArea reads in m²
    # — the unit the IDS lane and municipal rules expect.
    ifcopenshell.api.unit.assign_unit(
        model,
        length={"is_metric": True, "raw": "MILLIMETRE"},
        area={"is_metric": True, "raw": "SQUARE_METRE"},
    )

    # Contract version (§4, file-level) — Step 2 rejects incompatible files.
    _contract_pset = ifcopenshell.api.pset.add_pset(
        model, product=project, name=CONTRACT_PSET
    )
    _contract_props = {"ContractVersion": CONTRACT_VERSION}
    # Issue 4/16 — carry the pixel→mm scale and its provenance so Step 2 can
    # record scale confidence and downgrade dimensional checks when untrusted.
    _scale = bim_data.get("scale") or {}
    if _scale.get("mm_per_pixel") is not None:
        _contract_props["ScaleMmPerPixel"] = float(_scale["mm_per_pixel"])
    _contract_props["ScaleSource"] = str(_scale.get("source", "default"))
    if _scale.get("confidence") is not None:
        _contract_props["ScaleConfidence"] = float(_scale["confidence"])
    # Manual 3D-modeling parameters — write the values the geometry was built
    # with, plus WHICH of them the operator asserted (vs engine defaults), so
    # Step 2 can (a) run room-height checks without re-guessing the ceiling
    # and (b) tag parameter-based verdicts with honest provenance.
    for _key, _prop in _PARAM_PSET_MAP.items():
        _contract_props[_prop] = float(p[_key])
    _contract_props["BuildingParamsProvided"] = ",".join(sorted(_provided))
    ifcopenshell.api.pset.edit_pset(
        model, pset=_contract_pset,
        properties=_contract_props,
    )

    # Geometry contexts
    ctx  = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model",
        context_identifier="Body", target_view="MODEL_VIEW", parent=ctx
    )
    axis_ctx = ifcopenshell.api.context.add_context(
        model, context_type="Model",
        context_identifier="Axis", target_view="GRAPH_VIEW", parent=ctx
    )

    # ── 2. Spatial hierarchy: Site → Building → Storey ───────────────────────
    site = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSite", name="Site"
    )
    building = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuilding", name=p["building_name"]
    )
    storey = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcBuildingStorey", name=p["storey_name"]
    )
    storey.Elevation = p["storey_elevation"]
    _assign_guid(site,     "IfcSite",          "Site")
    _assign_guid(building, "IfcBuilding",      p["building_name"])
    _assign_guid(storey,   "IfcBuildingStorey", p["storey_name"])

    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=project, products=[site])
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=site, products=[building])
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=building, products=[storey])

    # Address (optional — for permit filing records)
    if p["project_address"]:
        building.BuildingAddress = model.createIfcPostalAddress(
            Purpose="OFFICE",
            AddressLines=[p["project_address"]]
        )

    # ── 3. Walls ─────────────────────────────────────────────────────────────
    ifc_walls: Dict[str, object] = {}   # wall_id → IfcWall  (needed for door/window hosting)
    wall_is_exterior: Dict[object, bool] = {
        w.get("id"): bool(w.get("is_exterior", False)) for w in walls
    }

    for w in walls:
        try:
            ifc_wall = _create_wall(model, body, axis_ctx, w, p["wall_height"])
            ifcopenshell.api.spatial.assign_container(
                model, relating_structure=storey, products=[ifc_wall])
            ifc_walls[w["id"]] = ifc_wall
        except Exception as exc:
            logger.warning(f"Skipped wall {w.get('id')}: {exc}")

    # ── 4. Doors ─────────────────────────────────────────────────────────────
    for d in doors:
        try:
            host = ifc_walls.get(d.get("host_wall_id"))
            _create_door(model, body, d, p["door_height"], host, storey)
        except Exception as exc:
            logger.warning(f"Skipped door {d.get('id')}: {exc}")

    # ── 5. Windows ───────────────────────────────────────────────────────────
    for win in windows:
        try:
            host = ifc_walls.get(win.get("host_wall_id"))
            # §A5: mirror the host wall's IsExternal onto the window so the IDS
            # lane can read window externality without geometric inference.
            win_ext = dict(win)
            win_ext["is_exterior"] = wall_is_exterior.get(win.get("host_wall_id"), False)
            _create_window(
                model, body, win_ext,
                p["window_height"], p["window_sill_height"],
                host, storey
            )
        except Exception as exc:
            logger.warning(f"Skipped window {win.get('id')}: {exc}")

    # ── 6. Rooms (IfcSpace) ──────────────────────────────────────────────────
    for room in rooms:
        try:
            _create_space(model, body, room, p["wall_height"], storey)
        except Exception as exc:
            logger.warning(f"Skipped room {room.get('id')}: {exc}")

    # ── 7. Stairs ────────────────────────────────────────────────────────────
    for stair in stairs:
        try:
            _create_stair(model, body, stair, storey)
        except Exception as exc:
            logger.warning(f"Skipped stair {stair.get('id')}: {exc}")

    # ── 8. Slabs (Balcony / Parking / Terrace) ───────────────────────────────
    for slab in slabs:
        try:
            _create_slab(model, body, slab, p["floor_thickness"], storey)
        except Exception as exc:
            logger.warning(f"Skipped slab {slab.get('id')}: {exc}")

    # ── 9. Write file ────────────────────────────────────────────────────────
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".ifc", delete=False, prefix="floorplan_"
        )
        output_path = tmp.name
        tmp.close()

    model.write(output_path)
    logger.info(f"IFC4 file written: {output_path}")

    # ── 10. Export-time contract gate (§A7) ──────────────────────────────────
    # Refuse to emit a non-conforming IFC: a file that fails the contract would
    # be silently mishandled by Step 2. On failure, discard the file and raise.
    from validation import validate_ifc_contract
    from validation.report import IfcContractError
    contract = validate_ifc_contract(
        output_path, provenance_pset=PROVENANCE_PSET, contract_pset=CONTRACT_PSET
    )
    if contract.blocked:
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except OSError:
            pass
        failed = [i.code for i in contract.issues if i.severity.value == "critical"]
        logger.error("IFC contract gate FAILED (%d critical): %s",
                     contract.n_critical, failed[:12])
        raise IfcContractError(
            "Exporter produced a non-conforming IFC; it was discarded "
            "(IFC Interface Spec §A7). Critical: " + ", ".join(failed[:12]),
            report=contract,
        )

    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Element creators
# ─────────────────────────────────────────────────────────────────────────────

def _create_wall(model, body_ctx, axis_ctx, w: dict, wall_height: float):
    """Create an IfcWallStandardCase with axis-based geometry."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.pset

    S = _unit_scale(model)   # §9.1: feed the high-level geometry API in metres

    sp = w["start_point"]   # [x, y, z] in mm
    ep = w["end_point"]     # [x, y, z] in mm
    thickness = float(w.get("thickness", 200.0))
    height    = float(w.get("height", wall_height))

    dx = ep[0] - sp[0]
    dy = ep[1] - sp[1]
    length = math.hypot(dx, dy)
    if length < 1.0:
        raise ValueError("Wall length < 1mm — degenerate wall skipped")

    wall = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcWall",
        name=str(w.get("id", "Wall")),
        predefined_type="SOLIDWALL"
    )
    wall.Description = w.get("type", "")

    # Placement matrix: origin at start_point, local X along wall direction
    matrix = _wall_matrix(sp[0], sp[1], dx / length, dy / length)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=wall, matrix=matrix
    )

    # Body geometry: rectangular extrusion along local +X
    representation = ifcopenshell.api.geometry.add_wall_representation(
        model,
        context=body_ctx,
        length=length * S,
        height=height * S,
        thickness=thickness * S,
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=wall, representation=representation
    )

    # Axis line (§A3): start at placement origin, end at +X·length in local
    # coords. Lane 2 uses it for door-side probing; the loader uses it to
    # recover wall endpoints on import.
    try:
        axis_rep = ifcopenshell.api.geometry.add_axis_representation(
            model, context=axis_ctx, axis=((0.0, 0.0), (length * S, 0.0))
        )
        ifcopenshell.api.geometry.assign_representation(
            model, product=wall, representation=axis_rep
        )
    except Exception as exc:
        logger.debug("Axis representation failed for wall %s: %s", w.get("id"), exc)

    # Pset_WallCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=wall, name="Pset_WallCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={
            "IsExternal":  bool(w.get("is_exterior", False)),
            "LoadBearing": False,
            "Reference":   w.get("type", ""),
        }
    )

    # Qto_WallBaseQuantities (mm) — Width is the thickness; lets the loader
    # recover thickness/length/height as real quantities instead of from geometry.
    _qto = ifcopenshell.api.pset.add_qto(
        model, product=wall, name="Qto_WallBaseQuantities"
    )
    ifcopenshell.api.pset.edit_qto(
        model, qto=_qto,
        properties={"Width": float(thickness), "Length": float(length),
                    "Height": float(height)}
    )

    _assign_guid(wall, "IfcWall", w.get("id", "Wall"))
    _add_provenance(model, wall, original_id=w.get("id", "Wall"),
                    source="maskrcnn", detector_class="wall",
                    confidence=float(w.get("confidence", 1.0)),
                    needs_review=bool(w.get("needs_review", False)),
                    review_reason=w.get("review_reason", "") or "")
    return wall


def _create_door(model, body_ctx, d: dict, default_height: float,
                 host_wall, storey):
    """Create an IfcDoor, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial
    import ifcopenshell.api.pset

    S = _unit_scale(model)   # §9.1

    width  = float(d.get("width",  900.0))
    height = float(d.get("height", default_height))
    ip     = d["insertion_point"]   # [x, y, z] mm
    angle  = math.radians(float(d.get("swing_angle", 0.0)))

    door = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcDoor",
        name=str(d.get("id", "Door")),
        predefined_type="DOOR"
    )
    door.OverallWidth  = width
    door.OverallHeight = height

    matrix = _point_rotation_matrix(ip[0], ip[1], angle)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=door, matrix=matrix
    )

    # Door geometry using the parametric door builder
    door_rep = ifcopenshell.api.geometry.add_door_representation(
        model,
        context=body_ctx,
        overall_width=width * S,
        overall_height=height * S,
        operation_type=_hinge_to_operation(d.get("hinge_side", "left_edge")),
    )
    if door_rep:
        ifcopenshell.api.geometry.assign_representation(
            model, product=door, representation=door_rep
        )

    if host_wall:
        # Create opening in host wall and fill with door
        opening = _create_opening(model, ip, width, height, 0.0, host_wall)
        door.ObjectPlacement = opening.ObjectPlacement
        _fill_opening(model, opening, door)
    else:
        # No host wall identified — place door in storey without opening
        ifcopenshell.api.spatial.assign_container(
            model, relating_structure=storey, products=[door]
        )

    # Pset_DoorCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=door, name="Pset_DoorCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={"IsExternal": bool(d.get("is_exterior", False))}
    )

    _assign_guid(door, "IfcDoor", d.get("id", "Door"))
    _add_provenance(model, door, original_id=d.get("id", "Door"),
                    source="maskrcnn", detector_class="door",
                    confidence=float(d.get("confidence", 1.0)),
                    needs_review=bool(d.get("needs_review", False)),
                    review_reason=d.get("review_reason", "") or "")
    return door


def _create_window(model, body_ctx, win: dict, default_height: float,
                   default_sill: float, host_wall, storey):
    """Create an IfcWindow, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial
    import ifcopenshell.api.pset

    S = _unit_scale(model)   # §9.1

    width       = float(win.get("width",  1200.0))
    height      = float(win.get("height", default_height))
    sill_height = float(win.get("sill_height", default_sill))
    ip          = win["insertion_point"]   # [x, y, z] mm

    window = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcWindow",
        name=str(win.get("id", "Window")),
        predefined_type="WINDOW"
    )
    window.OverallWidth  = width
    window.OverallHeight = height

    matrix = _point_rotation_matrix(ip[0], ip[1], 0.0)
    matrix[2, 3] = sill_height    # Z offset = sill height (mm)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=window, matrix=matrix
    )

    # Window geometry
    win_type = win.get("type", "Horizontal Window").lower()
    # SINGLE_PANEL is the only safe default: "FIXED" is not a valid
    # IfcWindowTypePartitioningEnum value, and DOUBLE_PANEL_HORIZONTAL
    # requires 2 panel_properties entries (IndexError with the default 1).
    partition_type = "SINGLE_PANEL"
    win_rep = ifcopenshell.api.geometry.add_window_representation(
        model,
        context=body_ctx,
        overall_width=width * S,
        overall_height=height * S,
        partition_type=partition_type,
    )
    if win_rep:
        ifcopenshell.api.geometry.assign_representation(
            model, product=window, representation=win_rep
        )

    if host_wall:
        opening = _create_opening(
            model, ip, width, height, sill_height, host_wall
        )
        window.ObjectPlacement = opening.ObjectPlacement
        _fill_opening(model, opening, window)
    else:
        ifcopenshell.api.spatial.assign_container(
            model, relating_structure=storey, products=[window]
        )

    # Pset_WindowCommon (BIM interoperability + code checks)
    _pset = ifcopenshell.api.pset.add_pset(
        model, product=window, name="Pset_WindowCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=_pset,
        properties={"IsExternal": bool(win.get("is_exterior", False))}
    )

    _assign_guid(window, "IfcWindow", win.get("id", "Window"))
    _add_provenance(model, window, original_id=win.get("id", "Window"),
                    source="maskrcnn", detector_class="window",
                    confidence=float(win.get("confidence", 1.0)),
                    needs_review=bool(win.get("needs_review", False)),
                    review_reason=win.get("review_reason", "") or "",
                    width_source=win.get("width_source", "measured"))
    return window


def _create_space(model, body_ctx, room: dict, wall_height: float, storey):
    """Create an IfcSpace from a room polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = room.get("polygon", [])
    if len(polygon) < 4:
        raise ValueError("Room polygon has fewer than 3 unique points")

    space = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSpace",
        name=room.get("name", room.get("id", "Space")),
        predefined_type="INTERNAL"
    )
    space.LongName = room.get("local_name", "")

    # Placement at room centroid (ground level)
    cx, cy = room["centroid_mm"]
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=space, matrix=matrix
    )

    # Build extruded footprint geometry from polygon.
    # local_pts are mm offsets from the centroid; _make_extruded_polygon_rep
    # writes raw IfcCartesianPoints, so it stays in mm (NOT scaled) and pairs
    # with the mm centroid placement above to give correct absolute geometry.
    local_pts = [
        (pt[0] - cx, pt[1] - cy)
        for pt in polygon[:-1]           # exclude closing point
    ]

    try:
        # Build profile and extrude to wall height
        representation = _make_extruded_polygon_rep(
            model, body_ctx, local_pts, wall_height
        )
        ifcopenshell.api.geometry.assign_representation(
            model, product=space, representation=representation
        )
    except Exception as exc:
        logger.debug(f"Space geometry failed for {room.get('id')}: {exc}")

    # IfcSpace is an IfcSpatialStructureElement — it must be decomposed from
    # the storey via IfcRelAggregates (aggregate.assign_object), NOT via
    # IfcRelContainedInSpatialStructure (spatial.assign_container).
    # Using spatial.assign_container raises:
    #   "entity instance of type 'IFC4.IfcSpace' has no attribute
    #    'ContainedInStructure'"
    ifcopenshell.api.aggregate.assign_object(
        model, relating_object=storey, products=[space]
    )

    # Property set with area and category
    pset = ifcopenshell.api.pset.add_pset(
        model, product=space, name="Pset_SpaceCommon"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=pset,
        properties={
            "GrossFloorArea":  room.get("area_m2", 0.0),
            "IsExternal":      False,
            "Category":        room.get("category", ""),
        }
    )

    # Qto_SpaceBaseQuantities (§A3): NetFloorArea is a REAL quantity the IDS lane
    # checks natively. Area unit is SQUARE_METRE (declared at project level), so
    # NetFloorArea is in m². Height is a length quantity → project length unit (mm).
    qto = ifcopenshell.api.pset.add_qto(
        model, product=space, name="Qto_SpaceBaseQuantities"
    )
    ifcopenshell.api.pset.edit_qto(
        model, qto=qto,
        properties={
            "NetFloorArea":   float(room.get("area_m2", 0.0)),   # m²
            "GrossFloorArea": float(room.get("area_m2", 0.0)),   # m²
            "Height":         float(wall_height),                # mm
        }
    )

    # Provenance (§A4): rooms are geometric polygons named by OCR. Confidence,
    # NeedsReview and the review reasons come from the upstream room-quality
    # assessment (Issue 5) so Step 2 can defer suspicious/untyped rooms.
    _name_source  = room.get("name_source", "none")
    _needs_review = bool(room.get("needs_review", _name_source == "none"))
    _reasons = room.get("review_reasons") or []
    _reason_text = "; ".join(_reasons) if _reasons else (
        "room name from OCR missing" if _needs_review else "")
    _assign_guid(space, "IfcSpace", room.get("id", "Space"))
    _add_provenance(
        model, space, original_id=room.get("id", "Space"),
        source=("ocr" if _name_source == "ocr" else "geometric"),
        detector_class="room",
        confidence=float(room.get("confidence", 1.0)),
        needs_review=_needs_review,
        review_reason=_reason_text,
        name_source=_name_source,
    )
    return space


def _create_stair(model, body_ctx, stair: dict, storey):
    """Create an IfcStair from a footprint polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = stair.get("footprint_polygon", stair.get("polygon", []))
    if not polygon or len(polygon) < 3:
        raise ValueError("Stair has no valid footprint polygon")

    ifc_stair = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcStair",
        name=str(stair.get("id", "Stair")),
        predefined_type="STRAIGHT_RUN_STAIR"
    )

    # Centroid for placement
    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=ifc_stair, matrix=matrix
    )

    top_level   = float(stair.get("top_level",  2800.0))
    base_level  = float(stair.get("base_level",    0.0))
    stair_height = top_level - base_level
    local_pts = [(pt[0] - cx, pt[1] - cy) for pt in polygon]
    try:
        rep = _make_extruded_polygon_rep(model, body_ctx, local_pts, stair_height)
        ifcopenshell.api.geometry.assign_representation(
            model, product=ifc_stair, representation=rep
        )
    except Exception:
        pass

    ifcopenshell.api.spatial.assign_container(
        model, relating_structure=storey, products=[ifc_stair]
    )

    import ifcopenshell.api.pset as _ps
    _sp = _ps.add_pset(model, product=ifc_stair, name="Pset_StairCommon")
    _ps.edit_pset(model, pset=_sp, properties={"IsExternal": False})
    _assign_guid(ifc_stair, "IfcStair", stair.get("id", "Stair"))
    _add_provenance(model, ifc_stair, original_id=stair.get("id", "Stair"),
                    source="default", detector_class="stair")
    return ifc_stair


def _create_slab(model, body_ctx, slab: dict, floor_thickness: float, storey):
    """Create an IfcSlab from a polygon (Balcony, Terrace, Parking)."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

    S = _unit_scale(model)   # §9.1 (placement only; manual footprint stays mm)

    polygon = slab.get("polygon", [])
    if not polygon or len(polygon) < 3:
        raise ValueError("Slab has no valid polygon")

    thickness = float(slab.get("thickness", floor_thickness))
    elevation = float(slab.get("elevation", 0.0))

    slab_type = slab.get("type", "Slab").upper()
    predefined_map = {
        "BALCONY": "FLOOR",
        "TERRACE": "ROOF",
        "PARKING": "FLOOR",
    }
    predefined = predefined_map.get(slab_type, "FLOOR")

    ifc_slab = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcSlab",
        name=str(slab.get("id", "Slab")),
        predefined_type=predefined
    )
    ifc_slab.Description = slab.get("type", "")

    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    matrix = _point_rotation_matrix(cx, cy, 0.0)
    matrix[2, 3] = elevation
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=ifc_slab, matrix=matrix
    )

    local_pts = [(pt[0] - cx, pt[1] - cy) for pt in polygon]
    try:
        rep = _make_extruded_polygon_rep(model, body_ctx, local_pts, thickness)
        ifcopenshell.api.geometry.assign_representation(
            model, product=ifc_slab, representation=rep
        )
    except Exception:
        pass

    ifcopenshell.api.spatial.assign_container(
        model, relating_structure=storey, products=[ifc_slab]
    )

    import ifcopenshell.api.pset as _ps
    _sp = _ps.add_pset(model, product=ifc_slab, name="Pset_SlabCommon")
    _ps.edit_pset(model, pset=_sp, properties={"IsExternal": True})
    _assign_guid(ifc_slab, "IfcSlab", slab.get("id", "Slab"))
    _add_provenance(model, ifc_slab, original_id=slab.get("id", "Slab"),
                    source="default", detector_class="slab")
    return ifc_slab


# ─────────────────────────────────────────────────────────────────────────────
# Opening helpers
# ─────────────────────────────────────────────────────────────────────────────

def _create_opening(model, insertion_point, width: float, height: float,
                    elevation: float, host_wall):
    """
    Create an IfcOpeningElement in the host wall at the given insertion point.
    The opening is sized to cut through the full wall thickness automatically
    (IFC viewers handle the boolean subtraction).
    """
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    # NOTE (review fix C4): do NOT `import ifcopenshell.api.void` here. That
    # module was removed in ifcopenshell 0.8.x (renamed to api.feature). Importing
    # it unconditionally raised ImportError at the top of this function — BEFORE
    # the feature/void fallback below could run — so every door/window opening was
    # skipped and the elements came out orphaned. The fallback at the end of this
    # function handles both module names correctly.

    S = _unit_scale(model)   # §9.1

    opening = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcOpeningElement",
        predefined_type="OPENING"
    )
    try:
        _host_gid = getattr(host_wall, "GlobalId", "wall")
        _assign_guid(opening, "IfcOpeningElement",
                     f"{_host_gid}:{round(insertion_point[0])},{round(insertion_point[1])}")
    except Exception:
        pass

    # Place opening relative to the host wall's coordinate system
    # We use a simple placement at the insertion point
    matrix = _point_rotation_matrix(
        insertion_point[0], insertion_point[1], 0.0
    )
    matrix[2, 3] = elevation
    matrix = _scale_translation(matrix, S)
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=opening, matrix=matrix
    )

    # Opening box geometry — deep enough to cut through any wall thickness.
    # _rect_profile writes raw IfcCartesianPoints (mm); only the API `depth`
    # arg is in SI and must be scaled.
    opening_rep = ifcopenshell.api.geometry.add_profile_representation(
        model,
        context=model.by_type("IfcGeometricRepresentationSubContext")[0],
        profile=_rect_profile(model, width, height),
        depth=600.0 * S,     # 600 mm — deeper than any wall we will encounter
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=opening, representation=opening_rep
    )

    # Void the host wall. The API moved across ifcopenshell versions:
    #   0.8.x (current): api.feature.add_feature(file, feature=opening, element=wall)
    #   earlier 0.8:     api.feature.add_opening(file, opening=opening, element=wall)
    #   0.7.x:           api.void.add_opening(file, opening=opening, element=wall)
    # Review fix (C4): the previous code called feature.add_opening (absent in
    # 0.8.5) then fell through to the removed api.void module, so NO opening ever
    # cut a wall — doors/windows came out orphaned. Try all three in order.
    _voided = False
    try:
        import ifcopenshell.api.feature as _feat
        if hasattr(_feat, "add_feature"):
            _feat.add_feature(model, feature=opening, element=host_wall)
            _voided = True
        elif hasattr(_feat, "add_opening"):
            _feat.add_opening(model, opening=opening, element=host_wall)
            _voided = True
    except ImportError:
        pass
    if not _voided:
        import ifcopenshell.api.void as _void
        _void.add_opening(model, opening=opening, element=host_wall)

    return opening


def _fill_opening(model, opening, element):
    """Fill an IfcOpeningElement with a door or window.
    api.feature.add_filling on 0.8.x; api.void.add_filling on 0.7.x (review fix C4)."""
    try:
        import ifcopenshell.api.feature as _feat
        if hasattr(_feat, "add_filling"):
            _feat.add_filling(model, opening=opening, element=element)
            return
    except ImportError:
        pass
    import ifcopenshell.api.void as _void
    _void.add_filling(model, opening=opening, element=element)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _unit_scale(model) -> float:
    """File length unit → SI metres (0.001 for a MILLIMETRE file).

    §9.1 unit fix. The high-level ifcopenshell ``geometry.*`` API
    (``edit_object_placement``, ``add_wall_representation``,
    ``add_door_representation``, ``add_window_representation``,
    ``add_axis_representation``, ``add_profile_representation``) interprets its
    numeric inputs as **SI metres** and rescales them to the file's unit on
    write. Our geometry is authored in millimetres, so every value handed to
    that API is multiplied by this factor first (mm × 0.001 = m, which the API
    then writes back as mm). Manual builders that emit raw IfcCartesianPoint
    coordinates directly (:func:`_make_extruded_polygon_rep`,
    :func:`_rect_profile`) bypass the API and therefore stay in **mm** — they
    must NOT be scaled.
    """
    import ifcopenshell.util.unit
    return ifcopenshell.util.unit.calculate_unit_scale(model)


def _scale_translation(matrix: np.ndarray, s: float) -> np.ndarray:
    """Return a copy of a 4×4 placement matrix with only its translation
    column scaled by ``s``. The rotation sub-matrix is unitless and is left
    untouched."""
    m = matrix.copy()
    m[:3, 3] *= s
    return m


def _wall_matrix(ox: float, oy: float, ux: float, uy: float) -> np.ndarray:
    """
    Build a 4×4 placement matrix for a wall.
    Origin = wall start point (ox, oy, 0).
    Local X axis = wall direction (ux, uy, 0).
    Local Z axis = (0, 0, 1) — walls are always vertical.
    """
    mat = np.eye(4)
    mat[0, 0] = ux;  mat[0, 1] = -uy;  mat[0, 3] = ox
    mat[1, 0] = uy;  mat[1, 1] =  ux;  mat[1, 3] = oy
    return mat


def _point_rotation_matrix(x: float, y: float,
                            angle_rad: float) -> np.ndarray:
    """
    Build a 4×4 placement matrix for a point element (door, window, space).
    Origin = (x, y, 0).  Rotation around Z by angle_rad.
    """
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    mat = np.eye(4)
    mat[0, 0] = c;   mat[0, 1] = -s;  mat[0, 3] = x
    mat[1, 0] = s;   mat[1, 1] =  c;  mat[1, 3] = y
    return mat


def _rect_profile(model, width: float, height: float):
    """Return an IfcRectangleProfileDef centred at origin."""
    return model.createIfcRectangleProfileDef(
        "AREA", None,
        model.createIfcAxis2Placement2D(
            model.createIfcCartesianPoint([width / 2.0, height / 2.0])
        ),
        width, height
    )


def _make_extruded_polygon_rep(model, body_ctx,
                                local_pts: List[Tuple[float, float]],
                                depth: float):
    """
    Create an IfcShapeRepresentation from an arbitrary closed polygon
    extruded by `depth` in the +Z direction.

    local_pts : list of (x_mm, y_mm) — already relative to the object's origin
    depth     : extrusion height in mm
    """
    # Build IFC polyline from local 2D points
    ifc_pts_2d = [
        model.createIfcCartesianPoint([float(x), float(y)])
        for x, y in local_pts
    ]
    # Close the polygon explicitly
    ifc_pts_2d.append(ifc_pts_2d[0])
    polyline = model.createIfcPolyline(ifc_pts_2d)
    profile  = model.createIfcArbitraryClosedProfileDef("AREA", None, polyline)

    direction = model.createIfcDirection([0.0, 0.0, 1.0])
    position  = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint([0.0, 0.0, 0.0]),
        model.createIfcDirection([0.0, 0.0, 1.0]),
        model.createIfcDirection([1.0, 0.0, 0.0]),
    )
    solid = model.createIfcExtrudedAreaSolid(profile, position, direction, float(depth))

    return model.createIfcShapeRepresentation(
        body_ctx, "Body", "SweptSolid", [solid]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lookup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hinge_to_operation(hinge_side: str) -> str:
    """Map hinge_side string to IFC door operation type."""
    mapping = {
        "left_edge":   "SINGLE_SWING_LEFT",
        "right_edge":  "SINGLE_SWING_RIGHT",
        "top_edge":    "SINGLE_SWING_LEFT",   # horizontal door fallback
        "bottom_edge": "SINGLE_SWING_RIGHT",
        "unknown":     "SINGLE_SWING_LEFT",
    }
    return mapping.get(hinge_side, "SINGLE_SWING_LEFT")
