import json

from fastapi.testclient import TestClient

from api import main as api_main


client = TestClient(api_main.app)


def test_analyze_accepts_v1_manual_inputs_and_threads_to_job(monkeypatch):
    captured = {}

    def fake_submit(bim_data, meta, manual_inputs=None):
        captured["manual_inputs"] = manual_inputs
        return "0123456789ab"

    monkeypatch.setattr(api_main, "submit_job", fake_submit)
    response = client.post("/analyze", json={
        "bim_data": {"rooms": [], "walls": [], "doors": [], "windows": []},
        "manual_inputs": {
            "schema_version": "1.0",
            "defaults": {"wall_height_mm": 3000},
            "element_overrides": {"windows": {"W-01": {"width_mm": 1200}}},
        },
    })
    assert response.status_code == 200, response.text
    assert captured["manual_inputs"]["defaults"]["wall_height_mm"] == 3000
    assert captured["manual_inputs"]["element_overrides"]["windows"]["W-01"]["width_mm"] == 1200


def test_analyze_rejects_invalid_manual_inputs(monkeypatch):
    response = client.post("/analyze", json={
        "bim_data": {"rooms": []},
        "manual_inputs": {"schema_version": "1.0", "defaults": {"wall_height_mm": True}},
    })
    assert response.status_code == 400
    assert "finite number" in response.json()["detail"]


def test_analyze_ifc_accepts_manual_inputs_form(monkeypatch):
    captured = {}

    def fake_submit(ifc_path, meta, manual_inputs=None):
        captured["manual_inputs"] = manual_inputs
        return "0123456789ab"

    monkeypatch.setattr(api_main, "submit_ifc_job", fake_submit)
    response = client.post(
        "/analyze-ifc",
        files={"file": ("plan.ifc", b"ISO-10303-21; dummy", "application/octet-stream")},
        data={"manual_inputs": json.dumps({
            "schema_version": "1.0",
            "project": {"default_storey_height_mm": 3200},
        })},
    )
    assert response.status_code == 200, response.text
    assert captured["manual_inputs"]["project"]["default_storey_height_mm"] == 3200


def test_analyze_ifc_rejects_removed_building_params_field():
    response = client.post(
        "/analyze-ifc",
        files={"file": ("plan.ifc", b"ISO-10303-21; dummy", "application/octet-stream")},
        data={
            "building_params": json.dumps({"wall_height": 3000}),
            "manual_inputs": json.dumps({"schema_version": "1.0"}),
        },
    )
    assert response.status_code == 400
    assert "removed in Phase 9" in response.json()["detail"]
