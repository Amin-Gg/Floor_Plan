#!/usr/bin/env python3
"""Authenticated live Phase-7 acceptance across Stage-1 and the engine."""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "compliance-engine"
BASELINE = ROOT / "release" / "local" / "security-http"
CORRELATION = "phase7-secure-live-acceptance"
PUBLIC_KEY = "phase7-public-" + "a" * 40
INTERNAL_KEY = "phase7-internal-" + "b" * 40


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_ready(url: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{url}/readyz", timeout=2)
            last = f"{response.status_code}: {response.text[:200]}"
            if response.status_code == 200:
                return response.json()
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"engine readiness timeout: {last}")


def report_signature(kind: str, data: bytes) -> bool:
    return {
        "json": data.lstrip().startswith(b"{"),
        "html": b"<html" in data[:1000].lower() or b"<!doctype" in data[:1000].lower(),
        "pdf": data.startswith(b"%PDF-"),
        "bcf": data.startswith(b"PK"),
    }[kind]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=BASELINE)
    args = parser.parse_args()
    baseline = args.out if args.out.is_absolute() else ROOT / args.out
    baseline.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    jobs = Path(tempfile.mkdtemp(prefix="phase7-engine-jobs-"))
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ENGINE), "APP_ENV": "testing", "ALLOW_EMPTY_CLAUSES": "1",
        "JOB_STORE": "local", "RESULTS_DIR": str(jobs), "LLM_PASS_ENABLED": "0",
        "CELERY_BROKER_URL": "", "SECURITY_REQUIRE_AUTH": "1",
        "COMPLIANCE_API_KEYS": INTERNAL_KEY,
    })
    stdout_path = baseline / "engine_live.stdout.txt"
    stderr_path = baseline / "engine_live.stderr.txt"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ENGINE, env=env, stdout=stdout, stderr=stderr, start_new_session=True,
        )
    result: dict[str, Any] = {
        "schema_version": "phase7-secure-live-acceptance-v1", "passed": False,
        "checks": {}, "reports": {}, "correlation_id": CORRELATION,
    }
    old_env = os.environ.copy()
    try:
        wait_ready(url)
        result["checks"]["engine_liveness_public"] = requests.get(f"{url}/livez", timeout=3).status_code == 200
        result["checks"]["engine_health_requires_auth"] = requests.get(f"{url}/health", timeout=3).status_code == 401
        engine_headers = {"X-API-Key": INTERNAL_KEY, "X-Correlation-ID": CORRELATION}
        health = requests.get(f"{url}/health", headers=engine_headers, timeout=5)
        result["checks"]["engine_authenticated_health"] = health.status_code == 200
        result["checks"]["engine_security_headers"] = health.headers.get("X-Content-Type-Options") == "nosniff"
        engine_spec = requests.get(f"{url}/openapi.json", headers=engine_headers, timeout=5).json()
        result["checks"]["engine_openapi_security"] = bool(engine_spec.get("security")) and "ApiKeyAuth" in engine_spec["components"]["securitySchemes"]

        os.environ.update({
            "APP_ENV": "testing", "FLOORPLAN_SKIP_MODEL_INIT": "1", "SECURITY_REQUIRE_AUTH": "1",
            "FLOORPLAN_API_KEYS": PUBLIC_KEY, "COMPLIANCE_API_KEY": INTERNAL_KEY,
            "COMPLIANCE_ENGINE_URL": url, "COMPLIANCE_CONNECT_TIMEOUT_SECONDS": "1",
            "COMPLIANCE_READ_TIMEOUT_SECONDS": "30", "COMPLIANCE_GET_RETRIES": "1",
            "COMPLIANCE_RETRY_BACKOFF_SECONDS": "0.05",
        })
        os.chdir(ROOT)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from application import create_app
        from config.settings import TestingConfig
        app = create_app(TestingConfig)
        app.config.update(TESTING=True)
        client = app.test_client()
        public_headers = {"X-API-Key": PUBLIC_KEY, "X-Correlation-ID": CORRELATION}
        result["checks"]["stage1_liveness_public"] = client.get("/livez").status_code == 200
        result["checks"]["stage1_openapi_requires_auth"] = client.get("/openapi/openapi.json").status_code == 401
        spec_response = client.get("/openapi/openapi.json", headers=public_headers)
        spec = spec_response.get_json()
        result["checks"]["stage1_openapi_security"] = spec_response.status_code == 200 and bool(spec.get("security"))
        result["checks"]["stage1_wrong_key_rejected"] = client.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401

        fixture = ENGINE / "tests" / "fixtures" / "sample_plan.ifc"
        submit = client.post(
            "/compliance/jobs/ifc",
            data={"ifc_file": (io.BytesIO(fixture.read_bytes()), fixture.name), "plan_name": "Phase 7"},
            content_type="multipart/form-data", headers=public_headers,
        )
        if submit.status_code != 202:
            raise AssertionError(f"secure submit failed: {submit.status_code} {submit.get_data(as_text=True)}")
        job_id = submit.get_json()["job_id"]
        result["job_id"] = job_id
        wait = client.get(
            f"/compliance/jobs/{job_id}/wait?timeout_seconds=90&poll_interval_seconds=0.2",
            headers=public_headers,
        )
        job = wait.get_json()["job"] if wait.status_code == 200 else {}
        result["checks"]["job_completed"] = job.get("status") == "completed"
        result["checks"]["correlation_preserved"] = job.get("correlation_id") == CORRELATION
        result["checks"]["traceback_not_exposed"] = "traceback" not in json.dumps(job).lower()
        for kind in ("json", "html", "pdf", "bcf"):
            response = client.get(f"/compliance/jobs/{job_id}/report/{kind}", headers=public_headers)
            result["reports"][kind] = {
                "status": response.status_code, "bytes": len(response.data),
                "signature_valid": response.status_code == 200 and report_signature(kind, response.data),
            }
        result["checks"]["all_reports"] = all(item["signature_valid"] for item in result["reports"].values())
        result["checks"]["engine_upload_cleanup"] = not any((jobs / "_incoming").glob("*"))
        result["passed"] = all(result["checks"].values())
        return 0 if result["passed"] else 1
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        stop_process(process)
        (baseline / "acceptance_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
