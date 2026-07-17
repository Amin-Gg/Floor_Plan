"""Regression tests for the removed flat ``building_params`` input.

The final independent review (post Phase 9) found the removed path
half-alive: a flat block embedded in input ``bim_data`` bypassed Manual
Inputs v1 validation for keys the agents consume (e.g. ``ceiling_height_mm``
flipped a ceiling-height clause from NOT_EVALUATED to PASS) and silently
no-opped for keys nothing consumes. These tests pin the fix: loud rejection at every public ingestion boundary.
Empty/marker-only legacy blocks remain harmless compatibility inputs, while a
value-bearing enriched seam is explicitly output-only and rejected.
"""
from __future__ import annotations

import pytest

from manual_inputs import (
    LEGACY_BUILDING_PARAMS_MESSAGE,
    ManualInputsError,
    reject_legacy_building_params,
)
from services.validation_pipeline import PipelineRequest, run_validation_pipeline

ROOMS = [{"id": "R1", "category": "bedroom", "area_m2": 10,
          "polygon": [[0, 0], [3, 0], [3, 4], [0, 4]]}]
CLAUSES = [{
    "article_id": "H1", "rule_type": "numeric",
    "text_en": "min ceiling height 2.6m",
    "entities": {"object": "room", "property": "ceiling_height",
                 "comparator": ">=", "value": 2.6, "unit": "m"},
}]


def _request(bim_data):
    return PipelineRequest(source_type="bim_data", bim_data=bim_data,
                           clauses=CLAUSES, generate_reports=False)


def test_guard_rejects_operator_keys_with_migration_message():
    with pytest.raises(ManualInputsError) as exc:
        reject_legacy_building_params(
            {"rooms": ROOMS, "building_params": {"ceiling_height_mm": 2900}})
    assert LEGACY_BUILDING_PARAMS_MESSAGE in str(exc.value)
    assert "ceiling_height_mm" in str(exc.value)


@pytest.mark.parametrize("block", [None, {}, {"_provided": []},
                                   {"_provided": ["wall_height"]}])
def test_guard_accepts_absent_or_internal_marker_only(block):
    bim = {"rooms": ROOMS}
    if block is not None:
        bim["building_params"] = block
    reject_legacy_building_params(bim)  # must not raise


def test_guard_rejects_non_mapping_block():
    with pytest.raises(ManualInputsError):
        reject_legacy_building_params({"rooms": ROOMS, "building_params": 3200})


def test_pipeline_rejects_flat_params_instead_of_half_alive_bypass():
    """The exact bypass found in review: flat ceiling_height_mm previously
    flipped H1 from NOT_EVALUATED to PASS without any v1 validation."""
    with pytest.raises(ManualInputsError) as exc:
        run_validation_pipeline(_request(
            dict(rooms=ROOMS, building_params={"ceiling_height_mm": 2900})))
    assert LEGACY_BUILDING_PARAMS_MESSAGE in str(exc.value)


def test_pipeline_accepts_harmless_legacy_marker_only_block():
    execution = run_validation_pipeline(_request(
        dict(rooms=ROOMS, building_params={"_provided": []})))
    assert execution.compliance is not None
    verdicts = {f.article_id: f.verdict.value for f in execution.compliance.findings}
    assert verdicts["H1"] == "NOT_EVALUATED"  # no unvalidated value applied


def test_v1_manual_inputs_remain_the_supported_path():
    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data", bim_data={"rooms": ROOMS}, clauses=CLAUSES,
        generate_reports=False,
        manual_inputs={"schema_version": "1.0",
                       "defaults": {"ceiling_height_mm": 2900}},
    ))
    verdicts = {f.article_id: f.verdict.value for f in execution.compliance.findings}
    assert verdicts["H1"] == "PASS"


def test_analyze_endpoint_returns_400_with_migration_message(monkeypatch):
    from fastapi.testclient import TestClient
    from api import main as api_main

    client = TestClient(api_main.app)
    response = client.post("/analyze", json={
        "bim_data": {"rooms": ROOMS,
                     "building_params": {"ceiling_height_mm": 2900}},
    })
    assert response.status_code == 400
    assert LEGACY_BUILDING_PARAMS_MESSAGE in response.json()["detail"]


def test_analyze_endpoint_accepts_marker_only_block(monkeypatch):
    from fastapi.testclient import TestClient
    from api import main as api_main

    monkeypatch.setattr(api_main, "submit_job",
                        lambda bim_data, meta, manual_inputs=None: "0123456789ab")
    client = TestClient(api_main.app)
    response = client.post("/analyze", json={
        "bim_data": {"rooms": ROOMS, "building_params": {"_provided": []}},
    })
    assert response.status_code == 200


def test_value_bearing_enriched_seam_is_output_only_and_rejected():
    enriched = {
        "rooms": ROOMS,
        "building_params": {
            "ceiling_height_mm": 2900.0,
            "_provided": ["ceiling_height_mm"],
        },
    }
    with pytest.raises(ManualInputsError) as exc:
        run_validation_pipeline(_request(enriched))
    assert "ceiling_height_mm" in str(exc.value)


def test_removed_services_orchestrator_public_entrypoint_is_absent():
    """The old direct verdict-driving entry point must not survive as a module."""
    import importlib.util
    import inspect

    assert importlib.util.find_spec("services.orchestrator") is None
    from validation.compliance import runner

    signature = inspect.signature(runner._run_compliance_core)
    assert "building_params" not in signature.parameters
    assert not hasattr(runner, "run_compliance")
