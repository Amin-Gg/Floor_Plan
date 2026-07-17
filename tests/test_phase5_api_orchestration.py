from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("FLOORPLAN_SKIP_MODEL_INIT", "1")

from application import create_app
from config.settings import TestingConfig
from routes import compliance_routes
from services.compliance_client import (
    ComplianceClient,
    ComplianceClientConfig,
    ComplianceProtocolError,
    ComplianceTimeout,
)


@pytest.fixture()
def client():
    app = create_app(TestingConfig)
    app.config.update(TESTING=True)
    return app.test_client()


def _response(status: int, payload: dict, *, content_type: str = "application/json"):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = content_type
    response.url = "http://engine.test/resource"
    return response


def test_openapi_registers_real_phase5_contracts(client):
    response = client.get("/openapi/openapi.json")
    assert response.status_code == 200
    spec = response.get_json()
    required = {
        "/analyze",
        "/export/ifc",
        "/export/ifc/upload",
        "/compliance/health",
        "/compliance/jobs/from-analysis",
        "/compliance/jobs/ifc",
        "/compliance/jobs/{job_id}",
        "/compliance/jobs/{job_id}/wait",
        "/compliance/jobs/{job_id}/report/{kind}",
    }
    assert required <= set(spec["paths"])
    analyze_content = spec["paths"]["/analyze"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in analyze_content
    export_content = spec["paths"]["/export/ifc"]["post"]["requestBody"]["content"]
    assert "application/json" in export_content
    upload_content = spec["paths"]["/export/ifc/upload"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in upload_content
    assert "ComplianceJobResponse" in spec["components"]["schemas"]


def test_invalid_payload_is_rejected_before_model_inference(client):
    response = client.post("/analyze", data={})
    assert response.status_code == 422
    body = response.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "schema_validation_failed"
    assert body["error"]["status"] == 422
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert any(v["loc"] == ["image"] for v in body["error"]["details"]["violations"])


def test_export_json_schema_rejects_unknown_fields(client):
    response = client.post(
        "/export/ifc",
        json={"analysis_file": "final1.json", "unknown": True},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "schema_validation_failed"


def test_direct_ifc_orchestration_deletes_temp_file_and_returns_urls(client, monkeypatch):
    captured = {}

    class FakeClient:
        def submit_ifc(self, path, **kwargs):
            path = Path(path)
            assert path.is_file()
            captured["path"] = path
            captured["kwargs"] = kwargs
            return {
                "job_id": "0123456789ab",
                "status": "queued",
                "correlation_id": kwargs["correlation_id"],
            }

    monkeypatch.setattr(compliance_routes, "_client", lambda: FakeClient())
    response = client.post(
        "/compliance/jobs/ifc",
        data={
            "ifc_file": (io.BytesIO(b"ISO-10303-21;\nEND-ISO-10303-21;"), "plan.ifc"),
            "plan_name": "Unit A",
        },
        content_type="multipart/form-data",
        headers={"X-Correlation-ID": "phase5-route-test"},
    )
    assert response.status_code == 202
    body = response.get_json()
    assert body["job_id"] == "0123456789ab"
    assert body["request_id"] == "phase5-route-test"
    assert body["correlation_id"] == "phase5-route-test"
    assert response.headers["X-Correlation-ID"] == "phase5-route-test"
    assert body["status_url"].endswith("/compliance/jobs/0123456789ab")
    assert set(body["reports"]) == {"json", "html", "pdf", "bcf"}
    assert captured["kwargs"]["plan_name"] == "Unit A"
    assert not captured["path"].exists()


def test_from_analysis_exports_then_submits_without_second_geometry_override(client, monkeypatch, tmp_path):
    ifc_path = tmp_path / "generated.ifc"
    ifc_path.write_bytes(b"ISO-10303-21;")
    captured = {}

    monkeypatch.setattr(compliance_routes, "load_analysis_bim", lambda _: {"walls": [{}]})
    monkeypatch.setattr(
        compliance_routes,
        "create_ifc_artifact",
        lambda *a, **k: SimpleNamespace(path=ifc_path),
    )

    class FakeClient:
        def submit_ifc(self, path, **kwargs):
            captured.update(kwargs)
            return {
                "job_id": "abcdef123456",
                "status": "queued",
                "correlation_id": kwargs["correlation_id"],
            }

    monkeypatch.setattr(compliance_routes, "_client", lambda: FakeClient())
    response = client.post(
        "/compliance/jobs/from-analysis",
        json={
            "analysis_file": "final1.json",
            "manual_inputs": {"schema_version": "1.0"},
        },
    )
    assert response.status_code == 202
    assert captured["manual_inputs"] is None
    assert not ifc_path.exists()


def test_upstream_404_is_preserved_as_public_404(client, monkeypatch):
    class FakeClient:
        def get_job(self, *_args, **_kwargs):
            raise ComplianceProtocolError(
                "engine returned 404",
                details={"upstream": {"status": 404, "error": {"code": "job_not_found"}}},
            )

    monkeypatch.setattr(compliance_routes, "_client", lambda: FakeClient())
    response = client.get("/compliance/jobs/0123456789ab")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_wait_timeout_maps_to_504(client, monkeypatch):
    class FakeClient:
        def wait_for_job(self, *_args, **_kwargs):
            raise ComplianceTimeout("job timed out", details={"job_id": "0123456789ab"})

    monkeypatch.setattr(compliance_routes, "_client", lambda: FakeClient())
    response = client.get(
        "/compliance/jobs/0123456789ab/wait?timeout_seconds=0.1&poll_interval_seconds=0.1"
    )
    assert response.status_code == 504
    assert response.get_json()["error"]["code"] == "upstream_timeout"


def test_client_retries_only_idempotent_gets():
    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = []
            self.responses = [
                _response(503, {"error": {"code": "busy"}}),
                _response(200, {"status": "ok"}),
            ]

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    session = FakeSession()
    client = ComplianceClient(
        ComplianceClientConfig(
            base_url="http://engine.test", get_retries=1, retry_backoff=0,
        ),
        session=session,
    )
    assert client.health("corr-1")["status"] == "ok"
    assert [call[0] for call in session.calls] == ["GET", "GET"]

    class PostSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.responses = [_response(503, {"error": {"code": "busy"}})]

    post = PostSession()
    client = ComplianceClient(
        ComplianceClientConfig(base_url="http://engine.test", get_retries=4, retry_backoff=0),
        session=post,
    )
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "phase5_no_retry.ifc"
    tmp.write_bytes(b"ISO-10303-21;")
    try:
        with pytest.raises(ComplianceProtocolError):
            client.submit_ifc(tmp, plan_name="p", manual_inputs=None, correlation_id="corr-2")
        assert len(post.calls) == 1
        assert post.calls[0][0] == "POST"
    finally:
        tmp.unlink(missing_ok=True)
