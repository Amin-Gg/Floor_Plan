from __future__ import annotations

from validation.compliance.adapter import building_model_from_bim_data
from validation.quality import run_model_quality_checks

from .phase4_helpers import model, room, storey


def test_ifc_space_without_global_id_is_reported():
    payload = model(rooms=[room()])
    typed = building_model_from_bim_data(payload, source_type="ifc")
    result = run_model_quality_checks(typed)
    assert "QC-SPACE-002" in {finding.code for finding in result.findings}


def test_missing_space_name_is_reported():
    payload = model(rooms=[room(name=None)])
    assert "QC-SPACE-003" in {
        row["code"] for row in __import__("tests.helpers", fromlist=["run_quality_checks"]).run_quality_checks(payload)["findings"]
    }


def test_valid_storey_assignment_avoids_space_007():
    payload = model(rooms=[room(storey_id="S1")], storeys=[storey("S1")])
    stage = __import__("tests.helpers", fromlist=["run_quality_checks"]).run_quality_checks(payload)
    assert "QC-SPACE-007" not in {row["code"] for row in stage["findings"]}
