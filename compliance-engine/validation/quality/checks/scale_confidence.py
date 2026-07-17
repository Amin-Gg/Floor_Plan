"""Existing global pixel-to-millimetre scale-confidence check."""
from __future__ import annotations

from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext
from ..findings import quality_finding


class ScaleConfidenceCheck:
    code_prefix = "QC-SCALE"
    codes = ("QC-SCALE-001",)
    name = "scale_confidence"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(context.review_summary.get("scale_flagged"))

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        value = context.review_summary.get("scale_confidence")
        rendered = f"{float(value):.2f}" if value is not None else None
        return [quality_finding(
            model,
            "QC-SCALE-001",
            f"Pixel→mm scale confidence {rendered or 'unknown'} is below "
            "threshold — no dimensional verdict is trustworthy; re-scale the "
            "plan (scale bar / known door width) and re-submit",
            object_type="scale",
            expected="scale confidence >= threshold",
            actual=rendered,
            source="quality.scale_confidence",
        )]
