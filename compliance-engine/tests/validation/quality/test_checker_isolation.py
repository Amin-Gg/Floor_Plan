from __future__ import annotations

from domain.findings import Finding, FindingSeverity, FindingStage, Verdict
from domain.model import BuildingModel, ModelProvenance
from validation.quality import QualityContext, run_model_quality_checks
from validation.quality.findings import quality_finding


def _model() -> BuildingModel:
    return BuildingModel(
        provenance=ModelProvenance(
            source_type="test",
            model_fingerprint="phase3-quality-test",
            model_name="test-model",
        )
    )


class ExplodingCheck:
    code_prefix = "QC-BOOM"
    codes = ("QC-BOOM-001",)
    name = "exploding"
    blocking = False

    def applies_to(self, model, context):
        return True

    def run(self, model, context):
        raise RuntimeError("controlled test failure")


class FollowingCheck:
    code_prefix = "QC-AFTER"
    codes = ("QC-AFTER-001",)
    name = "following"
    blocking = False

    def applies_to(self, model, context):
        return True

    def run(self, model, context):
        return [quality_finding(
            model,
            "QC-AFTER-001",
            "following check still executed",
            source="test",
        )]


def test_failed_plugin_does_not_suppress_later_plugins(caplog):
    model = _model()
    result = run_model_quality_checks(
        model,
        context=QualityContext.from_model(model),
        checks=[ExplodingCheck(), FollowingCheck()],
    )
    assert [finding.code for finding in result.findings] == [
        "QC-INTERNAL-001",
        "QC-AFTER-001",
    ]
    assert result.metadata["failed_checks"] == ["exploding"]
    assert result.metadata["executed_checks"] == ["following"]
    assert "controlled test failure" in caplog.text


def test_nonblocking_plugin_error_is_an_alert():
    model = _model()
    result = run_model_quality_checks(model, checks=[ExplodingCheck()])
    finding = result.findings[0]
    assert finding.stage is FindingStage.QUALITY
    assert finding.severity is FindingSeverity.ALERT
    assert result.status == "passed_with_alerts"


def test_blocking_plugin_error_fails_quality_stage():
    class BlockingExploder(ExplodingCheck):
        name = "blocking_exploder"
        code_prefix = "QC-BLOCK"
        codes = ("QC-BLOCK-001",)
        blocking = True

    model = _model()
    result = run_model_quality_checks(model, checks=[BlockingExploder()])
    assert result.findings[0].severity is FindingSeverity.FAIL
    assert result.status == "failed"


def test_initial_findings_are_preserved_before_plugin_findings():
    model = _model()
    seed = Finding(
        article_id="QC-SEED-001",
        verdict=Verdict.NOT_EVALUATED,
        message="seed",
        category=FindingStage.QUALITY.value,
        code="QC-SEED-001",
    )
    context = QualityContext.from_model(model, initial_findings=[seed])
    result = run_model_quality_checks(
        model,
        context=context,
        checks=[FollowingCheck()],
    )
    assert [finding.code for finding in result.findings] == [
        "QC-SEED-001",
        "QC-AFTER-001",
    ]


def test_context_nested_review_data_is_request_local_and_immutable():
    import pytest

    model = _model()
    source = {"threshold": 0.5, "flagged": [{"id": "A"}]}
    model.extras["_review_summary"] = source
    context = QualityContext.from_model(model)
    source["flagged"][0]["id"] = "MUTATED"
    assert context.review_summary["flagged"][0]["id"] == "A"
    with pytest.raises(TypeError):
        context.review_summary["flagged"][0]["id"] = "NOPE"
