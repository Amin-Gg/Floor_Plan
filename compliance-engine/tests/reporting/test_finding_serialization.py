from domain.findings import Finding, FindingStage, Verdict
from domain.validation import ValidationResult
from reporting.report_model import StageReport


def test_stage_report_serializes_shared_contract():
    finding = Finding(article_id="QC-X", verdict=Verdict.NOT_EVALUATED,
                      message="missing", category="quality", code="QC-X")
    result = ValidationResult(stage=FindingStage.QUALITY, findings=[finding])
    data = StageReport(result).to_dict()
    assert data["stage"] == "quality"
    assert data["status"] == "passed_with_alerts"
    assert data["findings"][0]["code"] == "QC-X"
