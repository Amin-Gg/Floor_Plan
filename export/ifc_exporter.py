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
import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

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

    # Merge params
    p: Dict = {**DEFAULTS}
    if building_params:
        p.update(building_params)

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

    # Millimetre units — keeps all our mm coordinates exact, no scaling needed
    ifcopenshell.api.unit.assign_unit(
        model,
        length={"is_metric": True, "raw": "MILLIMETRE"},
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
            _create_window(
                model, body, win,
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
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Element creators
# ─────────────────────────────────────────────────────────────────────────────

def _create_wall(model, body_ctx, axis_ctx, w: dict, wall_height: float):
    """Create an IfcWallStandardCase with axis-based geometry."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry

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
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=wall, matrix=matrix
    )

    # Body geometry: rectangular extrusion along local +X
    representation = ifcopenshell.api.geometry.add_wall_representation(
        model,
        context=body_ctx,
        length=length,
        height=height,
        thickness=thickness,
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=wall, representation=representation
    )

    return wall


def _create_door(model, body_ctx, d: dict, default_height: float,
                 host_wall, storey):
    """Create an IfcDoor, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

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
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=door, matrix=matrix
    )

    # Door geometry using the parametric door builder
    door_rep = ifcopenshell.api.geometry.add_door_representation(
        model,
        context=body_ctx,
        overall_width=width,
        overall_height=height,
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

    return door


def _create_window(model, body_ctx, win: dict, default_height: float,
                   default_sill: float, host_wall, storey):
    """Create an IfcWindow, optionally voiding its host wall."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

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
    matrix[2, 3] = sill_height    # Z offset = sill height
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
        overall_width=width,
        overall_height=height,
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

    return window


def _create_space(model, body_ctx, room: dict, wall_height: float, storey):
    """Create an IfcSpace from a room polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

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
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=space, matrix=matrix
    )

    # Build extruded footprint geometry from polygon
    # Polygon points are absolute mm — convert to local (relative to centroid)
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

    return space


def _create_stair(model, body_ctx, stair: dict, storey):
    """Create an IfcStair from a footprint polygon."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

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
    return ifc_stair


def _create_slab(model, body_ctx, slab: dict, floor_thickness: float, storey):
    """Create an IfcSlab from a polygon (Balcony, Terrace, Parking)."""
    import ifcopenshell.api.root
    import ifcopenshell.api.geometry
    import ifcopenshell.api.spatial

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
    import ifcopenshell.api.void

    opening = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcOpeningElement",
        predefined_type="OPENING"
    )

    # Place opening relative to the host wall's coordinate system
    # We use a simple placement at the insertion point
    matrix = _point_rotation_matrix(
        insertion_point[0], insertion_point[1], 0.0
    )
    matrix[2, 3] = elevation
    ifcopenshell.api.geometry.edit_object_placement(
        model, product=opening, matrix=matrix
    )

    # Opening box geometry — deep enough to cut through any wall thickness
    opening_rep = ifcopenshell.api.geometry.add_profile_representation(
        model,
        context=model.by_type("IfcGeometricRepresentationSubContext")[0],
        profile=_rect_profile(model, width, height),
        depth=600.0,     # 600 mm — deeper than any wall we will encounter
    )
    ifcopenshell.api.geometry.assign_representation(
        model, product=opening, representation=opening_rep
    )

    # Void the host wall — api.void was renamed to api.feature in ifcopenshell 0.8+
    try:
        import ifcopenshell.api.feature
        ifcopenshell.api.feature.add_opening(
            model, opening=opening, element=host_wall
        )
    except (ImportError, AttributeError):
        import ifcopenshell.api.void
        ifcopenshell.api.void.add_opening(
            model, opening=opening, element=host_wall
        )

    return opening


def _fill_opening(model, opening, element):
    """Fill an IfcOpeningElement with a door or window."""
    try:
        import ifcopenshell.api.feature
        ifcopenshell.api.feature.add_filling(model, opening=opening, element=element)
    except (ImportError, AttributeError):
        import ifcopenshell.api.void
        ifcopenshell.api.void.add_filling(model, opening=opening, element=element)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

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
