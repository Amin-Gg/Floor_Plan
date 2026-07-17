"""Storey assignment and one-storey FFL consistency checks."""
from __future__ import annotations

from collections.abc import Iterable

from domain.elements import ElementBase, Space
from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext, display_element_id, element_aliases
from ..findings import quality_finding


def _non_space_elements(model: BuildingModel) -> Iterable[ElementBase]:
    yield from model.walls
    yield from model.doors
    yield from model.windows
    yield from model.stairs
    yield from model.slabs


class StoreyConsistencyCheck:
    code_prefix = "QC-STOREY"
    codes = ("QC-STOREY-001", "QC-STOREY-002", "QC-STOREY-003")
    name = "storey_consistency"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(
            model.storeys or model.walls or model.doors or model.windows
            or model.spaces or model.stairs or model.slabs
        )

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        aliases = {
            alias
            for storey in model.storeys
            for alias in element_aliases(storey)
        }
        if not model.storeys:
            findings.append(quality_finding(
                model,
                "QC-STOREY-001",
                "The model has building elements but no Storey representation.",
                object_type="storey",
                expected="at least one Storey",
                actual="0 storeys",
                source="quality.storey_consistency",
                details={"blocks_capabilities": ["storey_specific_rules"]},
            ))
        else:
            for element in _non_space_elements(model):
                if not element.storey_id:
                    findings.append(quality_finding(
                        model,
                        "QC-STOREY-001",
                        f"{type(element).__name__} {display_element_id(element)} "
                        "has no storey assignment.",
                        element=element,
                        expected="storey_id",
                        actual=None,
                        source="quality.storey_consistency",
                        details={"blocks_capabilities": ["storey_specific_rules"]},
                    ))
                elif element.storey_id not in aliases:
                    findings.append(quality_finding(
                        model,
                        "QC-STOREY-002",
                        f"{type(element).__name__} {display_element_id(element)} "
                        f"references unknown storey '{element.storey_id}'.",
                        element=element,
                        expected="storey_id resolves to a Storey",
                        actual=element.storey_id,
                        source="quality.storey_consistency",
                        details={"blocks_capabilities": ["storey_specific_rules"]},
                    ))

        by_name: dict[str, set[float]] = {}
        for storey in model.storeys:
            if storey.name and storey.elevation_mm is not None:
                by_name.setdefault(storey.name.strip().casefold(), set()).add(
                    round(float(storey.elevation_mm), 3)
                )
        for name, elevations in sorted(by_name.items()):
            if len(elevations) > 1:
                findings.append(quality_finding(
                    model,
                    "QC-STOREY-003",
                    f"Storey name '{name}' is associated with conflicting "
                    f"finished floor levels: {sorted(elevations)} mm.",
                    object_type="storey",
                    expected="one FFL per storey name",
                    actual=sorted(elevations),
                    source="quality.storey_consistency",
                ))

        coordinate_ffl = model.coordinate_system.get("level_elevation")
        if len(model.storeys) == 1 and coordinate_ffl is not None \
                and model.storeys[0].elevation_mm is not None:
            try:
                coordinate_value = float(coordinate_ffl)
                storey_value = float(model.storeys[0].elevation_mm)
            except (TypeError, ValueError):
                coordinate_value = storey_value = 0.0
            if abs(coordinate_value - storey_value) > 1.0:
                findings.append(quality_finding(
                    model,
                    "QC-STOREY-003",
                    "The single storey's FFL conflicts with coordinate-system "
                    "level_elevation.",
                    element=model.storeys[0],
                    object_type="storey",
                    expected=f"{storey_value:.3f} mm",
                    actual=f"{coordinate_value:.3f} mm",
                    source="quality.storey_consistency",
                ))
        return findings
