"""Canonical BIM element dataclasses.

The typed fields are the documented contract. ``extras`` retains source fields
that have not yet been promoted, allowing lossless migration from legacy dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .geometry import OpeningPlacement, Point2D, Polygon2D
from .identifiers import ElementIdentity


@dataclass
class ElementBase:
    identity: ElementIdentity
    storey_id: Optional[str] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Storey(ElementBase):
    name: Optional[str] = None
    elevation_mm: Optional[float] = None


@dataclass
class Wall(ElementBase):
    start: Optional[Point2D] = None
    end: Optional[Point2D] = None
    thickness_mm: Optional[float] = None
    height_mm: Optional[float] = None
    is_exterior: Optional[bool] = None


@dataclass
class Door(ElementBase):
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    host_wall_id: Optional[str] = None
    insertion_point: Optional[Point2D] = None
    insertion_z_mm: Optional[float] = None
    placement: Optional[OpeningPlacement] = None
    connected_space_ids: list[str] = field(default_factory=list)


@dataclass
class Window(Door):
    sill_height_mm: Optional[float] = None
    is_exterior: Optional[bool] = None
    width_source: Optional[str] = None


@dataclass
class Space(ElementBase):
    name: Optional[str] = None
    local_name: Optional[str] = None
    canonical_type: Optional[str] = None
    raw_type: Optional[str] = None
    category_source: Optional[str] = None
    category_confidence: Optional[float] = None
    area_m2: Optional[float] = None
    boundary: Optional[Polygon2D] = None
    centroid: Optional[Point2D] = None
    dimensions: dict[str, Any] = field(default_factory=dict)
    name_source: Optional[str] = None
    needs_review: bool = False


@dataclass
class SimpleElement(ElementBase):
    centroid: Optional[Point2D] = None
