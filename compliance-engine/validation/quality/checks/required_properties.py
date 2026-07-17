"""Catalog-driven required semantic property checks."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from domain.elements import ElementBase
from domain.findings import Finding
from domain.model import BuildingModel
from standards.catalog_api import quality_requirements

from ..context import QualityContext, display_element_id
from ..findings import quality_finding


def _elements(model: BuildingModel, kind: str) -> Iterable[ElementBase]:
    return {
        "wall": model.walls,
        "door": model.doors,
        "window": model.windows,
        "space": model.spaces,
    }.get(kind, ())


def _invalid(value: Any, spec: dict[str, Any]) -> bool:
    if value is None or value == "":
        return True
    data_type = spec.get("data_type")
    if data_type == "number":
        if isinstance(value, bool):
            return True
        try:
            number = float(value)
        except (TypeError, ValueError):
            return True
        minimum = spec.get("min_value")
        maximum = spec.get("max_value")
        if minimum is not None and number < float(minimum):
            return True
        if maximum is not None and number > float(maximum):
            return True
    return False


class RequiredPropertiesCheck:
    code_prefix = "QC-PROP"
    codes = ("QC-PROP-001", "QC-PROP-002")
    name = "required_properties"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return bool(model.walls or model.doors or model.windows or model.spaces)

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        findings: list[Finding] = []
        contract = quality_requirements()
        for kind, requirements in contract.items():
            for element in _elements(model, kind):
                for property_name, spec in requirements.items():
                    if not bool(spec.get("required", False)):
                        continue
                    value = getattr(element, property_name, None)
                    if _invalid(value, spec):
                        findings.append(quality_finding(
                            model,
                            "QC-PROP-001",
                            f"{kind} {display_element_id(element)}: required "
                            f"property '{property_name}' is missing or invalid.",
                            element=element,
                            object_type=kind,
                            expected={
                                "property": property_name,
                                "unit": spec.get("unit"),
                            },
                            actual=value,
                            source="quality.required_properties",
                            details={
                                "property": property_name,
                                "required_for": list(spec.get("required_for") or []),
                                "blocks_capabilities": list(
                                    spec.get("required_for") or [f"{kind}.{property_name}"]
                                ),
                            },
                        ))

                mapping_issues = element.properties.get("_mapping_issues") \
                    or element.extras.get("_property_mapping_issues") or []
                if isinstance(mapping_issues, dict):
                    mapping_issues = [mapping_issues]
                if isinstance(mapping_issues, list):
                    for issue in mapping_issues:
                        if not isinstance(issue, dict):
                            continue
                        prop_name = issue.get("property") or issue.get("property_name")
                        findings.append(quality_finding(
                            model,
                            "QC-PROP-002",
                            f"{kind} {display_element_id(element)}: property "
                            f"'{prop_name or 'unknown'}' is mapped to the wrong "
                            "element, property set, or semantic field.",
                            element=element,
                            object_type=kind,
                            expected=issue.get("expected") or {
                                "pset": issue.get("expected_pset"),
                                "property": issue.get("expected_property"),
                            },
                            actual=issue.get("actual") or {
                                "pset": issue.get("actual_pset"),
                                "property": issue.get("actual_property"),
                            },
                            source="quality.required_properties",
                            details={"mapping_issue": dict(issue)},
                        ))
        return findings
