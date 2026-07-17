"""Phase-6 IFC schema checker and explicit parsed-source contract."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from domain.findings import Finding, FindingStage
from domain.identifiers import fingerprint_file
from domain.validation import ValidationResult
from ingest.ifc_io import open_ifc_safely
from validation.schema.checks import (
    check_global_ids,
    check_mandatory_attributes,
    check_spatial_hierarchy,
    count_engine_products,
    safe_by_type,
)
from validation.schema.policy import SchemaValidationPolicy

SCHEMA_CHECKER_VERSION = "stage8-remediation-phase6"

BLOCKING_CODES = {
    "IFC-SCHEMA-001",
    "IFC-SCHEMA-002",
    "IFC-SCHEMA-003",
    "IFC-SCHEMA-004",
    "IFC-SCHEMA-005",
    "IFC-SCHEMA-006",
    "IFC-SCHEMA-007",
    "IFC-SCHEMA-008",
    "IFC-SCHEMA-010",
    "IFC-SCHEMA-011",
}


@dataclass(frozen=True)
class ParsedIfcSource:
    """One successful IFC parse shared by schema validation and ingest."""

    path: str
    model: Any
    model_name: str
    model_fingerprint: str
    schema: str


class SchemaFinding(Finding):
    """Backward-compatible constructor over the shared Finding contract."""

    def __init__(
        self,
        code: str,
        severity: str,
        message: str,
        entity: Optional[str] = None,
        guid: Optional[str] = None,
        *,
        model_name: Optional[str] = None,
        model_fingerprint: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        shared = Finding.schema(
            code=code,
            severity=severity,
            message=message,
            entity=entity,
            guid=guid,
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            details=details,
        )
        super().__init__(**shared.__dict__)

    @property
    def entity(self) -> Optional[str]:
        return self.element_type

    @property
    def guid(self) -> Optional[str]:
        return self.element_ifc_guid

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["entity"] = self.entity
        out["guid"] = self.guid
        return out


class SchemaValidationResult(ValidationResult):
    """Schema-specific thin wrapper over the shared ValidationResult."""

    def __init__(
        self,
        status: str,
        findings: Optional[list[Finding]] = None,
        schema: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata["schema"] = schema
        super().__init__(
            stage=FindingStage.SCHEMA,
            status=status,
            findings=list(findings or []),
            checker_version=kwargs.pop("checker_version", SCHEMA_CHECKER_VERSION),
            metadata=metadata,
            **kwargs,
        )
        self.schema = schema

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["schema"] = self.schema
        return out


class IfcSchemaError(ValueError):
    """Raised by the pipeline gate on a blocking L1 failure."""

    def __init__(self, result: SchemaValidationResult):
        self.result = result
        fails = [finding for finding in result.findings
                 if getattr(finding.severity, "value", finding.severity) == "fail"]
        super().__init__(
            "IFC schema validation failed (%d blocking issue%s): %s"
            % (
                len(fails),
                "" if len(fails) == 1 else "s",
                "; ".join(f"{finding.code}: {finding.message}" for finding in fails[:3])
                + ("; …" if len(fails) > 3 else ""),
            )
        )


def _schema_finding(
    code: str,
    severity: str,
    message: str,
    *,
    model_name: str | None,
    model_fingerprint: str,
    entity: str | None = None,
    guid: str | None = None,
    details: dict[str, Any] | None = None,
) -> SchemaFinding:
    return SchemaFinding(
        code,
        severity,
        message,
        entity=entity,
        guid=guid,
        model_name=model_name,
        model_fingerprint=model_fingerprint,
        details=details,
    )


def parse_ifc_source(ifc_path: str) -> ParsedIfcSource:
    """Parse once and return the explicit source context used downstream."""
    path = Path(ifc_path)
    if not path.is_file():
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")
    fingerprint = fingerprint_file(str(path))
    model = open_ifc_safely(path)
    schema = str(getattr(model, "schema", "") or "").upper()
    return ParsedIfcSource(
        path=str(path),
        model=model,
        model_name=path.name,
        model_fingerprint=fingerprint,
        schema=schema,
    )


def validate_parsed_ifc(
    source: ParsedIfcSource,
    *,
    policy: SchemaValidationPolicy | None = None,
) -> SchemaValidationResult:
    """Validate an already parsed source without reopening the IFC file."""
    policy = policy or SchemaValidationPolicy()
    model = source.model
    findings: list[Finding] = []

    if not policy.supports(source.schema):
        findings.append(_schema_finding(
            "IFC-SCHEMA-002",
            "fail",
            f"Unsupported IFC schema {source.schema!r}; supported: {policy.supported_label}",
            model_name=source.model_name,
            model_fingerprint=source.model_fingerprint,
            details={"actual_schema": source.schema, "supported_versions": sorted(policy.supported_versions),
                     "allow_ifc2x3": policy.allow_ifc2x3},
        ))
        return SchemaValidationResult("failed", findings, schema=source.schema,
                                      metadata={"policy": _policy_metadata(policy)})

    project_count = len(safe_by_type(model, "IfcProject"))
    if project_count != 1:
        findings.append(_schema_finding(
            "IFC-SCHEMA-003",
            "fail",
            f"Expected exactly one IfcProject, found {project_count}",
            model_name=source.model_name,
            model_fingerprint=source.model_fingerprint,
            entity="IfcProject",
        ))

    for code, class_name in (
        ("IFC-SCHEMA-004", "IfcSite"),
        ("IFC-SCHEMA-005", "IfcBuilding"),
        ("IFC-SCHEMA-006", "IfcBuildingStorey"),
    ):
        if not safe_by_type(model, class_name):
            findings.append(_schema_finding(
                code,
                "fail",
                f"No {class_name} in the model",
                model_name=source.model_name,
                model_fingerprint=source.model_fingerprint,
                entity=class_name,
            ))

    findings.extend(check_global_ids(
        model,
        policy=policy,
        model_name=source.model_name,
        model_fingerprint=source.model_fingerprint,
    ))
    findings.extend(check_spatial_hierarchy(
        model,
        policy=policy,
        model_name=source.model_name,
        model_fingerprint=source.model_fingerprint,
    ))
    findings.extend(check_mandatory_attributes(
        model,
        policy=policy,
        model_name=source.model_name,
        model_fingerprint=source.model_fingerprint,
    ))

    if count_engine_products(model) == 0:
        findings.append(_schema_finding(
            "IFC-SCHEMA-009",
            "alert",
            "No IfcSpace/IfcWall/IfcDoor/IfcWindow entities; geometric checks will be NOT_EVALUATED",
            model_name=source.model_name,
            model_fingerprint=source.model_fingerprint,
        ))

    has_fail = any(getattr(finding.severity, "value", finding.severity) == "fail" for finding in findings)
    has_alert = any(getattr(finding.severity, "value", finding.severity) == "alert" for finding in findings)
    status = "failed" if has_fail else "passed_with_alerts" if has_alert else "passed"
    return SchemaValidationResult(
        status,
        findings,
        schema=source.schema,
        metadata={
            "policy": _policy_metadata(policy),
            "model_fingerprint": source.model_fingerprint,
            "single_parse_context": True,
        },
    )


def _policy_metadata(policy: SchemaValidationPolicy) -> dict[str, Any]:
    return {
        "supported_versions": sorted(policy.supported_versions),
        "allow_ifc2x3": policy.allow_ifc2x3,
        "strict_mandatory_attributes": policy.strict_mandatory_attributes,
        "require_spatial_hierarchy": policy.require_spatial_hierarchy,
        "require_unique_global_ids": policy.require_unique_global_ids,
    }


def validate_ifc_schema_context(
    ifc_path: str,
    *,
    policy: SchemaValidationPolicy | None = None,
) -> tuple[SchemaValidationResult, ParsedIfcSource | None]:
    """Parse and validate, returning the explicit shared parse context."""
    model_name = os.path.basename(ifc_path) if ifc_path else None
    try:
        source = parse_ifc_source(ifc_path)
    except Exception as exc:
        fingerprint = ""
        if ifc_path and os.path.isfile(ifc_path):
            try:
                fingerprint = fingerprint_file(ifc_path)
            except OSError:
                pass
        finding = _schema_finding(
            "IFC-SCHEMA-001",
            "fail",
            f"File is not readable as IFC: {exc}" if os.path.isfile(ifc_path or "")
            else f"IFC file not found: {ifc_path}",
            model_name=model_name,
            model_fingerprint=fingerprint,
        )
        return SchemaValidationResult("failed", [finding], schema=None,
                                      metadata={"policy": _policy_metadata(policy or SchemaValidationPolicy())}), None
    return validate_parsed_ifc(source, policy=policy), source


def validate_ifc_schema(
    ifc_path: str,
    *,
    policy: SchemaValidationPolicy | None = None,
) -> tuple[SchemaValidationResult, Any]:
    """Backward-compatible API returning ``(result, parsed_model)``."""
    result, source = validate_ifc_schema_context(ifc_path, policy=policy)
    return result, source.model if source is not None else None


def require_valid_ifc(
    ifc_path: str,
    *,
    policy: SchemaValidationPolicy | None = None,
) -> tuple[SchemaValidationResult, Any]:
    result, model = validate_ifc_schema(ifc_path, policy=policy)
    if result.blocking:
        raise IfcSchemaError(result)
    return result, model


__all__ = [
    "BLOCKING_CODES",
    "DEFAULT_SUPPORTED_VERSIONS",
    "IfcSchemaError",
    "ParsedIfcSource",
    "SchemaFinding",
    "SchemaValidationPolicy",
    "SchemaValidationResult",
    "parse_ifc_source",
    "require_valid_ifc",
    "validate_ifc_schema",
    "validate_ifc_schema_context",
    "validate_parsed_ifc",
]
