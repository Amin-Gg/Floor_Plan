"""Versioned manual-input contract for dimensions unavailable from 2D plans."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ResolvedValue:
    value: Any
    unit: Optional[str]
    source: str
    confidence: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class ProjectInputs:
    default_storey_height_mm: Optional[float] = None
    finished_floor_level_mm: Optional[float] = None
    floor_thickness_mm: Optional[float] = None


@dataclass
class DefaultInputs:
    ceiling_height_mm: Optional[float] = None
    wall_height_mm: Optional[float] = None
    door_height_mm: Optional[float] = None
    window_width_mm: Optional[float] = None
    window_height_mm: Optional[float] = None
    window_sill_height_mm: Optional[float] = None
    floor_thickness_mm: Optional[float] = None

    def as_dict(self) -> dict[str, float]:
        return {
            key: value
            for key, value in vars(self).items()
            if value is not None
        }


@dataclass
class ElementOverrides:
    windows: dict[str, dict[str, float]] = field(default_factory=dict)
    doors: dict[str, dict[str, float]] = field(default_factory=dict)
    walls: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class ManualInputs:
    schema_version: str = "1.0"
    project: ProjectInputs = field(default_factory=ProjectInputs)
    defaults: DefaultInputs = field(default_factory=DefaultInputs)
    element_overrides: ElementOverrides = field(default_factory=ElementOverrides)
    allow_unmatched_overrides: bool = False

    @property
    def empty(self) -> bool:
        return not (
            any(v is not None for v in vars(self.project).values())
            or bool(self.defaults.as_dict())
            or self.element_overrides.windows
            or self.element_overrides.doors
            or self.element_overrides.walls
        )

    def to_wire_dict(self) -> dict[str, Any]:
        """JSON-safe v1.0 payload suitable for Celery/HTTP boundaries."""
        return {
            "schema_version": self.schema_version,
            "project": {k: v for k, v in vars(self.project).items() if v is not None},
            "defaults": self.defaults.as_dict(),
            "element_overrides": {
                "windows": self.element_overrides.windows,
                "doors": self.element_overrides.doors,
                "walls": self.element_overrides.walls,
            },
            "allow_unmatched_overrides": self.allow_unmatched_overrides,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_wire_dict()


@dataclass
class ManualMergeResult:
    model: Any
    findings: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
