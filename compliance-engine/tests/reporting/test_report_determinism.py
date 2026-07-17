from __future__ import annotations

from copy import deepcopy

from reporting.report_model import build_validation_report


def _compliance(reverse: bool = False):
    findings = [
        {"article_id": "B", "verdict": "PASS", "message": "b", "element_id": "2"},
        {"article_id": "A", "verdict": "FAIL", "message": "a", "element_id": "1"},
    ]
    if reverse:
        findings.reverse()
    return {
        "stage": "compliance",
        "status": "completed",
        "summary": {"PASS": 1, "FAIL": 1, "NEEDS_REVIEW": 0, "NOT_EVALUATED": 0},
        "findings": findings,
    }


def test_fixed_context_and_shuffled_findings_produce_identical_report():
    kwargs = {
        "metadata": {"z": 1, "a": 2},
        "generated_at": "2026-07-10T12:00:00Z",
        "run_id": "11111111-1111-4111-8111-111111111111",
    }
    a = build_validation_report(compliance=_compliance(False), **kwargs).to_dict()
    b = build_validation_report(compliance=_compliance(True), **kwargs).to_dict()
    assert a == b
    assert [x["verdict"] for x in a["findings"]] == ["FAIL", "PASS"]


def test_stable_finding_ids_are_independent_of_input_order():
    a = build_validation_report(
        compliance=_compliance(False), generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    b = build_validation_report(
        compliance=_compliance(True), generated_at="2026-07-10T12:00:00Z",
        run_id="22222222-2222-4222-8222-222222222222",
    )
    assert [x["finding_id"] for x in a.findings] == [x["finding_id"] for x in b.findings]
