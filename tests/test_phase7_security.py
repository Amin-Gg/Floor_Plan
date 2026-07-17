from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image


def _testing_app(monkeypatch, *, auth=True, general_rate=120):
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("FLOORPLAN_SKIP_MODEL_INIT", "1")
    monkeypatch.setenv("SECURITY_REQUIRE_AUTH", "1" if auth else "0")
    monkeypatch.setenv("FLOORPLAN_API_KEYS", "a" * 40)
    monkeypatch.setenv("RATE_LIMIT_GENERAL_PER_MINUTE", str(general_rate))
    monkeypatch.setenv("RATE_LIMIT_GENERAL_BURST", "1")
    from application import create_app
    from config.settings import TestingConfig
    return create_app(TestingConfig)


def test_stage1_authentication_and_probe_exemption(monkeypatch):
    app = _testing_app(monkeypatch)
    client = app.test_client()
    assert client.get("/livez").status_code == 200
    denied = client.get("/health")
    assert denied.status_code == 401
    assert denied.json["error"]["code"] == "authentication_required"
    accepted = client.get("/health", headers={"X-API-Key": "a" * 40})
    assert accepted.status_code == 503  # model intentionally skipped, auth passed
    assert accepted.headers["X-Content-Type-Options"] == "nosniff"
    assert "a" * 20 not in json.dumps(denied.json)


def test_stage1_bearer_auth_and_invalid_correlation_id(monkeypatch):
    app = _testing_app(monkeypatch)
    client = app.test_client()
    response = client.get("/health", headers={"Authorization": f"Bearer {'a' * 40}"})
    assert response.status_code == 503
    invalid = client.get(
        "/health",
        headers={"X-API-Key": "a" * 40, "X-Correlation-ID": "bad\x01id"},
    )
    assert invalid.status_code == 400
    assert invalid.json["error"]["code"] == "invalid_correlation_id"


def test_stage1_rate_limit_is_per_identity(monkeypatch):
    app = _testing_app(monkeypatch, general_rate=1)
    client = app.test_client()
    headers = {"X-API-Key": "a" * 40}
    assert client.get("/health", headers=headers).status_code == 503
    limited = client.get("/health", headers=headers)
    assert limited.status_code == 429
    assert limited.json["error"]["code"] == "rate_limit_exceeded"
    assert int(limited.headers["Retry-After"]) >= 1


def test_stage1_production_settings_fail_closed(monkeypatch):
    from utils.security import validate_stage1_production_security
    monkeypatch.setenv("APP_ENV", "production")
    for key in (
        "FLOORPLAN_API_KEYS", "FLOORPLAN_API_KEYS_FILE", "APP_CORS_ORIGINS",
        "APP_ALLOWED_HOSTS", "COMPLIANCE_API_KEY", "COMPLIANCE_API_KEY_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="FLOORPLAN_API_KEYS"):
        validate_stage1_production_security()
    monkeypatch.setenv("FLOORPLAN_API_KEYS", "a" * 40)
    monkeypatch.setenv("APP_CORS_ORIGINS", "*")
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "api.example.test")
    monkeypatch.setenv("COMPLIANCE_API_KEY", "b" * 40)
    with pytest.raises(RuntimeError, match="non-wildcard"):
        validate_stage1_production_security()


def test_image_decompression_limits_are_enforced(monkeypatch):
    monkeypatch.setenv("APP_ENV", "testing")
    from application import create_app
    from config.settings import TestingConfig
    import utils.validators as validators

    app = create_app(TestingConfig)
    monkeypatch.setattr(validators._SECURITY_CONFIG, "MAX_IMAGE_PIXELS", 100)
    raw = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(raw, format="PNG")
    raw.seek(0)
    with app.test_request_context(
        "/upload", method="POST",
        data={"image": (raw, "plan.png", "image/png")},
        content_type="multipart/form-data",
    ):
        with pytest.raises(validators.ImageValidationError, match="safety limits"):
            validators.require_image_upload()


def test_phase7_openapi_declares_api_key_security(monkeypatch):
    app = _testing_app(monkeypatch, auth=False)
    spec = app.api_doc
    assert spec["info"]["version"] == "2.8.0"
    assert "ApiKeyAuth" in spec["components"]["securitySchemes"]
    assert spec["security"]
    assert spec["paths"]["/livez"]["get"]["security"] == []
