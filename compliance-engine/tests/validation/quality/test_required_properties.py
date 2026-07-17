from .phase4_helpers import findings, model, wall


def test_missing_catalog_required_property_is_prop_001():
    payload = model(walls=[wall(height=None)])
    match = next(row for row in findings(payload) if row["code"] == "QC-PROP-001")
    assert match["details"]["property"] == "height_mm"


def test_mapping_issue_is_prop_002():
    payload = model(walls=[wall(properties={
        "_mapping_issues": [{
            "property": "height_mm",
            "expected_pset": "Qto_WallBaseQuantities",
            "actual_pset": "WrongPset",
        }]
    })])
    assert "QC-PROP-002" in {row["code"] for row in findings(payload)}
