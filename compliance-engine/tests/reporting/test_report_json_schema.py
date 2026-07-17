from __future__ import annotations

import json

import pytest

from reporting.json_report import validate_report_dict, write_json_report
from reporting.report_model import build_validation_report


SCHEMA = {
    "stage": "schema",
    "status": "passed",
    "checker_version": "schema-test",
    "findings": [],
    "metadata": {"schema": "IFC4", "model_fingerprint": "abc"},
}
QUALITY = {
    "stage": "quality",
    "status": "passed_with_alerts",
    "checker_version": "quality-test",
    "findings": [{
        "category": "quality",
        "code": "QC-X",
        "verdict": "NOT_EVALUATED",
        "severity": "alert",
        "message": "missing property",
        "element_id": "internal-1",
        "element_ifc_guid": "2h$guid",
    }],
}
COMPLIANCE = {
    "stage": "compliance",
    "status": "completed",
    "checker_version": "compliance-test",
    "summary": {"PASS": 1, "FAIL": 0, "NEEDS_REVIEW": 0, "NOT_EVALUATED": 0},
    "findings": [{
        "category": "compliance",
        "article_id": "4-1",
        "verdict": "PASS",
        "severity": "info",
        "message": "ok",
        "element_id": "internal-2",
        "element_ifc_guid": "3k$guid",
    }],
}


def test_generated_report_validates_against_v1_schema(tmp_path):
    report = build_validation_report(
        compliance=COMPLIANCE,
        schema=SCHEMA,
        quality=QUALITY,
        model={
            "name": "sample.ifc",
            "source_type": "ifc",
            "ifc_schema": "IFC4",
            "project_guid": "project-guid",
            "fingerprint": "abc",
        },
        metadata={"plan_name": "sample.ifc"},
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    payload = report.to_dict()
    validate_report_dict(payload)
    path = write_json_report(report, tmp_path / "result.json")
    loaded = json.load(open(path, encoding="utf-8"))
    assert loaded["report_schema_version"] == "1.0"
    assert loaded["engine_version"] == "stage8-remediation-phase9-final-r2"
    assert len(loaded["findings"]) == 2


def test_schema_validation_rejects_missing_required_field():
    report = build_validation_report(
        compliance=COMPLIANCE,
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    ).to_dict()
    del report["overall"]
    with pytest.raises(ValueError, match="overall"):
        validate_report_dict(report)
