"""Finding helpers shared by quality-check plugins."""
from __future__ import annotations

from typing import Any, Optional

from domain.elements import ElementBase
from domain.findings import Finding, FindingSeverity, FindingStage, Verdict
from domain.model import BuildingModel

from .context import display_element_id


def quality_finding(
    model: BuildingModel,
    code: str,
    message: str,
    *,
    element: Optional[ElementBase] = None,
    element_id: Optional[str] = None,
    object_type: Optional[str] = None,
    expected: Any = None,
    actual: Any = None,
    severity: FindingSeverity | str = FindingSeverity.ALERT,
    source: str = "quality_checker",
    details: Optional[dict[str, Any]] = None,
) -> Finding:
    identity = element.identity if element is not None else None
    legacy_id = (
        display_element_id(element)
        if element is not None
        else (str(element_id) if element_id is not None else None)
    )
    resolved_type = object_type or (
        type(element).__name__.lower() if element is not None else None
    )
    return Finding(
        article_id=code,
        verdict=Verdict.NOT_EVALUATED,
        message=message,
        object=resolved_type,
        element_id=legacy_id,
        category=FindingStage.QUALITY.value,
        code=code,
        severity=severity,
        element_internal_id=identity.internal_id if identity else None,
        element_ifc_guid=identity.ifc_guid if identity else None,
        element_type=type(element).__name__ if element is not None else resolved_type,
        model_name=(identity.model_name if identity else None)
        or model.provenance.model_name,
        model_fingerprint=model.provenance.model_fingerprint,
        storey_id=element.storey_id if element is not None else None,
        expected=expected,
        actual=actual,
        source=source,
        details=dict(details or {}),
    )


def plugin_error_finding(
    model: BuildingModel,
    *,
    plugin_name: str,
    code_prefix: str,
    blocking: bool,
    error: BaseException,
    phase: str,
) -> Finding:
    severity = FindingSeverity.FAIL if blocking else FindingSeverity.ALERT
    return quality_finding(
        model,
        "QC-INTERNAL-001",
        f"Quality check '{plugin_name}' failed during {phase}: "
        f"{type(error).__name__}: {error}",
        object_type="quality_checker",
        expected=f"plugin {plugin_name!r} completes without error",
        actual=type(error).__name__,
        severity=severity,
        source="quality_checker.registry",
        details={
            "plugin": plugin_name,
            "code_prefix": code_prefix,
            "phase": phase,
            "exception_type": type(error).__name__,
            "blocking_plugin": blocking,
        },
    )
