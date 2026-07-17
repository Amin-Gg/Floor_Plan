import pytest

from validation.schema import SchemaValidationPolicy, validate_ifc_schema

from .helpers import write_model


@pytest.mark.parametrize("schema", ["IFC4", "IFC4X1", "IFC4X3", "IFC4X3_ADD2"])
def test_default_policy_accepts_documented_ifc4_versions(tmp_path, schema):
    path = write_model(tmp_path / f"{schema}.ifc", schema=schema)
    result, _ = validate_ifc_schema(
        path,
        policy=SchemaValidationPolicy(strict_mandatory_attributes=False),
    )
    assert not any(f.code == "IFC-SCHEMA-002" for f in result.findings)


def test_default_policy_rejects_ifc4x2(tmp_path):
    path = write_model(tmp_path / "ifc4x2.ifc", schema="IFC4X2")
    result, _ = validate_ifc_schema(
        path,
        policy=SchemaValidationPolicy(strict_mandatory_attributes=False),
    )
    assert result.status == "failed"
    assert any(f.code == "IFC-SCHEMA-002" for f in result.findings)


def test_ifc2x3_requires_explicit_opt_in(tmp_path):
    path = write_model(tmp_path / "ifc2x3.ifc", schema="IFC2X3")
    strict_result, _ = validate_ifc_schema(
        path,
        policy=SchemaValidationPolicy(strict_mandatory_attributes=False),
    )
    assert any(f.code == "IFC-SCHEMA-002" for f in strict_result.findings)

    compatible_result, _ = validate_ifc_schema(
        path,
        policy=SchemaValidationPolicy(
            allow_ifc2x3=True,
            strict_mandatory_attributes=False,
        ),
    )
    assert not any(f.code == "IFC-SCHEMA-002" for f in compatible_result.findings)
