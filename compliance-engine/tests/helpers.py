"""Test-only adapters for exercising canonical APIs from compact fixtures."""
from __future__ import annotations

from typing import Any, Optional, Sequence

from domain.findings import Finding
from services.validation_pipeline import PipelineRequest, run_validation_pipeline
from validation.compliance.adapter import building_model_from_bim_data
from validation.quality import QualityCheck, QualityContext, run_model_quality_checks


def run_quality_checks(
    bim_data: dict[str, Any],
    additional_findings: Optional[list[Finding]] = None,
    *,
    checks: Optional[Sequence[QualityCheck]] = None,
) -> dict[str, Any]:
    model = building_model_from_bim_data(bim_data)
    context = QualityContext.from_model(
        model,
        initial_findings=additional_findings or (),
        metadata={"test_adapter": True},
    )
    return run_model_quality_checks(model, context=context, checks=checks).to_dict()


def run_ifc_compliance(ifc_path: str, clauses: list[dict[str, Any]], **kwargs):
    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc",
        ifc_path=ifc_path,
        clauses=clauses,
        threshold=kwargs.get("threshold"),
        corpus_total=kwargs.get("corpus_total"),
        retriever=kwargs.get("retriever"),
        llm=kwargs.get("llm"),
        use_langgraph=kwargs.get("use_langgraph", False),
        manual_inputs=kwargs.get("manual_inputs"),
        generate_reports=False,
    ))
    if execution.blocked:
        from validation.schema import IfcSchemaError
        assert execution.schema_result is not None
        raise IfcSchemaError(execution.schema_result)
    return execution.compliance, execution.bim_data
