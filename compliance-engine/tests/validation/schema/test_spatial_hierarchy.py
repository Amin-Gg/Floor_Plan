from __future__ import annotations

from validation.schema import validate_ifc_schema

from .helpers import write_model


def test_disconnected_spatial_entities_fail_hierarchy_check(tmp_path):
    path = write_model(tmp_path / "disconnected.ifc", connected=False)
    result, _ = validate_ifc_schema(path)
    hierarchy = [finding for finding in result.findings if finding.code == "IFC-SCHEMA-008"]
    assert result.status == "failed"
    assert len(hierarchy) == 3
    assert {finding.element_type for finding in hierarchy} == {
        "IfcSite", "IfcBuilding", "IfcBuildingStorey"
    }


def test_connected_spatial_chain_passes_hierarchy_check(tmp_path):
    path = write_model(tmp_path / "connected.ifc", connected=True)
    result, _ = validate_ifc_schema(path)
    assert result.status == "passed_with_alerts"  # no products => 009 only
    assert not any(finding.code == "IFC-SCHEMA-008" for finding in result.findings)


def test_wrong_parent_type_is_reported(tmp_path):
    import ifcopenshell
    import ifcopenshell.guid as guid

    path = tmp_path / "wrong-parent.ifc"
    model = ifcopenshell.file(schema="IFC4")
    project = model.create_entity("IfcProject", GlobalId=guid.new(), Name="P")
    site = model.create_entity("IfcSite", GlobalId=guid.new(), Name="S")
    building = model.create_entity("IfcBuilding", GlobalId=guid.new(), Name="B")
    storey = model.create_entity("IfcBuildingStorey", GlobalId=guid.new(), Name="L1")
    # Deliberately skip Site -> Building and aggregate Building directly under Project.
    for parent, child in ((project, site), (project, building), (building, storey)):
        model.create_entity("IfcRelAggregates", GlobalId=guid.new(),
                            RelatingObject=parent, RelatedObjects=[child])
    model.write(str(path))

    result, _ = validate_ifc_schema(str(path))
    finding = next(f for f in result.findings
                   if f.code == "IFC-SCHEMA-008" and f.element_type == "IfcBuilding")
    assert finding.severity.value == "fail"
    assert finding.details["expected_parent_type"] == "IfcSite"
    assert finding.details["actual_parent_types"] == ["IfcProject"]


def test_schema_failure_blocks_precheck_and_full_check_equally(tmp_path):
    from services.validation_pipeline import PipelineRequest, run_validation_pipeline

    path = write_model(tmp_path / "disconnected-modes.ifc", connected=False)
    for mode in ("precheck", "full_check"):
        execution = run_validation_pipeline(PipelineRequest(
            source_type="ifc",
            ifc_path=path,
            mode=mode,
            generate_reports=False,
        ))
        assert execution.blocked is True
        assert execution.schema["status"] == "failed"
        assert any(row["code"] == "IFC-SCHEMA-008"
                   for row in execution.schema["findings"])
        assert execution.quality is None
        assert execution.compliance is None
