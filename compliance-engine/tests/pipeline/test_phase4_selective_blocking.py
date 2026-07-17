from services.validation_pipeline import PipelineRequest, run_validation_pipeline


def test_missing_space_area_yields_quality_issue_and_not_evaluated_rule():
    clause = {
        "article_id": "AREA-1",
        "rule_type": "numeric",
        "text_en": "Bedroom area must be at least 8 m2",
        "entities": {
            "object": "bedroom", "property": "area", "comparator": ">=",
            "value": 8, "unit": "m2", "condition": None,
        },
    }
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data={
            "units": {"length": "mm", "area": "m2"},
            "storeys": [{"id": "S1", "name": "Storey 1", "elevation_mm": 0}],
            "rooms": [{
                "id": "R1", "name": "Bedroom", "category": "room_bedroom",
                "category_raw": "bedroom", "category_source": "label",
                "category_confidence": 1.0, "area_m2": None,
                "polygon": [[0, 0], [3000, 0], [3000, 3000], [0, 3000], [0, 0]],
                "storey_id": "S1",
            }],
            "walls": [], "doors": [], "windows": [],
        },
        clauses=[clause],
        generate_reports=False,
    ))
    assert "QC-SPACE-004" in {
        row["code"] for row in execution.quality["findings"]
    }
    area_findings = [
        finding for finding in execution.compliance.findings
        if finding.article_id == "AREA-1"
    ]
    assert len(area_findings) == 1
    assert area_findings[0].verdict.value == "NOT_EVALUATED"


def test_missing_unit_contract_survives_pipeline_rehydration():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data={
            "rooms": [],
            "walls": [{
                "id": "W1", "start_point": [0, 0, 0],
                "end_point": [4000, 0, 0], "height": 3000,
                "thickness": 200,
            }],
            "doors": [], "windows": [],
        },
        mode="precheck",
    ))
    assert "QC-UNIT-001" in {
        row["code"] for row in execution.quality["findings"]
    }
