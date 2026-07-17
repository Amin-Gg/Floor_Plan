"""Quality finding for a failed Pset_SimsysContract read."""
from __future__ import annotations

from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext
from ..findings import quality_finding


class ContractReadCheck:
    code_prefix = "QC-CONTRACT"
    codes = ("QC-CONTRACT-001",)
    name = "contract_read"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(model.extras.get("_contract_read_error"))

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        error = str(model.extras.get("_contract_read_error"))
        return [quality_finding(
            model,
            "QC-CONTRACT-001",
            f"Reading Pset_SimsysContract from the IFC failed ({error}) — "
            "building parameters are unavailable and every parameter-dependent "
            "clause is NOT_EVALUATED; fix the exporter contract or the file",
            object_type="building_params",
            expected="Pset_SimsysContract readable",
            actual=error,
            source="quality.contract_read",
        )]
