from pathlib import Path

from scripts.run_validation_acceptance import run_acceptance


ROOT = Path(__file__).resolve().parents[2]


def test_final_acceptance_scenario(tmp_path):
    result = run_acceptance(
        ifc_path=ROOT / "tests" / "fixtures" / "sample_plan.ifc",
        manual_inputs_path=ROOT / "tests" / "fixtures" / "remediation_manual_inputs.json",
        clauses_path=ROOT / "data" / "mabhas_clauses.json",
        output_dir=tmp_path,
    )
    summary = result.summary
    assert summary["ok"] is True
    assert summary["schema_status"] == "passed"
    assert "QC-SPACE-004" in summary["quality_codes"]
    assert "QC-SPACE-006" in summary["quality_codes"]
    assert "QC-PLACE-007" in summary["quality_codes"]
    assert summary["window_values"]["Wb"]["width_mm"] == 1400.0
    assert summary["window_values"]["W-ACCEPT-02"]["width_mm"] == 900.0
    assert summary["compliance_summary"]["FAIL"] >= 1
    assert summary["compliance_summary"]["NOT_EVALUATED"] >= 1
    assert summary["bcf"]["topics"] >= 1
    assert (tmp_path / "compliance_result.json").exists()
    assert (tmp_path / "compliance_report.html").exists()
    assert (tmp_path / "compliance_report.pdf").exists()
    assert (tmp_path / "compliance_issues.bcf").exists()
