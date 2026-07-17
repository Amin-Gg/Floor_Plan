from __future__ import annotations

import pytest

from reporting.report_model import OverallCode, compute_overall_status


def _stage(stage, status, findings=()):
    return {"stage": stage, "status": status, "findings": list(findings)}


def _finding(verdict, severity=None):
    return {"verdict": verdict, "severity": severity or ("fail" if verdict == "FAIL" else "alert")}


@pytest.mark.parametrize(
    "stages,expected",
    [
        ({"schema": _stage("schema", "failed"), "quality": None, "compliance": None}, OverallCode.REJECTED),
        ({"schema": _stage("schema", "passed"), "quality": _stage("quality", "passed"), "compliance": _stage("compliance", "completed", [_finding("FAIL")])}, OverallCode.NON_COMPLIANT),
        ({"schema": None, "quality": _stage("quality", "passed"), "compliance": _stage("compliance", "completed_with_review", [_finding("NOT_EVALUATED")])}, OverallCode.INCOMPLETE),
        ({"schema": None, "quality": _stage("quality", "passed"), "compliance": _stage("compliance", "completed_with_review", [_finding("NEEDS_REVIEW")])}, OverallCode.NEEDS_REVIEW),
        ({"schema": None, "quality": _stage("quality", "passed_with_alerts", [_finding("NOT_EVALUATED")]), "compliance": _stage("compliance", "completed", [_finding("PASS", "info")])}, OverallCode.COMPLIANT_WITH_QUALITY_ALERTS),
        ({"schema": None, "quality": _stage("quality", "passed"), "compliance": _stage("compliance", "completed", [_finding("PASS", "info")])}, OverallCode.COMPLIANT),
    ],
)
def test_full_check_policy(stages, expected):
    assert compute_overall_status(mode="full_check", stages=stages).code is expected


def test_precheck_never_claims_regulatory_compliance():
    status = compute_overall_status(
        mode="precheck",
        stages={"schema": None, "quality": _stage("quality", "passed"), "compliance": None},
        skipped_stages={"compliance": "precheck"},
    )
    assert status.code is OverallCode.PRECHECK_READY
    assert "compliant" not in status.label
