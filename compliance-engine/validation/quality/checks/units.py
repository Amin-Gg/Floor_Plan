"""Model unit contract checks.

The legacy adapter normalises supported units once at the boundary. This plugin
reports missing assumptions and unsupported declarations; it never guesses a
conversion itself.
"""
from __future__ import annotations

from domain.findings import Finding, FindingSeverity
from domain.model import BuildingModel
from domain.units import normalise_unit_name
from standards.catalog_api import supported_units

from ..context import QualityContext
from ..findings import quality_finding


class UnitConsistencyCheck:
    code_prefix = "QC-UNIT"
    codes = ("QC-UNIT-001", "QC-UNIT-002")
    name = "unit_consistency"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(model.walls or model.doors or model.windows or model.spaces)

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        contract = model.extras.get("_unit_contract") or {}
        declared = bool(contract.get("declared", True))
        input_units = contract.get("input") or model.units
        assumptions = list(contract.get("assumptions") or [])

        if not declared:
            findings.append(quality_finding(
                model,
                "QC-UNIT-001",
                "The legacy payload did not declare units. Millimetres and m² "
                "were assumed for backward compatibility; verify the source "
                "contract before relying on dimensional verdicts.",
                object_type="model",
                expected={"length": "declared unit", "area": "declared unit"},
                actual={"declared": False, "assumptions": assumptions},
                source="quality.units",
                details={"blocks_capabilities": ["trusted_measurements"]},
            ))

        length = input_units.get("length") if isinstance(input_units, dict) else None
        area = input_units.get("area") if isinstance(input_units, dict) else None
        unsupported: dict[str, object] = {}
        length_supported = {normalise_unit_name(v) for v in supported_units("length")}
        area_supported = {normalise_unit_name(v) for v in supported_units("area")}
        if length is not None and normalise_unit_name(str(length)) not in length_supported:
            unsupported["length"] = length
        if area is not None and normalise_unit_name(str(area)) not in area_supported:
            unsupported["area"] = area
        if unsupported:
            findings.append(quality_finding(
                model,
                "QC-UNIT-002",
                f"Unsupported or inconsistent model units: {unsupported}.",
                object_type="model",
                expected={"length": list(supported_units("length")), "area": list(supported_units("area"))},
                actual=unsupported,
                source="quality.units",
                severity=FindingSeverity.FAIL,
                details={"blocks_capabilities": ["trusted_measurements"]},
            ))
        return findings
