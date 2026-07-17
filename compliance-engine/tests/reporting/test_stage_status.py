import pytest

from domain.findings import Finding, FindingSeverity, FindingStage, Verdict
from domain.validation import ValidationResult, compute_stage_status


def test_schema_quality_status_is_centralized():
    alert = Finding(article_id="QC-X", verdict=Verdict.NOT_EVALUATED,
                    message="x", category="quality", code="QC-X")
    fail = Finding.schema(code="IFC-X", severity="fail", message="x")
    assert compute_stage_status(FindingStage.QUALITY, []) == "passed"
    assert compute_stage_status(FindingStage.QUALITY, [alert]) == "passed_with_alerts"
    assert compute_stage_status(FindingStage.SCHEMA, [fail]) == "failed"


def test_compliance_status_is_centralized():
    review = Finding(article_id="A", verdict=Verdict.NEEDS_REVIEW, message="x")
    assert compute_stage_status(FindingStage.COMPLIANCE, []) == "completed"
    assert compute_stage_status(FindingStage.COMPLIANCE, [review]) == "completed_with_review"


def test_invalid_status_is_rejected():
    with pytest.raises(ValueError):
        ValidationResult(stage="quality", status="completed")
