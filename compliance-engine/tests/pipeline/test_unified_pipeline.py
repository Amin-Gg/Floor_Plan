from pathlib import Path

from services.validation_pipeline import PipelineRequest, run_validation_pipeline
from validation.compliance.adapter import building_model_from_bim_data


def _rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _bim(category="room_bedroom"):
    return {
        "rooms": [{
            "id": "R1", "category": category, "area_m2": 9.0,
            "polygon": _rect(0, 0, 3000, 3000),
            "dimensions": {"width_mm": 3000, "length_mm": 3000},
        }],
        "walls": [], "doors": [], "windows": [], "stairs": [], "slabs": [],
        "building_params": {"_provided": []},
    }


def _area_clause():
    return {
        "article_id": "A1", "rule_type": "numeric", "text_en": "minimum area",
        "entities": {"object": "bedroom", "property": "area", "comparator": ">=",
                     "value": 6, "unit": "m2", "condition": None},
    }


def test_raw_input_cannot_bypass_quality():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data=_bim("not-a-known-room"),
        clauses=[],
        generate_reports=False,
    ))
    codes = {f["code"] for f in execution.quality["findings"]}
    assert "QC-SPACE-TAG-001" in codes


def test_manual_inputs_merge_before_quality_removes_false_param_alert():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data=_bim(),
        manual_inputs={
            "schema_version": "1.0",
            "defaults": {"wall_height_mm": 3000},
        },
        clauses=[],
        generate_reports=False,
    ))
    codes = {f["code"] for f in execution.quality["findings"]}
    assert "QC-PARAM-001" not in codes
    assert execution.bim_data["building_params"]["wall_height"] == 3000


def test_bim_data_and_building_model_entrypoints_are_equivalent():
    legacy = _bim()
    model = building_model_from_bim_data(legacy)
    a = run_validation_pipeline(PipelineRequest(
        source_type="bim_data", bim_data=legacy,
        clauses=[_area_clause()], generate_reports=False,
    ))
    b = run_validation_pipeline(PipelineRequest(
        source_type="building_model", building_model=model,
        clauses=[_area_clause()], generate_reports=False,
    ))
    assert a.compliance.summary == b.compliance.summary
    assert [f.verdict for f in a.compliance.findings] == [f.verdict for f in b.compliance.findings]
    assert {f["code"] for f in a.quality["findings"]} == {
        f["code"] for f in b.quality["findings"]
    }


def test_precheck_runs_quality_but_skips_compliance():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data", bim_data=_bim(),
        mode="precheck", clauses=[_area_clause()], generate_reports=False,
    ))
    assert execution.quality is not None
    assert execution.compliance is None
    assert "compliance" in execution.skipped_stages


def test_schema_failure_blocks_downstream_and_records_reasons(tmp_path):
    path = tmp_path / "bad.ifc"
    path.write_text("not IFC", encoding="utf-8")
    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc", ifc_path=str(path), clauses=[], generate_reports=False,
    ))
    assert execution.blocked
    assert execution.schema["status"] == "failed"
    assert execution.quality is None and execution.compliance is None
    assert set(execution.skipped_stages) >= {"quality", "compliance", "reporting"}


def test_ifc_invalid_parse_emits_no_unraisable_warning(tmp_path):
    # Pytest's unraisable-exception plugin will fail this test under -W error
    # if the ifcopenshell destructor defect is reintroduced.
    path = tmp_path / "bad.ifc"
    path.write_text("STEP; not a valid file", encoding="utf-8")
    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc", ifc_path=str(path), clauses=[], generate_reports=False,
    ))
    assert execution.blocked


def test_stage_trace_proves_manual_merge_precedes_quality_and_compliance():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data=_bim(),
        manual_inputs={"schema_version": "1.0", "defaults": {"wall_height_mm": 3000}},
        clauses=[_area_clause()],
        generate_reports=False,
    ))
    assert execution.stage_trace == [
        "parse_source", "building_model", "manual_merge", "quality", "compliance"
    ]
