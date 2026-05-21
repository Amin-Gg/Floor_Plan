"""
services/bim_builder.py
=======================
Transforms already-extracted analysis results into the `bim_data` block
returned by /analyze.

Design intent
-------------
This service does ONE thing: assemble the final BIM dict that ships to the
client. It does NOT:
  - Run model inference (that's mask_rcnn_model)
  - Extract walls, rooms, doors, windows, stairs, slabs (that's analysis/)
  - Convert pixel coordinates to mm (that happens upstream)
  - Compute geometric attributes (that's utils/polygon_geometry)
  - Apply Douglas-Peucker simplification (that's analysis/)

By keeping this layer purely transformational, swapping the model later
(or adding a new BIM target like ArchiCAD/SketchUp) means rewriting THIS
file only — no need to touch the route handler.

Why this exists
---------------
Before extraction, the bim_data dict was inlined as a ~50-line literal inside
the /analyze route. This made the route hard to read, hard to test in
isolation, and impossible to reuse from other endpoints (like a future
/export-only route that takes pre-extracted analysis JSON).

Now the route can call `BimDataBuilder(building_params).build(...)` and get
a fully-formed dict back. The route's job becomes orchestration, not BIM
assembly.

Building parameter defaults
---------------------------
If a value is missing from `building_params`, the corresponding industry
standard for Iranian residential construction is used as a fallback:
  wall_height          2800 mm
  door_height          2100 mm
  window_height        1200 mm
  window_sill_height    900 mm
  floor_thickness       200 mm

These match the previous in-route defaults exactly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Default building parameters (mm)
# ─────────────────────────────────────────────────────────────────────────────
# These match the Iranian residential standards used by the previous inline
# BIM construction in visualization_routes.py. Centralising them here means
# the route, the IFC exporter, and any future BIM target use the same values.
DEFAULT_WALL_HEIGHT_MM       = 2800.0
DEFAULT_DOOR_HEIGHT_MM       = 2100.0
DEFAULT_WINDOW_HEIGHT_MM     = 1200.0
DEFAULT_WINDOW_SILL_MM       =  900.0
DEFAULT_FLOOR_THICKNESS_MM   =  200.0


class BimDataBuilder:
    """Builds the `bim_data` dict that ships to the client."""

    def __init__(self, building_params: Optional[Dict[str, Any]] = None):
        bp = building_params or {}
        # Coerce to float so downstream JSON serialization is clean and so
        # callers can pass strings (e.g. from form fields) without crashing.
        try:
            self.wall_height       = float(bp.get("wall_height",        DEFAULT_WALL_HEIGHT_MM))
        except (TypeError, ValueError):
            self.wall_height       = DEFAULT_WALL_HEIGHT_MM
        try:
            self.door_height       = float(bp.get("door_height",        DEFAULT_DOOR_HEIGHT_MM))
        except (TypeError, ValueError):
            self.door_height       = DEFAULT_DOOR_HEIGHT_MM
        try:
            self.window_height     = float(bp.get("window_height",      DEFAULT_WINDOW_HEIGHT_MM))
        except (TypeError, ValueError):
            self.window_height     = DEFAULT_WINDOW_HEIGHT_MM
        try:
            self.window_sill       = float(bp.get("window_sill_height", DEFAULT_WINDOW_SILL_MM))
        except (TypeError, ValueError):
            self.window_sill       = DEFAULT_WINDOW_SILL_MM
        try:
            self.floor_thickness   = float(bp.get("floor_thickness",    DEFAULT_FLOOR_THICKNESS_MM))
        except (TypeError, ValueError):
            self.floor_thickness   = DEFAULT_FLOOR_THICKNESS_MM

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self,
              wall_parameters: List[Dict[str, Any]],
              detailed_doors:  List[Dict[str, Any]],
              detailed_windows: List[Dict[str, Any]],
              room_polygons:   List[Dict[str, Any]],
              bim_stairs:      List[Dict[str, Any]],
              bim_slabs:       List[Dict[str, Any]],
              exterior_walls:  Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Assemble the bim_data dict.

        Parameters
        ----------
        wall_parameters : list of wall dicts from extract_wall_parameters()
            Each must have:
              wall_id, centerline (list of [x_mm, y_mm]),
              thickness (dict with 'average' key)
        detailed_doors / detailed_windows : list of door/window dicts
            Each must have:
              door_id / window_id, host_wall_id, location, dimensions
        room_polygons : already-formed list from extract_room_polygons()
            Passed through as-is (this service does not re-process rooms).
        bim_stairs / bim_slabs : already-formed lists
            Passed through as-is.
        exterior_walls : iterable of exterior-wall dicts
            Used only to flag each wall's is_exterior. Each dict must have
            a 'wall_id' key (or the wall is treated as interior).

        Returns
        -------
        dict — the bim_data block, ready to be jsonified.
        """
        # Build the exterior-wall ID set once so the per-wall lookup is O(1).
        exterior_ids = set()
        try:
            for ew in exterior_walls:
                wid = ew.get("wall_id") if isinstance(ew, dict) else None
                if wid is not None:
                    exterior_ids.add(wid)
        except Exception:
            # Malformed exterior_walls — every wall is treated as interior
            exterior_ids = set()

        return {
            "description":       "Geometric vector data ready for Revit/BIM modeling via Dynamo",
            "coordinate_system": {
                "origin":          [0.0, 0.0, 0.0],
                "units":           "millimeters",
                "level_elevation": 0.0,
                "note":            "All coordinates are relative to image top-left corner",
            },
            "walls":   self._build_walls(wall_parameters, exterior_ids),
            "doors":   self._build_doors(detailed_doors),
            "windows": self._build_windows(detailed_windows),
            "rooms":   list(room_polygons),
            "stairs":  list(bim_stairs),
            "slabs":   list(bim_slabs),
        }

    # ── Internal builders (one per element type) ──────────────────────────────

    def _build_walls(self,
                     walls: List[Dict[str, Any]],
                     exterior_ids: set) -> List[Dict[str, Any]]:
        out = []
        for wall in walls:
            cl = wall.get("centerline", [])
            if len(cl) < 2:
                continue
            thickness_avg = wall["thickness"]["average"]
            out.append({
                "id":          wall["wall_id"],
                "start_point": [cl[0][0],  cl[0][1],  0.0],
                "end_point":   [cl[-1][0], cl[-1][1], 0.0],
                "thickness":   thickness_avg,
                "height":      self.wall_height,
                "type":        f"Basic Wall - {int(thickness_avg)}mm",
                "is_exterior": wall["wall_id"] in exterior_ids,
            })
        return out

    def _build_doors(self, doors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for d in doors:
            out.append({
                "id":              f"Door_{d['door_id']}",
                "host_wall_id":    d.get("host_wall_id"),
                "insertion_point": [d["location"]["center"]["x"],
                                    d["location"]["center"]["y"], 0.0],
                "width":           d["dimensions"]["width"],
                "height":          self.door_height,
                "swing_angle":     d.get("swing_angle", 0.0),
                "hinge_side":      d["orientation"].get("hinge_side", "unknown"),
                "type":            "Single-Flush",
            })
        return out

    def _build_windows(self, windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for w in windows:
            out.append({
                "id":              f"Window_{w['window_id']}",
                "host_wall_id":    w.get("host_wall_id"),
                "insertion_point": [w["location"]["center"]["x"],
                                    w["location"]["center"]["y"], 0.0],
                "width":           w["dimensions"]["width"],
                "height":          self.window_height,
                "sill_height":     self.window_sill,
                "type":            w["window_type"].capitalize() + " Window",
            })
        return out
