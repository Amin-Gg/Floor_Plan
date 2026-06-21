"""
services/spatial_graph.py
=========================
Step 3 — Spatial Graph Builder

Converts the `bim_data` dict produced by BimDataBuilder into a NetworkX
graph that the compliance agents (Steps 5-8) query to check spatial rules.

Graph structure
---------------
Nodes  →  rooms  (one node per bim_data["rooms"] entry)
Edges  →  doors  (one edge per door that connects two rooms)

Node attributes (all sourced directly from bim_data):
    category          str    e.g. "room_bedroom", "room_bathroom"
    name              str    OCR-detected room name
    area_m2           float  net floor area
    polygon           Shapely Polygon   (mm coordinates)
    has_exterior      bool   True if room boundary touches building perimeter
    has_exterior_door bool   True if at least one door leads to the outside
    windows           list   window dicts assigned to this room (see below)

Edge attributes:
    door_id           str
    width             float  mm
    height            float  mm
    host_wall_id      str

Window dicts attached to each room node:
    id                str
    width             float  mm
    height            float  mm
    sill_height       float  mm
    is_exterior       bool   window is on an external wall
    host_wall_id      str

Query API (use from compliance agents):
    sg.are_directly_connected(room_a_id, room_b_id)  → bool
    sg.get_rooms_by_category(category)               → list[str]
    sg.get_exterior_windows(room_id)                 → list[dict]
    sg.total_glazing_area_m2(room_id)                → float
    sg.glazing_ratio(room_id)                        → float
    sg.can_reach_exit(room_id)                       → bool
    sg.egress_path(room_id)                          → list[str] | None
    sg.summary()                                     → dict
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

# Distance threshold (mm): how close a probe point must be to a polygon
# boundary before we consider it "inside or touching" that room.
# 30 mm (3 cm) tolerates typical polygon digitisation imprecision.
_TOUCH_THRESHOLD_MM = 30.0

# How far each side we probe from the door insertion point along the wall
# normal to discover the two connected rooms (mm).
_DOOR_PROBE_OFFSET_MM = 150.0


class SpatialGraph:
    """
    Builds and exposes the room-adjacency graph for one floor plan.

    Parameters
    ----------
    bim_data : dict
        The `bim_data` dict returned by BimDataBuilder.build().
    """

    def __init__(self, bim_data: Dict[str, Any]) -> None:
        self._bim = bim_data
        self.graph: nx.Graph = nx.Graph()

        # Internal geometry caches (populated during _build)
        self._room_poly:    Dict[str, Polygon]   = {}   # room_id  → Shapely Polygon
        self._wall_line:    Dict[str, LineString] = {}   # wall_id  → Shapely LineString
        self._footprint:    Optional[Polygon]    = None  # union of all room polys

        self._build()

    # ── Build pipeline ────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._parse_room_polygons()
        self._parse_wall_lines()
        self._build_footprint()
        self._add_room_nodes()
        self._add_door_edges()
        self._assign_windows()
        logger.info("SpatialGraph built: %d rooms, %d door-edges",
                    self.graph.number_of_nodes(),
                    self.graph.number_of_edges())

    def _parse_room_polygons(self) -> None:
        for room in self._bim.get("rooms", []):
            rid   = room.get("id", "")
            coords = room.get("polygon", [])
            if len(coords) < 3:
                continue
            try:
                poly = Polygon(coords)
                if not poly.is_valid:
                    poly = poly.buffer(0)   # attempt self-intersection repair
                if poly.is_valid and not poly.is_empty:
                    self._room_poly[rid] = poly
            except Exception as exc:
                logger.warning("Could not build polygon for room %s: %s", rid, exc)

    def _parse_wall_lines(self) -> None:
        for wall in self._bim.get("walls", []):
            wid = wall.get("id", "")
            sp  = wall.get("start_point")
            ep  = wall.get("end_point")
            if not (sp and ep):
                continue
            try:
                # start_point / end_point may be [x, y, z] lists or dicts
                sx, sy = _xy(sp)
                ex, ey = _xy(ep)
                if (sx, sy) == (ex, ey):
                    continue
                self._wall_line[wid] = LineString([(sx, sy), (ex, ey)])
            except Exception as exc:
                logger.warning("Could not build line for wall %s: %s", wid, exc)

    def _build_footprint(self) -> None:
        if self._room_poly:
            try:
                self._footprint = unary_union(list(self._room_poly.values()))
            except Exception as exc:
                logger.warning("Could not build building footprint: %s", exc)

    def _add_room_nodes(self) -> None:
        for room in self._bim.get("rooms", []):
            rid  = room.get("id", "")
            poly = self._room_poly.get(rid)

            # Exterior flag: does the room boundary touch the outer perimeter?
            has_exterior = False
            if poly and self._footprint:
                try:
                    fp_ext = (self._footprint.exterior
                              if hasattr(self._footprint, "exterior")
                              else None)
                    if fp_ext:
                        has_exterior = poly.exterior.distance(fp_ext) < _TOUCH_THRESHOLD_MM
                except Exception:
                    pass

            self.graph.add_node(rid, **{
                "category":          room.get("category", "unknown"),
                "name":              room.get("name", ""),
                "area_m2":           float(room.get("area_m2", 0.0)),
                "polygon":           poly,
                "has_exterior":      has_exterior,
                "has_exterior_door": False,   # updated in _add_door_edges
                "windows":           [],       # populated in _assign_windows
            })

    def _add_door_edges(self) -> None:
        for door in self._bim.get("doors", []):
            room_a, room_b = self._rooms_on_each_side(door)

            if room_a and room_b:
                # Interior door connecting two rooms
                if self.graph.has_node(room_a) and self.graph.has_node(room_b):
                    # Keep the wider door if multiple doors between same pair
                    existing = self.graph.get_edge_data(room_a, room_b)
                    new_w = float(door.get("width", 0))
                    if existing is None or new_w > existing.get("width", 0):
                        self.graph.add_edge(room_a, room_b, **{
                            "door_id":      door.get("id", ""),
                            "width":        new_w,
                            "height":       float(door.get("height", 0)),
                            "host_wall_id": door.get("host_wall_id", ""),
                        })
            elif room_a:
                # Door leads outside — mark the room
                if self.graph.has_node(room_a):
                    self.graph.nodes[room_a]["has_exterior_door"] = True
            elif room_b:
                if self.graph.has_node(room_b):
                    self.graph.nodes[room_b]["has_exterior_door"] = True

    def _assign_windows(self) -> None:
        for window in self._bim.get("windows", []):
            ip  = window.get("insertion_point")
            if not ip:
                continue
            wx, wy = _xy(ip)
            wp     = Point(wx, wy)

            # Assign to the room whose polygon is closest to the window point
            best_room, best_dist = None, float("inf")
            for rid, poly in self._room_poly.items():
                d = poly.distance(wp)
                if d < best_dist:
                    best_dist, best_room = d, rid

            if best_room is None or best_dist > 500:   # >500 mm → stray window
                continue

            wid      = window.get("id", "")
            host_wid = window.get("host_wall_id", "")
            is_ext   = self._wall_is_exterior(host_wid)

            self.graph.nodes[best_room]["windows"].append({
                "id":           wid,
                "width":        float(window.get("width", 0)),
                "height":       float(window.get("height", 0)),
                "sill_height":  float(window.get("sill_height", 0)),
                "is_exterior":  is_ext,
                "host_wall_id": host_wid,
            })

    # ── Geometry helpers ──────────────────────────────────────────────────────

    def _rooms_on_each_side(
        self, door: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Return (room_a_id, room_b_id) for the two rooms a door connects.
        One of them may be None when the door opens to the exterior.
        """
        ip = door.get("insertion_point")
        if not ip:
            return None, None

        dx, dy = _xy(ip)
        door_pt = Point(dx, dy)

        # Find wall normal vector for directional probing
        host_wid = door.get("host_wall_id", "")
        wall_geom = self._wall_line.get(host_wid)

        if wall_geom:
            c0, c1   = wall_geom.coords[0], wall_geom.coords[-1]
            vx, vy   = c1[0] - c0[0], c1[1] - c0[1]
            length   = max((vx**2 + vy**2) ** 0.5, 0.001)
            # Perpendicular (normal) unit vector
            nx_, ny_ = -vy / length, vx / length
            off      = _DOOR_PROBE_OFFSET_MM
            probe_a  = Point(dx + nx_ * off, dy + ny_ * off)
            probe_b  = Point(dx - nx_ * off, dy - ny_ * off)
        else:
            # No wall geometry — use a small radius fallback
            probe_a = probe_b = door_pt

        room_a = self._nearest_room(probe_a)
        room_b = self._nearest_room(probe_b, exclude=room_a)
        return room_a, room_b

    def _nearest_room(
        self,
        point: Point,
        exclude: Optional[str] = None,
        max_dist_mm: float = _DOOR_PROBE_OFFSET_MM * 2,
    ) -> Optional[str]:
        """Return the room whose polygon contains or is nearest to `point`."""
        best_room, best_dist = None, float("inf")
        for rid, poly in self._room_poly.items():
            if rid == exclude:
                continue
            d = poly.distance(point)
            if d < best_dist:
                best_dist, best_room = d, rid
        return best_room if best_dist <= max_dist_mm else None

    def _wall_is_exterior(self, wall_id: str) -> bool:
        """True if the wall's midpoint lies on the building's outer boundary."""
        if not wall_id or not self._footprint:
            return False
        geom = self._wall_line.get(wall_id)
        if geom is None:
            # Fall back to bim_data is_exterior flag
            for w in self._bim.get("walls", []):
                if w.get("id") == wall_id:
                    return bool(w.get("is_exterior", False))
            return False
        try:
            mid = geom.interpolate(0.5, normalized=True)
            ext = (self._footprint.exterior
                   if hasattr(self._footprint, "exterior") else None)
            return ext is not None and ext.distance(mid) < _TOUCH_THRESHOLD_MM
        except Exception:
            return False

    # ── Public query API (used by compliance agents) ──────────────────────────

    def are_directly_connected(self, room_a: str, room_b: str) -> bool:
        """True if exactly one door connects room_a and room_b."""
        return self.graph.has_edge(room_a, room_b)

    def get_rooms_by_category(self, category: str) -> List[str]:
        """Return all room IDs whose category matches (exact string)."""
        return [
            n for n, d in self.graph.nodes(data=True)
            if d.get("category") == category
        ]

    def get_neighbours(self, room_id: str) -> List[str]:
        """Return IDs of all rooms directly connected to room_id by a door."""
        return list(self.graph.neighbors(room_id))

    def get_exterior_windows(self, room_id: str) -> List[Dict[str, Any]]:
        """Return windows attached to room_id that face the exterior."""
        return [
            w for w in self.graph.nodes[room_id].get("windows", [])
            if w.get("is_exterior")
        ]

    def total_glazing_area_m2(self, room_id: str) -> float:
        """Sum of (width × height) for all exterior windows of room_id (m²)."""
        wins = self.get_exterior_windows(room_id)
        # dimensions are stored in mm → convert to m²
        return sum(w["width"] * w["height"] / 1_000_000 for w in wins)

    def glazing_ratio(self, room_id: str) -> float:
        """
        Glazing-to-floor-area ratio for room_id.
        Returns 0.0 if room area is zero to avoid division by zero.
        """
        area = self.graph.nodes[room_id].get("area_m2", 0.0)
        if area <= 0:
            return 0.0
        return self.total_glazing_area_m2(room_id) / area

    def can_reach_exit(self, room_id: str) -> bool:
        """
        True if a path exists from room_id to any room that has an exterior
        door (has_exterior_door=True), travelling only through door-edges.
        Uses BFS — no cycle risk.
        """
        for node in nx.bfs_tree(self.graph, room_id).nodes():
            if self.graph.nodes[node].get("has_exterior_door"):
                return True
        return False

    def egress_path(self, room_id: str) -> Optional[List[str]]:
        """
        Return the shortest door-path from room_id to the nearest exit room.
        Returns None if no path exists.
        """
        exit_rooms = [
            n for n, d in self.graph.nodes(data=True)
            if d.get("has_exterior_door")
        ]
        if not exit_rooms:
            return None
        best_path, best_len = None, float("inf")
        for er in exit_rooms:
            try:
                path = nx.shortest_path(self.graph, room_id, er)
                if len(path) < best_len:
                    best_path, best_len = path, len(path)
            except nx.NetworkXNoPath:
                continue
        return best_path

    def summary(self) -> Dict[str, Any]:
        """Quick statistics about the graph (useful for logging/smoke tests)."""
        nodes = list(self.graph.nodes(data=True))
        return {
            "total_rooms":     len(nodes),
            "total_doors":     self.graph.number_of_edges(),
            "exit_rooms":      sum(1 for _, d in nodes if d.get("has_exterior_door")),
            "exterior_rooms":  sum(1 for _, d in nodes if d.get("has_exterior")),
            "rooms_with_windows": sum(1 for _, d in nodes if d.get("windows")),
            "room_categories": dict(Counter(d.get("category","?") for _, d in nodes)),
        }


# ── Module-level helper ───────────────────────────────────────────────────────

def _xy(point: Any) -> Tuple[float, float]:
    """
    Extract (x, y) from various point representations:
      - [x, y, z]  list / tuple
      - {"x": ..., "y": ...}  dict
    """
    if isinstance(point, (list, tuple)):
        return float(point[0]), float(point[1])
    if isinstance(point, dict):
        return float(point["x"]), float(point["y"])
    raise TypeError(f"Cannot extract x/y from {type(point)}: {point!r}")
