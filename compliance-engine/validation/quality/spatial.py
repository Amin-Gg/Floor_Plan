"""Shared spatial helpers for Room/Space and opening-quality plugins."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from domain.elements import Door, Space, Wall, Window
from domain.geometry import Point2D
from domain.model import BuildingModel

from .context import element_aliases


@dataclass(frozen=True)
class SpaceConnectivity:
    explicit: tuple[Space, ...]
    derived: tuple[Space, ...]
    unresolved_explicit_ids: tuple[str, ...]

    @property
    def effective(self) -> tuple[Space, ...]:
        return self.explicit or self.derived


def _space_index(spaces: Iterable[Space]) -> dict[str, Space | None]:
    index: dict[str, Space | None] = {}
    for space in spaces:
        for alias in element_aliases(space):
            if alias in index and index[alias] is not space:
                index[alias] = None
            else:
                index[alias] = space
    return index


def spaces_touching_point(
    model: BuildingModel,
    point: Point2D | None,
    *,
    tolerance_mm: float,
) -> tuple[Space, ...]:
    if point is None or not point.finite:
        return ()
    matches: list[Space] = []
    for space in model.spaces:
        boundary = space.boundary
        if boundary is None or boundary.validation_errors():
            continue
        distance = boundary.distance_to_boundary(point)
        if distance is not None and distance <= tolerance_mm:
            matches.append(space)
    return tuple(matches)


def opening_space_connectivity(
    model: BuildingModel,
    opening: Door | Window,
    *,
    tolerance_mm: float,
) -> SpaceConnectivity:
    index = _space_index(model.spaces)
    explicit: list[Space] = []
    unresolved: list[str] = []
    for value in opening.connected_space_ids:
        resolved = index.get(str(value))
        if resolved is None:
            unresolved.append(str(value))
        elif resolved not in explicit:
            explicit.append(resolved)
    derived = spaces_touching_point(
        model,
        opening.insertion_point,
        tolerance_mm=tolerance_mm,
    )
    return SpaceConnectivity(tuple(explicit), derived, tuple(unresolved))


def wall_index(model: BuildingModel) -> dict[str, Wall]:
    result: dict[str, Wall] = {}
    for wall in model.walls:
        for alias in element_aliases(wall):
            result[alias] = wall
    return result
