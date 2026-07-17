"""Unified Stage-2 validation pipeline.

All public entry points delegate here. The deterministic compliance orchestrator
remains unchanged and is called only after source parsing, manual-input merge,
and model-quality validation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from domain.model import BuildingModel
from ingest.category_normalizer import normalize_room_categories
from ingest.ifc_to_bim_data import ifc_to_building_model
from ingest.review_prepass import apply_review_prepass, downgrade_flagged_findings
from validation.schema import (ParsedIfcSource, SchemaValidationPolicy,
                               SchemaValidationResult, validate_ifc_schema_context)
from manual_inputs import (
    ManualInputs,
    ManualInputsError,
    merge_manual_inputs,
    parse_manual_inputs,
    reject_legacy_building_params,
)
from validation.compliance.adapter import building_model_from_bim_data, building_model_to_bim_data


class PipelineSourceType(str, Enum):
    IFC = "ifc"
    BUILDING_MODEL = "building_model"
    BIM_DATA = "bim_data"


class PipelineMode(str, Enum):
    PRECHECK = "precheck"
    FULL_CHECK = "full_check"


@dataclass
class PipelineRequest:
    source_type: PipelineSourceType | str
    clauses: list[dict[str, Any]] = field(default_factory=list)
    out_dir: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    mode: PipelineMode | str = PipelineMode.FULL_CHECK

    ifc_path: Optional[str] = None
    building_model: Optional[BuildingModel] = None
    bim_data: Optional[dict[str, Any]] = None
    manual_inputs: ManualInputs | dict[str, Any] | str | None = None

    threshold: Optional[float] = None
    corpus_total: Optional[int] = None
    retriever: Optional[Any] = None
    llm: Optional[Callable[[str], str]] = None
    use_langgraph: bool = False
    generate_reports: bool = True
    schema_policy: Optional[SchemaValidationPolicy] = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_type, PipelineSourceType):
            self.source_type = PipelineSourceType(str(self.source_type))
        if not isinstance(self.mode, PipelineMode):
            self.mode = PipelineMode(str(self.mode))
        supplied = {
            PipelineSourceType.IFC: self.ifc_path is not None,
            PipelineSourceType.BUILDING_MODEL: self.building_model is not None,
            PipelineSourceType.BIM_DATA: self.bim_data is not None,
        }
        if not supplied[self.source_type]:
            raise ValueError(f"source_type={self.source_type.value!r} requires its matching source payload")
        if sum(bool(v) for v in supplied.values()) != 1:
            raise ValueError("PipelineRequest must contain exactly one source payload")


@dataclass
class PipelineExecution:
    request: PipelineRequest
    building_model: Optional[BuildingModel] = None
    bim_data: Optional[dict[str, Any]] = None
    schema: Optional[dict[str, Any]] = None
    schema_result: Optional[SchemaValidationResult] = field(default=None, repr=False)
    parsed_source: Optional[ParsedIfcSource] = field(default=None, repr=False)
    quality: Optional[dict[str, Any]] = None
    compliance: Optional[Any] = None
    coverage: dict[str, Any] = field(default_factory=dict)
    reports: dict[str, Optional[str]] = field(default_factory=dict)
    manual_input_metadata: dict[str, Any] = field(default_factory=dict)
    skipped_stages: dict[str, str] = field(default_factory=dict)
    stage_trace: list[str] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)

    def to_dict(self) -> dict[str, Any]:
        compliance_dict = self.compliance.to_dict() if self.compliance is not None else None
        return {
            "mode": self.request.mode.value,
            "source_type": self.request.source_type.value,
            "schema": self.schema,
            "quality": self.quality,
            "compliance": compliance_dict,
            "coverage": dict(self.coverage),
            "reports": dict(self.reports),
            "manual_inputs": dict(self.manual_input_metadata),
            "skipped_stages": dict(self.skipped_stages),
            "stage_trace": list(self.stage_trace),
            "blocked_reason": self.blocked_reason,
        }

    def to_api_response(self) -> dict[str, Any]:
        result = self.compliance
        review = (self.bim_data or {}).get("_review_summary", {}) or {}
        payload = {
            "summary": dict(result.summary) if result is not None else {},
            "coverage": dict(self.coverage),
            "duration_s": round(result.duration_s, 3) if result is not None else 0.0,
            "n_findings": len(result.findings) if result is not None else 0,
            "reports": {
                key: (os.path.basename(value) if value else None)
                for key, value in self.reports.items()
            },
            "quality": self.quality,
            "manual_inputs": dict(self.manual_input_metadata),
            "skipped_stages": dict(self.skipped_stages),
            "stage_trace": list(self.stage_trace),
        }
        if self.schema is not None:
            payload["schema"] = self.schema
        if self.request.source_type is PipelineSourceType.IFC:
            payload.update({
                "flagged_count": review.get("flagged_count", 0),
                "downgraded_count": review.get("downgraded_count", 0),
                "category_summary": (self.bim_data or {}).get("_category_summary", {}),
                "categories_seen": (self.bim_data or {}).get("_categories_seen", {}),
                "schema_version": (self.bim_data or {}).get("schema_version"),
            })
        if self.blocked_reason:
            payload["blocked_reason"] = self.blocked_reason
        return payload


def _categories_seen(bim_data: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for room in bim_data.get("rooms", []) or []:
        category = room.get("category") or "unknown"
        result[category] = result.get(category, 0) + 1
    return result


def _load_source(request: PipelineRequest, execution: PipelineExecution) -> Optional[BuildingModel]:
    execution.stage_trace.append("parse_source")
    if request.source_type is PipelineSourceType.IFC:
        assert request.ifc_path is not None
        if not os.path.isfile(request.ifc_path):
            raise FileNotFoundError(f"IFC file not found: {request.ifc_path}")
        schema_result, parsed_source = validate_ifc_schema_context(
            request.ifc_path, policy=request.schema_policy)
        execution.stage_trace.append("schema")
        execution.schema_result = schema_result
        execution.parsed_source = parsed_source
        execution.schema = schema_result.to_dict()
        if schema_result.blocking:
            execution.blocked_reason = "IFC schema validation failed"
            execution.skipped_stages.update({
                "quality": "blocked by IFC schema failure",
                "compliance": "blocked by IFC schema failure",
            })
            if not (request.generate_reports and request.out_dir):
                execution.skipped_stages["reporting"] = "blocked by IFC schema failure"
            return None
        assert parsed_source is not None
        model = ifc_to_building_model(request.ifc_path, parsed_model=parsed_source.model)
        execution.stage_trace.append("building_model")
        return model

    if request.source_type is PipelineSourceType.BUILDING_MODEL:
        assert request.building_model is not None
        from copy import deepcopy
        execution.stage_trace.append("building_model")
        return deepcopy(request.building_model)

    assert request.bim_data is not None
    # Phase 9 removed the flat building_params public input. Reject it loudly
    # at ingestion: keys the agents consume would otherwise bypass Manual
    # Inputs v1 validation, and keys nothing consumes would silently no-op.
    # Empty/marker-only blocks are tolerated for legacy raw payloads only.
    # Value-bearing enriched seams are output-only and must be resubmitted as
    # the original raw bim_data plus Manual Inputs v1, or reused as BuildingModel.
    reject_legacy_building_params(request.bim_data)
    execution.stage_trace.append("building_model")
    return building_model_from_bim_data(request.bim_data)


def run_validation_pipeline(request: PipelineRequest) -> PipelineExecution:
    """Execute the single authoritative pipeline in documented stage order."""
    execution = PipelineExecution(request=request)
    model = _load_source(request, execution)
    if model is None:
        if request.generate_reports and request.out_dir:
            from reporting.generator import generate_report_bundle
            schema_meta = dict((execution.schema or {}).get("metadata") or {})
            model_hint = {
                "name": os.path.basename(request.ifc_path or "") or None,
                "source_type": request.source_type.value,
                "source_path": request.ifc_path,
                "ifc_schema": schema_meta.get("schema"),
                "fingerprint": schema_meta.get("model_fingerprint"),
            }
            execution.reports = generate_report_bundle(
                None,
                dict(request.metadata),
                output_dir=request.out_dir,
                stages={"schema": execution.schema, "quality": None},
                model=model_hint,
                mode=request.mode.value,
                skipped_stages=execution.skipped_stages,
            )
            execution.stage_trace.append("reporting")
        return execution

    manual = parse_manual_inputs(request.manual_inputs)
    merge_result = merge_manual_inputs(model, manual)
    execution.stage_trace.append("manual_merge")
    model = merge_result.model
    execution.building_model = model
    execution.manual_input_metadata = merge_result.metadata

    bim_data = building_model_to_bim_data(model)
    if execution.schema is not None:
        bim_data["_schema"] = execution.schema
    bim_data["_manual_inputs"] = dict(execution.manual_input_metadata)
    execution.bim_data = bim_data

    # Category normalization and confidence review are model-preparation steps;
    # they now run for every source path, eliminating the former raw-data bypass.
    normalize_room_categories(bim_data)
    apply_review_prepass(bim_data, threshold=request.threshold)
    bim_data["_categories_seen"] = _categories_seen(bim_data)

    # Rehydrate the normalized/review-enriched legacy seam into the canonical
    # model before validation. Quality plugins never inspect raw dictionaries.
    quality_model = building_model_from_bim_data(
        bim_data,
        source_type=model.provenance.source_type,
        model_fingerprint=model.provenance.model_fingerprint,
        model_name=model.provenance.model_name,
        source_path=model.provenance.source_path,
        ifc_schema=model.provenance.ifc_schema,
    )
    execution.building_model = quality_model
    from validation.quality import QualityContext, run_model_quality_checks
    quality_context = QualityContext.from_model(
        quality_model,
        initial_findings=merge_result.findings,
        metadata={"pipeline_mode": request.mode.value},
    )
    quality_result = run_model_quality_checks(
        quality_model,
        context=quality_context,
    )
    execution.quality = quality_result.to_dict()
    bim_data["_quality"] = execution.quality
    execution.stage_trace.append("quality")

    if request.mode is PipelineMode.PRECHECK:
        execution.skipped_stages["compliance"] = (
            "precheck mode runs schema and quality only"
        )
        if request.generate_reports and request.out_dir:
            from reporting.generator import generate_report_bundle
            meta = dict(request.metadata)
            execution.reports = generate_report_bundle(
                None,
                meta,
                output_dir=request.out_dir,
                stages={"schema": execution.schema, "quality": execution.quality},
                model=execution.building_model,
                mode=request.mode.value,
                skipped_stages=execution.skipped_stages,
            )
            execution.stage_trace.append("reporting")
        else:
            execution.skipped_stages["reporting"] = (
                "report generation disabled for this precheck request"
            )
        return execution

    from validation.compliance.runner import _run_compliance_core
    result = _run_compliance_core(
        bim_data,
        request.clauses,
        retriever=request.retriever,
        llm=request.llm,
        use_langgraph=request.use_langgraph,
    )
    downgrade_flagged_findings(result, bim_data)
    execution.compliance = result
    execution.stage_trace.append("compliance")
    if request.llm is not None:
        execution.stage_trace.append("rag_advisory")

    from services.coverage import build_coverage
    execution.coverage = build_coverage(
        result,
        request.clauses,
        corpus_total=request.corpus_total,
    )
    bim_data["_coverage"] = execution.coverage

    if request.generate_reports and request.out_dir:
        from reporting.generator import generate_report_bundle
        meta = dict(request.metadata)
        execution.reports = generate_report_bundle(
            result.to_dict(),
            meta,
            output_dir=request.out_dir,
            coverage=execution.coverage,
            stages={"schema": execution.schema, "quality": execution.quality},
            model=execution.building_model,
            mode=request.mode.value,
            skipped_stages=execution.skipped_stages,
        )
        execution.stage_trace.append("reporting")

    return execution
