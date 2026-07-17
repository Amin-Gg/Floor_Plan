from reporting.report_model import build_validation_report


def test_element_finding_contains_internal_and_ifc_identity():
    report = build_validation_report(
        compliance={
            "summary": {"FAIL": 1},
            "findings": [{
                "article_id": "4-1",
                "verdict": "FAIL",
                "message": "too narrow",
                "element_internal_id": "internal-door-1",
                "element_ifc_guid": "2abc$ifcGuid",
                "element_type": "Door",
                "model_name": "sample.ifc",
            }],
        },
        model={"name": "sample.ifc", "source_type": "ifc", "fingerprint": "abc"},
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    ).to_dict()
    finding = report["findings"][0]
    assert finding["element_internal_id"] == "internal-door-1"
    assert finding["element_ifc_guid"] == "2abc$ifcGuid"
    assert finding["element_id"] == "internal-door-1"
