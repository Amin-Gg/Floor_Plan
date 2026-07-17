"""Existing per-element detector/provenance confidence check."""
from __future__ import annotations

from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext, display_element_id
from ..findings import quality_finding


class ElementConfidenceCheck:
    code_prefix = "QC-ELEM-CONF"
    codes = ("QC-ELEM-CONF-001",)
    name = "element_confidence"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(context.review_summary.get("flagged"))

    @staticmethod
    def _space_tag_ids(model: BuildingModel, threshold: float) -> set[str]:
        ids: set[str] = set()
        for room in model.spaces:
            low = (
                room.category_source is not None
                and room.category_confidence is not None
                and float(room.category_confidence) < threshold
            )
            if room.category_source == "unmapped" or low:
                ids.add(display_element_id(room))
                ids.add(room.identity.internal_id)
                if room.identity.ifc_guid:
                    ids.add(room.identity.ifc_guid)
        return ids

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        review = context.review_summary
        threshold = context.threshold
        scale_low = bool(review.get("scale_flagged"))
        space_tag_ids = self._space_tag_ids(model, threshold)

        for row in review.get("flagged", []) or []:
            eid = row.get("id")
            reason = row.get("reason", "") or ""
            if scale_low and "scale confidence" in reason:
                continue
            if str(eid) in space_tag_ids and "category" in reason:
                continue
            element = context.resolve_element(eid)
            confidence = row.get("confidence")
            findings.append(quality_finding(
                model,
                "QC-ELEM-CONF-001",
                f"{row.get('collection', 'element')} {eid}: "
                f"{reason or 'flagged uncertain by detector provenance'} — "
                "its verdicts are withheld (NOT_EVALUATED) until the element "
                "is confirmed",
                element=element,
                element_id=eid,
                object_type=row.get("collection"),
                expected=f"confidence >= {threshold:.2f}",
                actual=(f"{float(confidence):.2f}" if confidence is not None else None),
                source="quality.element_confidence",
                details={"review_reason": reason},
            ))
        return findings
