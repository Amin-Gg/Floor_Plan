"""Existing check for verdict-driving building parameters left unasserted."""
from __future__ import annotations

from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext
from ..findings import quality_finding


VERDICT_DRIVING_PARAMETERS = ("wall_height",)


class ManualParametersCheck:
    code_prefix = "QC-PARAM"
    codes = ("QC-PARAM-001",)
    name = "manual_parameters"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(model.parameters.values) or model.parameters.provided_marker_present

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        for name in VERDICT_DRIVING_PARAMETERS:
            if name not in model.parameters.provided:
                findings.append(quality_finding(
                    model,
                    "QC-PARAM-001",
                    f"Building parameter '{name}' was not asserted by the "
                    "operator — clauses depending on it are NOT_EVALUATED until "
                    "the real value is supplied",
                    object_type="building_params",
                    expected=f"'{name}' in building_params._provided",
                    actual="engine default",
                    source="quality.manual_parameters",
                ))
        return findings
