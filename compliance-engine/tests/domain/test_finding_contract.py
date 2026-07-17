from domain.findings import Finding, FindingSeverity, FindingStage, Verdict


def test_shared_finding_serializes_legacy_and_new_fields():
    finding = Finding(
        article_id="5-4-7-4",
        verdict=Verdict.FAIL,
        message="Door too narrow",
        object="door",
        element_id="D1",
        element_internal_id="internal-D1",
        element_ifc_guid="IFC-D1",
        model_fingerprint="f" * 64,
        measured=0.8,
        required=1.2,
        unit="m",
    )
    data = finding.to_dict()
    assert data["stage"] == "compliance"
    assert data["severity"] == "fail"
    assert data["element_internal_id"] == "internal-D1"
    assert data["element_ifc_guid"] == "IFC-D1"
    assert data["element_id"] == "D1"
    assert data["article_id"] == "5-4-7-4"
    assert data["expected"] == 1.2 and data["actual"] == 0.8


def test_schema_factory_uses_same_contract():
    f = Finding.schema(code="IFC-SCHEMA-007", severity="fail",
                       message="missing guid", entity="IfcDoor")
    assert isinstance(f, Finding)
    assert f.stage is FindingStage.SCHEMA
    assert f.severity is FindingSeverity.FAIL
    assert f.verdict is Verdict.FAIL
