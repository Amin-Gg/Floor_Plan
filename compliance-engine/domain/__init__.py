"""Canonical domain contracts for the compliance engine."""
from .elements import Door, SimpleElement, Space, Storey, Wall, Window
from .findings import Finding, FindingSeverity, FindingStage, Verdict
from .geometry import Point2D, Polygon2D
from .identifiers import ElementIdentity
from .model import BuildingModel, BuildingParameters, ModelProvenance
from .units import area_to_m2, length_to_mm
from .validation import ValidationResult

__all__ = [
    "BuildingModel", "BuildingParameters", "ModelProvenance",
    "ElementIdentity", "Point2D", "Polygon2D", "Storey", "Wall", "Door",
    "Window", "Space", "SimpleElement", "Finding", "FindingStage",
    "FindingSeverity", "Verdict", "ValidationResult", "length_to_mm", "area_to_m2",
]
