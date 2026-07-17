"""Geometry value objects and deterministic validation helpers.

The canonical domain stores all coordinates in millimetres.  The helpers in
this module are deliberately small and side-effect free so Quality plugins can
reason about geometry without reaching back into IFC or legacy dictionaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot, isfinite
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class Point2D:
    x: float
    y: float

    @classmethod
    def from_value(cls, value: Any) -> Optional["Point2D"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
            try:
                return cls(float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                return None
        if isinstance(value, dict) and "x" in value and "y" in value:
            try:
                return cls(float(value["x"]), float(value["y"]))
            except (TypeError, ValueError):
                return None
        return None

    @property
    def finite(self) -> bool:
        return isfinite(self.x) and isfinite(self.y)

    def distance_to(self, other: "Point2D") -> float:
        return hypot(self.x - other.x, self.y - other.y)

    def to_legacy(self, z: float = 0.0) -> list[float]:
        return [self.x, self.y, float(z)]

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class OpeningPlacement:
    """Canonical along-wall placement expressed as the opening centre offset."""

    center_offset_mm: float
    source_convention: str = "center"

    @classmethod
    def from_legacy(cls, row: Any, width_mm: float | None) -> Optional["OpeningPlacement"]:
        if not isinstance(row, dict):
            return None
        payload = row.get("opening_placement")
        if isinstance(payload, dict) and payload.get("center_offset_mm") is not None:
            try:
                return cls(float(payload["center_offset_mm"]), str(payload.get("source_convention") or "center"))
            except (TypeError, ValueError):
                return None
        value = row.get("insertion_offset_mm")
        if value is None:
            return None
        try:
            offset = float(value)
        except (TypeError, ValueError):
            return None
        convention = str(row.get("insertion_convention") or "center").strip().lower()
        width = float(width_mm or 0.0)
        if convention == "start":
            center = offset + width / 2.0
        elif convention == "end":
            center = offset - width / 2.0
        else:
            convention = "center"
            center = offset
        return cls(center, convention)

    def to_legacy(self) -> dict[str, Any]:
        return {
            "center_offset_mm": self.center_offset_mm,
            "source_convention": self.source_convention,
        }


@dataclass(frozen=True)
class Polygon2D:
    points: tuple[Point2D, ...]

    @classmethod
    def from_value(cls, value: Any) -> Optional["Polygon2D"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        try:
            points = tuple(p for raw in value if (p := Point2D.from_value(raw)) is not None)
        except TypeError:
            return None
        return cls(points) if points else None

    @property
    def finite(self) -> bool:
        return bool(self.points) and all(point.finite for point in self.points)

    def is_closed(self, tolerance_mm: float = 1e-6) -> bool:
        return len(self.points) >= 2 and self.points[0].distance_to(self.points[-1]) <= tolerance_mm

    def ring_points(self, tolerance_mm: float = 1e-6) -> tuple[Point2D, ...]:
        """Return the polygon ring without a duplicated closing point."""
        if self.is_closed(tolerance_mm):
            return self.points[:-1]
        return self.points

    def unique_point_count(self, tolerance_mm: float = 1e-6) -> int:
        unique: list[Point2D] = []
        for point in self.ring_points(tolerance_mm):
            if not any(point.distance_to(existing) <= tolerance_mm for existing in unique):
                unique.append(point)
        return len(unique)

    def signed_area_mm2(self) -> float:
        ring = self.ring_points()
        if len(ring) < 3:
            return 0.0
        return 0.5 * sum(
            first.x * second.y - second.x * first.y
            for first, second in zip(ring, (*ring[1:], ring[0]))
        )

    def area_mm2(self) -> float:
        return abs(self.signed_area_mm2())

    def derived_area_m2(self) -> float:
        return self.area_mm2() / 1_000_000.0

    def centroid(self) -> Optional[Point2D]:
        ring = self.ring_points()
        if len(ring) < 3:
            return None
        signed = self.signed_area_mm2()
        if abs(signed) <= 1e-9:
            return Point2D(
                sum(point.x for point in ring) / len(ring),
                sum(point.y for point in ring) / len(ring),
            )
        factor = 1.0 / (6.0 * signed)
        cx = cy = 0.0
        for first, second in zip(ring, (*ring[1:], ring[0])):
            cross = first.x * second.y - second.x * first.y
            cx += (first.x + second.x) * cross
            cy += (first.y + second.y) * cross
        return Point2D(cx * factor, cy * factor)

    def to_shapely(self):
        """Return a Shapely polygon, or ``None`` when it cannot be built."""
        if not self.finite or self.unique_point_count() < 3:
            return None
        try:
            from shapely.geometry import Polygon
            polygon = Polygon([(point.x, point.y) for point in self.ring_points()])
            return polygon
        except Exception:
            return None

    def validation_errors(self, tolerance_mm: float = 1e-6) -> list[str]:
        errors: list[str] = []
        if not self.finite:
            errors.append("non_finite_coordinate")
        if self.unique_point_count(tolerance_mm) < 3:
            errors.append("fewer_than_three_unique_vertices")
        if not self.is_closed(tolerance_mm):
            errors.append("open_ring")
        if self.area_mm2() <= tolerance_mm * tolerance_mm:
            errors.append("zero_area")
        geometry = self.to_shapely()
        if geometry is not None and not geometry.is_valid:
            errors.append("self_intersection_or_invalid_topology")
        return errors

    def distance_to_boundary(self, point: Point2D) -> Optional[float]:
        geometry = self.to_shapely()
        if geometry is None or not point.finite:
            return None
        try:
            from shapely.geometry import Point
            return float(geometry.boundary.distance(Point(point.x, point.y)))
        except Exception:
            return None

    def contains_or_touches(self, point: Point2D, tolerance_mm: float = 1.0) -> bool:
        geometry = self.to_shapely()
        if geometry is None or not point.finite:
            return False
        try:
            from shapely.geometry import Point
            candidate = Point(point.x, point.y)
            return bool(geometry.buffer(tolerance_mm).covers(candidate))
        except Exception:
            return False

    def overlap_area_mm2(self, other: "Polygon2D") -> Optional[float]:
        first = self.to_shapely()
        second = other.to_shapely()
        if first is None or second is None:
            return None
        try:
            return float(first.intersection(second).area)
        except Exception:
            return None

    def coverage_ratio(self, other: "Polygon2D") -> Optional[float]:
        """Share of this polygon covered by ``other``."""
        area = self.area_mm2()
        overlap = self.overlap_area_mm2(other)
        if area <= 0 or overlap is None:
            return None
        return max(0.0, min(1.0, overlap / area))

    def to_legacy(self) -> list[list[float]]:
        return [[p.x, p.y] for p in self.points]

    def to_dict(self) -> dict[str, Any]:
        return {"points": [p.to_dict() for p in self.points]}
