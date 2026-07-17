from __future__ import annotations

from domain.model import BuildingModel, ModelProvenance
from validation.quality import run_model_quality_checks
from validation.quality.findings import quality_finding


def _check(index: int):
    class OrderedCheck:
        code_prefix = f"QC-ORDER-{index}"
        codes = (f"QC-ORDER-{index}-001",)
        name = f"order_{index}"
        blocking = False

        def applies_to(self, model, context):
            return True

        def run(self, model, context):
            return [quality_finding(model, self.codes[0], self.name)]

    return OrderedCheck()


def test_registry_order_controls_deterministic_finding_order():
    model = BuildingModel(ModelProvenance("test", "ordered-model"))
    checks = [_check(3), _check(1), _check(2)]
    first = run_model_quality_checks(model, checks=checks)
    second = run_model_quality_checks(model, checks=checks)
    expected = ["QC-ORDER-3-001", "QC-ORDER-1-001", "QC-ORDER-2-001"]
    assert [finding.code for finding in first.findings] == expected
    assert [finding.code for finding in second.findings] == expected
    assert [finding.finding_id for finding in first.findings] == [
        finding.finding_id for finding in second.findings
    ]
    assert first.metadata["executed_checks"] == ["order_3", "order_1", "order_2"]
