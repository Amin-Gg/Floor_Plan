from __future__ import annotations

import json

from services.validation_pipeline import PipelineRequest, run_validation_pipeline


def _minimal_bim():
    return {
        "rooms": [], "walls": [], "doors": [], "windows": [],
        "stairs": [], "slabs": [], "building_params": {"_provided": []},
    }


def test_precheck_writes_v1_report_with_compliance_skip_reason(tmp_path):
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data=_minimal_bim(),
        mode="precheck",
        out_dir=str(tmp_path),
        generate_reports=True,
    ))
    report = json.load(open(execution.reports["json"], encoding="utf-8"))
    assert report["report_schema_version"] == "1.0"
    assert report["overall"]["code"].startswith("precheck_")
    assert report["stages"]["compliance"]["skipped"] is True
    assert "precheck" in report["stages"]["compliance"]["skip_reason"]
    assert execution.stage_trace[-1] == "reporting"


def test_schema_failure_still_writes_rejection_report(tmp_path):
    bad = tmp_path / "bad.ifc"
    bad.write_text("not IFC", encoding="utf-8")
    out = tmp_path / "out"
    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc",
        ifc_path=str(bad),
        mode="full_check",
        out_dir=str(out),
        generate_reports=True,
    ))
    report = json.load(open(execution.reports["json"], encoding="utf-8"))
    assert report["overall"]["code"] == "rejected"
    assert report["stages"]["quality"]["skipped"] is True
    assert report["stages"]["compliance"]["skipped"] is True
    assert report["model"]["name"] == "bad.ifc"
    assert str(tmp_path) not in json.dumps(report)
