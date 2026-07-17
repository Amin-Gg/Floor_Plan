from validation.schema import validate_ifc_schema

from .helpers import write_model


def test_duplicate_global_id_is_blocking(tmp_path):
    path = write_model(tmp_path / "duplicate.ifc", duplicate_guid=True)
    result, _ = validate_ifc_schema(path)
    duplicates = [finding for finding in result.findings if finding.code == "IFC-SCHEMA-010"]
    assert result.status == "failed"
    assert len(duplicates) == 1
    assert len(duplicates[0].details["entities"]) == 2
