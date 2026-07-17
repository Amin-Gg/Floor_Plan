from __future__ import annotations

from domain.model import BuildingModel, ModelProvenance
from validation.quality import run_model_quality_checks


def _model() -> BuildingModel:
    return BuildingModel(ModelProvenance("test", "policy-model"))


class AppliesFailure:
    code_prefix = "QC-APPLIES"
    codes = ("QC-APPLIES-001",)
    name = "applies_failure"
    blocking = False

    def applies_to(self, model, context):
        raise ValueError("applies failed")

    def run(self, model, context):
        raise AssertionError("must not run")


class InvalidOutput:
    code_prefix = "QC-INVALID"
    codes = ("QC-INVALID-001",)
    name = "invalid_output"
    blocking = False

    def applies_to(self, model, context):
        return True

    def run(self, model, context):
        return None


class WrongCode:
    code_prefix = "QC-WRONG"
    codes = ("QC-WRONG-001",)
    name = "wrong_code"
    blocking = False

    def applies_to(self, model, context):
        return True

    def run(self, model, context):
        from validation.quality.findings import quality_finding
        return [quality_finding(model, "QC-UNDECLARED-001", "bad")]


def test_applies_to_exception_becomes_visible_internal_finding():
    result = run_model_quality_checks(_model(), checks=[AppliesFailure()])
    finding = result.findings[0]
    assert finding.code == "QC-INTERNAL-001"
    assert finding.details["phase"] == "applies_to"
    assert result.metadata["failed_checks"] == ["applies_failure"]


def test_invalid_plugin_return_is_not_silently_ignored():
    result = run_model_quality_checks(_model(), checks=[InvalidOutput()])
    assert result.findings[0].code == "QC-INTERNAL-001"
    assert result.findings[0].details["exception_type"] == "TypeError"


def test_undeclared_finding_code_is_reported_as_internal_error():
    result = run_model_quality_checks(_model(), checks=[WrongCode()])
    assert result.findings[0].code == "QC-INTERNAL-001"
    assert result.findings[0].details["exception_type"] == "ValueError"
