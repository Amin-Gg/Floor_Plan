from __future__ import annotations

import json

from reporting.report_model import build_validation_report


def test_report_removes_secrets_and_absolute_server_paths():
    secret_path = "/srv/private/jobs/123/input/sample.ifc"
    report = build_validation_report(
        compliance={"findings": [], "summary": {}},
        model={
            "name": secret_path,
            "source_type": "ifc",
            "source_path": secret_path,
            "fingerprint": "abc",
        },
        metadata={
            "plan_name": secret_path,
            "api_token": "SUPER-SECRET",
            "password": "pw",
            "output_dir": "/srv/private/jobs/123/output",
            "nested": {"authorization": "Bearer hidden", "safe": "ok"},
        },
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    ).to_dict()
    text = json.dumps(report)
    assert "SUPER-SECRET" not in text
    assert "Bearer hidden" not in text
    assert "/srv/private" not in text
    assert report["model"]["name"] == "sample.ifc"
    assert report["metadata"]["nested"] == {"safe": "ok"}
