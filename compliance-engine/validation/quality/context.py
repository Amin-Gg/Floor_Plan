"""Immutable per-run context for quality-check plugins."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from domain.elements import ElementBase
from domain.findings import Finding
from domain.model import BuildingModel




def _freeze(value: Any) -> Any:
    """Deeply copy request data into immutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return deepcopy(value)

DEFAULT_QUALITY_TOLERANCES: Mapping[str, float] = MappingProxyType({
    "place_len_tol_mm": 10.0,
    "place_axis_tol_mm": 50.0,
    "place_overlap_tol_mm": 10.0,
    "place_endpoint_tol_mm": 10.0,
    "place_vertical_tol_mm": 10.0,
    "space_boundary_tol_mm": 1.0,
    "space_area_abs_tol_m2": 0.10,
    "space_area_rel_tol": 0.02,
    "space_overlap_abs_tol_m2": 0.01,
    "space_connectivity_tol_mm": 75.0,
})


def element_aliases(element: ElementBase) -> set[str]:
    """Return every stable identifier by which a source row may reference it."""
    identity = element.identity
    return {
        str(value)
        for value in (
            identity.internal_id,
            identity.ifc_guid,
            identity.source_id,
        )
        if value
    }


def display_element_id(element: ElementBase) -> str:
    """Use the legacy-facing ID while retaining canonical identity fields."""
    identity = element.identity
    return identity.source_id or identity.ifc_guid or identity.internal_id


def _element_index(model: BuildingModel) -> dict[str, Optional[ElementBase]]:
    """Build an alias index; ambiguous aliases intentionally resolve to None."""
    index: dict[str, Optional[ElementBase]] = {}
    collections: Iterable[Iterable[ElementBase]] = (
        model.storeys,
        model.walls,
        model.doors,
        model.windows,
        model.spaces,
        model.stairs,
        model.slabs,
    )
    for collection in collections:
        for element in collection:
            for alias in element_aliases(element):
                if alias in index and index[alias] is not element:
                    index[alias] = None
                else:
                    index[alias] = element
    return index


@dataclass(frozen=True)
class QualityContext:
    """All non-domain inputs needed by quality checks for one execution.

    The context copies mutable input structures on construction so request-level
    overrides and plugin execution cannot leak into later requests.
    """

    threshold: float = 0.5
    review_summary: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    tolerances: Mapping[str, float] = field(
        default_factory=lambda: DEFAULT_QUALITY_TOLERANCES
    )
    initial_findings: tuple[Finding, ...] = ()
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _elements_by_alias: Mapping[str, Optional[ElementBase]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )

    @classmethod
    def from_model(
        cls,
        model: BuildingModel,
        *,
        initial_findings: Iterable[Finding] = (),
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "QualityContext":
        review_raw = dict(model.extras.get("_review_summary") or {})
        try:
            threshold = float(review_raw.get("threshold", 0.5))
        except (TypeError, ValueError):
            threshold = 0.5

        tolerances = dict(DEFAULT_QUALITY_TOLERANCES)
        raw_tolerances = model.extras.get("_qc_tolerances") or {}
        if isinstance(raw_tolerances, Mapping):
            for key, value in raw_tolerances.items():
                if key not in tolerances:
                    continue
                try:
                    tolerances[key] = float(value)
                except (TypeError, ValueError):
                    # Invalid tolerance values are ignored here; a later
                    # configuration-validation phase can make them first-class.
                    continue

        return cls(
            threshold=threshold,
            review_summary=_freeze(review_raw),
            tolerances=MappingProxyType(tolerances),
            initial_findings=tuple(initial_findings),
            metadata=_freeze(dict(metadata or {})),
            _elements_by_alias=MappingProxyType(_element_index(model)),
        )

    def resolve_element(self, value: Any) -> Optional[ElementBase]:
        if value is None:
            return None
        return self._elements_by_alias.get(str(value))
