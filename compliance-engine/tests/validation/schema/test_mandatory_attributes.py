from validation.schema import SchemaValidationPolicy, validate_ifc_schema

from .helpers import write_model


def test_schema_metadata_detects_empty_required_aggregate(tmp_path):
    path = write_model(tmp_path / "missing-required.ifc", empty_polyline=True)
    result, _ = validate_ifc_schema(path)
    finding = next(f for f in result.findings if f.code == "IFC-SCHEMA-011")
    assert result.status == "failed"
    assert finding.element_type == "IfcPolyline"
    assert finding.details["missing_attributes"] == ["Points"]


def test_mandatory_attribute_check_can_be_disabled_by_policy(tmp_path):
    path = write_model(tmp_path / "relaxed.ifc", empty_polyline=True)
    policy = SchemaValidationPolicy(strict_mandatory_attributes=False)
    result, _ = validate_ifc_schema(path, policy=policy)
    assert result.status == "passed_with_alerts"
    assert not any(f.code == "IFC-SCHEMA-011" for f in result.findings)
