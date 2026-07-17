from reporting.report_model import build_validation_report


def test_skipped_stage_is_materialized_with_reason():
    report = build_validation_report(
        compliance=None,
        schema={"stage": "schema", "status": "failed", "findings": []},
        quality=None,
        model={"source_type": "ifc", "name": "bad.ifc"},
        skipped_stages={
            "quality": "blocked by IFC schema failure",
            "compliance": "blocked by IFC schema failure",
        },
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    ).to_dict()
    assert report["stages"]["quality"]["skipped"] is True
    assert report["stages"]["quality"]["skip_reason"] == "blocked by IFC schema failure"
    assert report["stages"]["compliance"]["skipped"] is True
    assert report["overall"]["code"] == "rejected"
