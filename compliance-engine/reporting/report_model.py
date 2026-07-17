"""Versioned, authoritative validation-report model.

Every output format is generated from :class:`ValidationReport`. The builder
normalises stage payloads to the shared ``Finding`` shape, applies one
overall-status policy, removes sensitive/local path data, and sorts findings
deterministically.
"""
from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from domain.findings import (
    Finding,
    FindingSeverity,
    FindingStage,
    Verdict,
)
from domain.model import BuildingModel
from domain.validation import ValidationResult

REPORT_SCHEMA_VERSION = "1.0"
ENGINE_VERSION = "stage8-remediation-phase9-final-r2"
REPORT_RUN_NAMESPACE = uuid.UUID("292779be-0268-5415-a6fb-2788d52542f8")

_STAGE_ORDER = {"schema": 0, "quality": 1, "compliance": 2}
_SEVERITY_ORDER = {"fail": 0, "alert": 1, "info": 2}
_VERDICT_ORDER = {
    "FAIL": 0,
    "NEEDS_REVIEW": 1,
    "NOT_EVALUATED": 2,
    "PASS": 3,
    "NOT_APPLICABLE": 4,
}

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential|"
    r"private[_-]?key|access[_-]?key|refresh[_-]?token|connection[_-]?string)",
    re.IGNORECASE,
)
_PATH_KEY = re.compile(
    r"(?:^|_)(?:path|filepath|file_path|source_path|output_dir|out_dir|cwd|home)(?:$|_)",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class OverallCode(str, Enum):
    REJECTED = "rejected"
    NON_COMPLIANT = "non_compliant"
    INCOMPLETE = "incomplete"
    NEEDS_REVIEW = "needs_review"
    COMPLIANT_WITH_QUALITY_ALERTS = "compliant_with_quality_alerts"
    COMPLIANT = "compliant"
    PRECHECK_FAILED = "precheck_failed"
    PRECHECK_READY_WITH_ALERTS = "precheck_ready_with_alerts"
    PRECHECK_READY = "precheck_ready"


@dataclass(frozen=True)
class OverallStatus:
    code: OverallCode
    label: str
    status: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "label": self.label,
            "status": self.status,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReportModelInfo:
    name: Optional[str]
    source_type: str
    ifc_schema: Optional[str] = None
    project_guid: Optional[str] = None
    fingerprint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_type": self.source_type,
            "ifc_schema": self.ifc_schema,
            "project_guid": self.project_guid,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ValidationReport:
    report_schema_version: str
    engine_version: str
    run_id: str
    generated_at: str
    mode: str
    model: ReportModelInfo
    overall: OverallStatus
    stages: Mapping[str, Optional[dict[str, Any]]]
    summary: Mapping[str, Any]
    findings: tuple[dict[str, Any], ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    skipped_stages: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Field insertion order is the public JSON presentation order.  Nested
        # arrays/maps have already been sorted/normalised by the builder.
        return {
            "report_schema_version": self.report_schema_version,
            "engine_version": self.engine_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "model": self.model.to_dict(),
            "overall": self.overall.to_dict(),
            "stages": {
                "schema": self.stages.get("schema"),
                "quality": self.stages.get("quality"),
                "compliance": self.stages.get("compliance"),
            },
            "summary": dict(self.summary),
            "findings": [dict(item) for item in self.findings],
            "metadata": dict(self.metadata),
            "skipped_stages": dict(self.skipped_stages),
        }


@dataclass(frozen=True)
class StageReport:
    """Backward-compatible Phase-1 wrapper retained for callers/tests."""

    result: ValidationResult

    def to_dict(self) -> dict[str, Any]:
        return self.result.to_dict()


def _utc_iso(value: datetime | str | None = None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any, *, forbidden_paths: Sequence[str] = ()) -> Any:
    """Convert arbitrary values to JSON-safe data and redact deployment data."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_iso(value)
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            text = value
            for raw in forbidden_paths:
                if raw:
                    text = text.replace(raw, Path(raw).name or "[local path]")
            # A value that is itself an absolute local path is never useful in
            # a portable report. Keep only its basename.
            if os.path.isabs(text) or _WINDOWS_ABSOLUTE.match(text):
                return Path(text.replace("\\", "/")).name or "[local path]"
            return text
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key in sorted((str(k) for k in value), key=str.casefold):
            original = value[key] if key in value else next(
                value[k] for k in value if str(k) == key
            )
            if _SENSITIVE_KEY.search(key):
                continue
            if _PATH_KEY.search(key):
                # Preserve only a portable filename when a path key is useful.
                safe = _json_safe(original, forbidden_paths=forbidden_paths)
                if safe not in (None, ""):
                    clean[key.replace("path", "name")] = safe
                continue
            clean[key] = _json_safe(original, forbidden_paths=forbidden_paths)
        return clean
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, forbidden_paths=forbidden_paths) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_json_safe(v, forbidden_paths=forbidden_paths) for v in value),
            key=lambda item: repr(item),
        )
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict(), forbidden_paths=forbidden_paths)
    return str(value)


def sanitize_metadata(
    metadata: Optional[Mapping[str, Any]],
    *,
    forbidden_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Return deterministic metadata with secrets and local paths removed."""
    cleaned = _json_safe(dict(metadata or {}), forbidden_paths=forbidden_paths)
    assert isinstance(cleaned, dict)
    return cleaned


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _stage_from(value: Any, default: str) -> FindingStage:
    raw = str(value or default).lower()
    try:
        return FindingStage(raw)
    except ValueError:
        return FindingStage(default)


def _verdict_from(value: Any, stage: FindingStage, severity: Any) -> Verdict:
    if isinstance(value, Verdict):
        return value
    if value is not None:
        try:
            return Verdict(str(value))
        except ValueError:
            pass
    sev = str(getattr(severity, "value", severity) or "").lower()
    if sev == "fail":
        return Verdict.FAIL
    if stage is FindingStage.COMPLIANCE:
        return Verdict.NEEDS_REVIEW
    return Verdict.NOT_EVALUATED


def _severity_from(value: Any, stage: FindingStage, verdict: Verdict) -> FindingSeverity:
    if isinstance(value, FindingSeverity):
        return value
    if value is not None:
        try:
            return FindingSeverity(str(value).lower())
        except ValueError:
            pass
    if verdict is Verdict.FAIL:
        return FindingSeverity.FAIL
    if stage is FindingStage.COMPLIANCE and verdict is Verdict.PASS:
        return FindingSeverity.INFO
    return FindingSeverity.ALERT if verdict in {
        Verdict.NEEDS_REVIEW,
        Verdict.NOT_EVALUATED,
    } else FindingSeverity.INFO


def _normalise_finding(
    raw: Mapping[str, Any] | Finding,
    *,
    stage_name: str,
    model: ReportModelInfo,
    forbidden_paths: Sequence[str],
    ordinal: int = 0,
) -> dict[str, Any]:
    if isinstance(raw, Finding):
        data = raw.to_dict()
    else:
        data = dict(raw)

    stage = _stage_from(data.get("stage") or data.get("category"), stage_name)
    verdict = _verdict_from(data.get("verdict"), stage, data.get("severity"))
    severity = _severity_from(data.get("severity"), stage, verdict)
    code = data.get("code") or (
        data.get("article_id") if stage is not FindingStage.COMPLIANCE else None
    )
    article_id = str(data.get("article_id") or data.get("clause_id") or code or "")
    legacy_element_id = data.get("element_id")
    internal_id = data.get("element_internal_id") or legacy_element_id
    ifc_guid = data.get("element_ifc_guid")
    model_fingerprint = data.get("model_fingerprint") or model.fingerprint or ""

    finding = Finding(
        article_id=article_id,
        verdict=verdict,
        message=str(data.get("message") or ""),
        object=data.get("object") or data.get("element_type"),
        measured=data.get("measured"),
        required=data.get("required"),
        unit=data.get("unit"),
        element_id=str(legacy_element_id) if legacy_element_id is not None else (
            str(internal_id) if internal_id is not None else None
        ),
        rule_text_en=data.get("rule_text_en") or data.get("clause_text"),
        category=stage.value,
        code=str(code) if code is not None else None,
        expected=data.get("expected"),
        actual=data.get("actual"),
        ordinal=int(data.get("ordinal") or ordinal or 0),
        unsupported=bool(data.get("unsupported", False)),
        severity=severity,
        element_internal_id=str(internal_id) if internal_id is not None else None,
        element_ifc_guid=str(ifc_guid) if ifc_guid is not None else None,
        element_type=data.get("element_type") or data.get("object"),
        model_name=data.get("model_name") or model.name,
        model_fingerprint=str(model_fingerprint),
        storey_id=data.get("storey_id"),
        requirement=data.get("requirement") or data.get("clause_text") or data.get("rule_text_en"),
        clause_id=data.get("clause_id") or (article_id if stage is FindingStage.COMPLIANCE else None),
        clause_text=data.get("clause_text") or data.get("rule_text_en"),
        source=data.get("source"),
        details=dict(data.get("details") or {}),
    )
    result = finding.to_dict()
    supplied_id = data.get("finding_id")
    if supplied_id and _is_uuid(supplied_id):
        result["finding_id"] = str(uuid.UUID(str(supplied_id)))
    return _json_safe(result, forbidden_paths=forbidden_paths)


def _finding_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _STAGE_ORDER.get(str(item.get("stage")), 99),
        _SEVERITY_ORDER.get(str(item.get("severity")), 99),
        _VERDICT_ORDER.get(str(item.get("verdict")), 99),
        str(item.get("code") or ""),
        str(item.get("clause_id") or item.get("article_id") or ""),
        str(item.get("element_ifc_guid") or item.get("element_internal_id") or ""),
        str(item.get("finding_id") or ""),
    )


def _normalise_stage(
    stage_name: str,
    stage: Optional[Mapping[str, Any] | ValidationResult],
    *,
    model: ReportModelInfo,
    forbidden_paths: Sequence[str],
    skipped_reason: Optional[str] = None,
    fallback_summary: Optional[Mapping[str, Any]] = None,
    fallback_coverage: Optional[Mapping[str, Any]] = None,
    fallback_duration: Any = None,
) -> Optional[dict[str, Any]]:
    if stage is None and skipped_reason is None:
        return None
    if isinstance(stage, ValidationResult):
        raw: dict[str, Any] = stage.to_dict()
    else:
        raw = dict(stage or {})

    source_findings = list(raw.get("findings") or [])
    # Existing shared findings already carry IDs.  Legacy duplicates receive a
    # deterministic occurrence ordinal after canonical pre-sort.
    source_findings.sort(key=lambda f: repr(_json_safe(
        f.to_dict() if isinstance(f, Finding) else dict(f),
        forbidden_paths=forbidden_paths,
    )))
    seen_basis: dict[tuple[Any, ...], int] = {}
    findings: list[dict[str, Any]] = []
    for item in source_findings:
        data = item.to_dict() if isinstance(item, Finding) else dict(item)
        basis = (
            data.get("code"), data.get("article_id"), data.get("clause_id"),
            data.get("element_ifc_guid"), data.get("element_internal_id"),
            data.get("element_id"), data.get("message"),
        )
        ordinal = seen_basis.get(basis, 0)
        seen_basis[basis] = ordinal + 1
        findings.append(_normalise_finding(
            item,
            stage_name=stage_name,
            model=model,
            forbidden_paths=forbidden_paths,
            ordinal=ordinal,
        ))
    findings.sort(key=_finding_sort_key)

    if stage_name in {"schema", "quality"}:
        status = raw.get("status") or (
            "failed" if any(f["severity"] == "fail" for f in findings)
            else "passed_with_alerts" if findings else "passed"
        )
    else:
        status = raw.get("status") or (
            "blocked" if any(
                f["verdict"] == "NOT_EVALUATED" and f["severity"] == "fail"
                for f in findings
            )
            else "completed_with_review" if any(
                f["verdict"] in {"NEEDS_REVIEW", "NOT_EVALUATED"}
                for f in findings
            )
            else "completed"
        )

    summary = raw.get("summary") if raw.get("summary") is not None else fallback_summary
    coverage = raw.get("coverage") if raw.get("coverage") is not None else fallback_coverage
    duration = raw.get("duration_s") if raw.get("duration_s") is not None else fallback_duration
    return {
        "stage": stage_name,
        "status": str(status),
        "checker_version": raw.get("checker_version"),
        "started_at": _utc_iso(raw["started_at"]) if raw.get("started_at") else None,
        "completed_at": _utc_iso(raw["completed_at"]) if raw.get("completed_at") else None,
        "metadata": sanitize_metadata(raw.get("metadata"), forbidden_paths=forbidden_paths),
        "summary": _json_safe(summary, forbidden_paths=forbidden_paths) if summary is not None else None,
        "coverage": _json_safe(coverage, forbidden_paths=forbidden_paths) if coverage is not None else None,
        "duration_s": float(duration) if isinstance(duration, (int, float)) and math.isfinite(float(duration)) else None,
        "skipped": stage is None and skipped_reason is not None,
        "skip_reason": str(skipped_reason) if skipped_reason is not None else None,
        "findings": findings,
    }


def _model_info(
    model: Optional[BuildingModel | Mapping[str, Any]],
    *,
    metadata: Mapping[str, Any],
    schema_stage: Optional[Mapping[str, Any]],
) -> tuple[ReportModelInfo, tuple[str, ...]]:
    paths: list[str] = []
    if isinstance(model, BuildingModel):
        provenance = model.provenance
        if provenance.source_path:
            paths.append(provenance.source_path)
        name = provenance.model_name or (
            Path(provenance.source_path).name if provenance.source_path else None
        )
        return ReportModelInfo(
            name=name,
            source_type=provenance.source_type or "unknown",
            ifc_schema=provenance.ifc_schema,
            project_guid=model.project_id,
            fingerprint=provenance.model_fingerprint or None,
        ), tuple(paths)

    raw = dict(model or {})
    source_path = raw.get("source_path") or metadata.get("source_path") or metadata.get("ifc_path")
    if source_path:
        paths.append(str(source_path))
    stage_meta = dict((schema_stage or {}).get("metadata") or {})
    name = raw.get("name") or raw.get("model_name") or metadata.get("plan_name")
    if not name and source_path:
        name = Path(str(source_path)).name
    if name and (os.path.isabs(str(name)) or _WINDOWS_ABSOLUTE.match(str(name))):
        name = Path(str(name).replace("\\", "/")).name
    return ReportModelInfo(
        name=str(name) if name else None,
        source_type=str(raw.get("source_type") or metadata.get("source_type") or "unknown"),
        ifc_schema=(raw.get("ifc_schema") or stage_meta.get("schema") or
                    (schema_stage or {}).get("schema")),
        project_guid=raw.get("project_guid") or raw.get("project_id"),
        fingerprint=raw.get("fingerprint") or raw.get("model_fingerprint") or stage_meta.get("model_fingerprint"),
    ), tuple(paths)


def compute_overall_status(
    *,
    mode: str,
    stages: Mapping[str, Optional[Mapping[str, Any]]],
    skipped_stages: Optional[Mapping[str, str]] = None,
) -> OverallStatus:
    """Central, format-independent report status policy."""
    skipped = dict(skipped_stages or {})
    schema = stages.get("schema") or {}
    quality = stages.get("quality") or {}
    compliance = stages.get("compliance") or {}

    if schema.get("status") == "failed":
        return OverallStatus(
            OverallCode.REJECTED,
            "rejected — IFC schema invalid",
            "error",
            ("IFC schema validation failed; downstream checks are not authoritative.",),
        )

    if mode == "precheck":
        if quality.get("status") == "failed":
            return OverallStatus(
                OverallCode.PRECHECK_FAILED,
                "precheck failed — model quality errors",
                "error",
                ("Model-quality validation contains blocking failures.",),
            )
        if quality.get("status") == "passed_with_alerts":
            return OverallStatus(
                OverallCode.PRECHECK_READY_WITH_ALERTS,
                "precheck complete — model-quality alerts",
                "warning",
                ("Regulatory compliance was not run in precheck mode.",),
            )
        return OverallStatus(
            OverallCode.PRECHECK_READY,
            "precheck complete",
            "success",
            ("Regulatory compliance was not run in precheck mode.",),
        )

    findings = list(compliance.get("findings") or [])
    verdicts: dict[str, int] = {}
    for finding in findings:
        verdict = str(finding.get("verdict") or "")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1

    if compliance.get("status") == "blocked":
        return OverallStatus(
            OverallCode.INCOMPLETE,
            "incomplete — compliance stage blocked",
            "incomplete",
            (str(skipped.get("compliance") or "Compliance could not be completed."),),
        )
    if verdicts.get("FAIL", 0):
        return OverallStatus(
            OverallCode.NON_COMPLIANT,
            "non-compliant",
            "error",
            (f"{verdicts['FAIL']} deterministic compliance failure(s) found.",),
        )
    if verdicts.get("NOT_EVALUATED", 0):
        return OverallStatus(
            OverallCode.INCOMPLETE,
            "incomplete — checks not evaluated",
            "incomplete",
            (f"{verdicts['NOT_EVALUATED']} check(s) lack trustworthy required data.",),
        )
    if verdicts.get("NEEDS_REVIEW", 0):
        return OverallStatus(
            OverallCode.NEEDS_REVIEW,
            "needs review",
            "warning",
            (f"{verdicts['NEEDS_REVIEW']} check(s) require qualified human review.",),
        )
    if not compliance and "compliance" in skipped:
        return OverallStatus(
            OverallCode.INCOMPLETE,
            "incomplete — compliance not run",
            "incomplete",
            (str(skipped["compliance"]),),
        )
    if quality.get("status") in {"failed", "passed_with_alerts"} or quality.get("findings"):
        if quality.get("status") == "failed":
            return OverallStatus(
                OverallCode.INCOMPLETE,
                "incomplete — model quality failed",
                "incomplete",
                ("Compliance results cannot establish a clean model state.",),
            )
        return OverallStatus(
            OverallCode.COMPLIANT_WITH_QUALITY_ALERTS,
            "compliant — with model-quality alerts",
            "warning",
            ("No deterministic compliance failures were found, but model-quality alerts remain.",),
        )
    return OverallStatus(
        OverallCode.COMPLIANT,
        "compliant",
        "success",
        (),
    )


def _summary(
    findings: Sequence[Mapping[str, Any]],
    stages: Mapping[str, Optional[Mapping[str, Any]]],
) -> dict[str, Any]:
    by_stage = {"schema": 0, "quality": 0, "compliance": 0}
    by_severity = {"fail": 0, "alert": 0, "info": 0}
    verdicts = {
        "PASS": 0,
        "FAIL": 0,
        "NEEDS_REVIEW": 0,
        "NOT_EVALUATED": 0,
        "NOT_APPLICABLE": 0,
    }
    for item in findings:
        stage = str(item.get("stage") or "")
        if stage in by_stage:
            by_stage[stage] += 1
        severity = str(item.get("severity") or "")
        if severity in by_severity:
            by_severity[severity] += 1
        verdict = str(item.get("verdict") or "")
        if verdict in verdicts:
            verdicts[verdict] += 1
    compliance = stages.get("compliance") or {}
    return {
        "findings_total": len(findings),
        "findings_by_stage": by_stage,
        "findings_by_severity": by_severity,
        "verdicts": verdicts,
        "coverage": dict(compliance.get("coverage") or {}),
    }


def _standards_versions() -> dict[str, Optional[str]]:
    try:
        from standards.loaders import load_controlled_values, load_semantic_catalog
        return {
            "semantic_catalog": load_semantic_catalog().version,
            "controlled_values": load_controlled_values().version,
        }
    except Exception:
        # Report creation must not hide a standards startup failure in normal
        # application startup; this fallback only keeps legacy standalone
        # report generation portable.
        return {"semantic_catalog": None, "controlled_values": None}


def build_validation_report(
    *,
    compliance: Optional[Mapping[str, Any]] = None,
    schema: Optional[Mapping[str, Any] | ValidationResult] = None,
    quality: Optional[Mapping[str, Any] | ValidationResult] = None,
    model: Optional[BuildingModel | Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    mode: str = "full_check",
    skipped_stages: Optional[Mapping[str, str]] = None,
    coverage: Optional[Mapping[str, Any]] = None,
    engine_version: str = ENGINE_VERSION,
    generated_at: datetime | str | None = None,
    run_id: Optional[str] = None,
) -> ValidationReport:
    """Build the single authoritative report used by JSON/HTML/PDF/BCF."""
    if mode not in {"precheck", "full_check"}:
        raise ValueError("mode must be 'precheck' or 'full_check'")
    raw_metadata = dict(metadata or {})
    schema_dict = schema.to_dict() if isinstance(schema, ValidationResult) else dict(schema or {})
    model_info, forbidden_paths = _model_info(
        model,
        metadata=raw_metadata,
        schema_stage=schema_dict,
    )
    skipped = {
        str(key): str(value)
        for key, value in sorted(dict(skipped_stages or {}).items())
    }
    if schema is None and model_info.source_type in {"building_model", "bim_data"}:
        skipped.setdefault("schema", "not applicable to non-IFC source")
    if mode == "precheck":
        skipped.setdefault("compliance", "precheck mode runs schema and quality only")

    compliance_raw = dict(compliance or {})
    stages: dict[str, Optional[dict[str, Any]]] = {
        "schema": _normalise_stage(
            "schema", schema,
            model=model_info,
            forbidden_paths=forbidden_paths,
            skipped_reason=skipped.get("schema") if schema is None else None,
        ),
        "quality": _normalise_stage(
            "quality", quality,
            model=model_info,
            forbidden_paths=forbidden_paths,
            skipped_reason=skipped.get("quality") if quality is None else None,
        ),
        "compliance": _normalise_stage(
            "compliance", compliance_raw if compliance is not None else None,
            model=model_info,
            forbidden_paths=forbidden_paths,
            skipped_reason=skipped.get("compliance") if compliance is None else None,
            fallback_summary=compliance_raw.get("summary"),
            fallback_coverage=coverage,
            fallback_duration=compliance_raw.get("duration_s"),
        ),
    }

    findings: list[dict[str, Any]] = []
    for stage_name in ("schema", "quality", "compliance"):
        stage_data = stages.get(stage_name)
        if stage_data:
            findings.extend(stage_data.get("findings") or [])
    findings.sort(key=_finding_sort_key)

    overall = compute_overall_status(mode=mode, stages=stages, skipped_stages=skipped)
    safe_meta = sanitize_metadata(raw_metadata, forbidden_paths=forbidden_paths)
    safe_meta.setdefault("standards_versions", _standards_versions())
    generated = _utc_iso(generated_at)
    if run_id is None:
        basis = "\x1f".join([
            model_info.fingerprint or model_info.name or "unknown-model",
            mode,
            generated,
            str(len(findings)),
        ])
        run_id = str(uuid.uuid5(REPORT_RUN_NAMESPACE, basis))
    elif not _is_uuid(run_id):
        raise ValueError("run_id must be a UUID")
    else:
        run_id = str(uuid.UUID(str(run_id)))

    return ValidationReport(
        report_schema_version=REPORT_SCHEMA_VERSION,
        engine_version=str(engine_version),
        run_id=run_id,
        generated_at=generated,
        mode=mode,
        model=model_info,
        overall=overall,
        stages=stages,
        summary=_summary(findings, stages),
        findings=tuple(findings),
        metadata=safe_meta,
        skipped_stages=skipped,
    )


__all__ = [
    "ENGINE_VERSION",
    "REPORT_SCHEMA_VERSION",
    "OverallCode",
    "OverallStatus",
    "ReportModelInfo",
    "StageReport",
    "ValidationReport",
    "build_validation_report",
    "compute_overall_status",
    "sanitize_metadata",
]
