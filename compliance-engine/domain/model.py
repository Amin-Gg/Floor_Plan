"""Canonical internal BuildingModel contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .elements import Door, SimpleElement, Space, Storey, Wall, Window


@dataclass
class ModelProvenance:
    source_type: str
    model_fingerprint: str
    source_path: Optional[str] = None
    model_name: Optional[str] = None
    ifc_schema: Optional[str] = None


@dataclass
class BuildingParameters:
    values: dict[str, Any] = field(default_factory=dict)
    provided: set[str] = field(default_factory=set)
    provided_marker_present: bool = False

    @classmethod
    def from_legacy(cls, value: Any) -> "BuildingParameters":
        block = dict(value or {})
        marker_present = "_provided" in block
        if marker_present:
            provided = set(block.pop("_provided", []) or [])
        else:
            # Stage-8 legacy semantics: without an explicit marker, every
            # supplied key is operator-asserted.
            provided = set(block)
        return cls(values=block, provided=provided,
                   provided_marker_present=marker_present)

    def to_legacy(self) -> dict[str, Any]:
        out = dict(self.values)
        if self.provided_marker_present:
            out["_provided"] = sorted(self.provided)
        return out


@dataclass
class BuildingModel:
    provenance: ModelProvenance
    project_id: Optional[str] = None
    site_id: Optional[str] = None
    building_id: Optional[str] = None
    storeys: list[Storey] = field(default_factory=list)
    walls: list[Wall] = field(default_factory=list)
    doors: list[Door] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    spaces: list[Space] = field(default_factory=list)
    stairs: list[SimpleElement] = field(default_factory=list)
    slabs: list[SimpleElement] = field(default_factory=list)
    parameters: BuildingParameters = field(default_factory=BuildingParameters)
    scale: dict[str, Any] = field(default_factory=dict)
    units: dict[str, Any] = field(default_factory=lambda: {"length": "mm", "area": "m2"})
    coordinate_system: dict[str, Any] = field(default_factory=dict)
    contract_version: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)
