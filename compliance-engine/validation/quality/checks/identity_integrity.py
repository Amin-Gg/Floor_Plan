"""Element-identity integrity check.

ADR-001 defines a deterministic geometry-key fallback for elements that
arrive with neither an IFC GlobalId nor a source/detector ID, and mandates
that using the fallback must surface as a Quality alert: such elements
cannot be reliably targeted by Manual-Inputs element overrides or selected
in BCF topics, and two geometrically identical unidentified elements would
collide on the same internal ID. The flag was recorded on
``ElementIdentity.used_geometry_fallback`` in Phase 1; this plugin (added
after the final independent review) is its consumer.

Non-blocking by design: a fallback identity degrades addressability, not
the trustworthiness of the measurements the deterministic agents evaluate.
"""
from __future__ import annotations

from typing import Iterator

from domain.elements import ElementBase
from domain.findings import Finding
from domain.model import BuildingModel

from ..context import QualityContext, display_element_id
from ..findings import quality_finding

_COLLECTIONS = (
    "storeys", "walls", "doors", "windows", "spaces", "stairs", "slabs",
)


def _iter_elements(model: BuildingModel) -> Iterator[tuple[str, ElementBase]]:
    for collection in _COLLECTIONS:
        for element in getattr(model, collection, []) or []:
            yield collection, element


class IdentityIntegrityCheck:
    code_prefix = "QC-IDENT"
    codes = ("QC-IDENT-001",)
    name = "identity_integrity"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return any(
            element.identity.used_geometry_fallback
            for _, element in _iter_elements(model)
        )

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        for collection, element in _iter_elements(model):
            identity = element.identity
            if not identity.used_geometry_fallback:
                continue
            findings.append(quality_finding(
                model,
                "QC-IDENT-001",
                f"{collection[:-1]} {display_element_id(element)}: identity "
                "derived from the geometry fallback (no IFC GlobalId and no "
                "source ID). The element cannot be reliably targeted by "
                "manual-input element overrides or BCF component selection, "
                "and a geometrically identical unidentified element would "
                "receive the same internal ID.",
                element=element,
                expected="element supplies an IFC GlobalId or a stable source ID",
                actual="geometry-fallback identity",
                details={
                    "collection": collection,
                    "internal_id": identity.internal_id,
                    "used_geometry_fallback": True,
                },
            ))
        return findings
