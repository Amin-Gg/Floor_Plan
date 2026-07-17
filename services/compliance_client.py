"""Public HTTP client for the compliance engine.

Stage 1 never imports engine internals. The only production boundary is the
engine's documented HTTP API.
"""
from __future__ import annotations

import io
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from utils.security import read_secret

import requests

_JOB_RE = re.compile(r"^[0-9a-f]{12}$")
_REPORT_KINDS = {"json", "html", "pdf", "bcf"}
_RETRYABLE_STATUS = {502, 503, 504}
_TERMINAL = {"completed", "failed", "rejected", "cancelled"}


class ComplianceClientError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class ComplianceUnavailable(ComplianceClientError):
    pass


class ComplianceTimeout(ComplianceClientError):
    pass


class ComplianceProtocolError(ComplianceClientError):
    pass


@dataclass(frozen=True)
class ComplianceClientConfig:
    base_url: str
    connect_timeout: float = 3.0
    read_timeout: float = 30.0
    get_retries: int = 2
    retry_backoff: float = 0.4
    max_report_bytes: int = 100 * 1024 * 1024
    api_key: str = ""
    tls_verify: bool = True

    @classmethod
    def from_env(cls) -> "ComplianceClientConfig":
        base_url = os.getenv("COMPLIANCE_ENGINE_URL", "http://compliance-api:8000").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("COMPLIANCE_ENGINE_URL must be a credential-free http(s) URL")
        return cls(
            base_url=base_url,
            connect_timeout=float(os.getenv("COMPLIANCE_CONNECT_TIMEOUT_SECONDS", "3")),
            read_timeout=float(os.getenv("COMPLIANCE_READ_TIMEOUT_SECONDS", "30")),
            get_retries=max(0, int(os.getenv("COMPLIANCE_GET_RETRIES", "2"))),
            retry_backoff=max(0.0, float(os.getenv("COMPLIANCE_RETRY_BACKOFF_SECONDS", "0.4"))),
            max_report_bytes=max(1024, int(os.getenv("COMPLIANCE_MAX_REPORT_BYTES", str(100 * 1024 * 1024)))),
            api_key=read_secret("COMPLIANCE_API_KEY"),
            tls_verify=os.getenv("COMPLIANCE_TLS_VERIFY", "1") == "1",
        )


class ComplianceClient:
    def __init__(self, config: ComplianceClientConfig | None = None,
                 session: requests.Session | None = None):
        self.config = config or ComplianceClientConfig.from_env()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "FloorPlanToIFC-Orchestrator/2.5"})

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _error_payload(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {"status": response.status_code, "body": response.text[:1000]}
        if isinstance(payload, dict):
            return payload
        return {"status": response.status_code, "body": payload}

    def _request(self, method: str, path: str, *, correlation_id: str,
                 retry_idempotent: bool = False, **kwargs) -> requests.Response:
        attempts = self.config.get_retries + 1 if retry_idempotent else 1
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Correlation-ID"] = correlation_id
        if self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        timeout = kwargs.pop("timeout", (self.config.connect_timeout, self.config.read_timeout))
        kwargs.setdefault("verify", self.config.tls_verify)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, self._url(path), headers=headers, timeout=timeout, **kwargs
                )
            except requests.Timeout as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise ComplianceTimeout(
                        f"Compliance engine timed out during {method} {path}",
                        details={"engine_url": self.config.base_url, "operation": path},
                    ) from exc
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise ComplianceUnavailable(
                        f"Compliance engine is unavailable: {exc}",
                        details={"engine_url": self.config.base_url, "operation": path},
                    ) from exc
            else:
                if response.status_code < 400:
                    return response
                if retry_idempotent and response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    pass
                else:
                    raise ComplianceProtocolError(
                        f"Compliance engine returned HTTP {response.status_code}",
                        details={
                            "engine_url": self.config.base_url,
                            "operation": path,
                            "upstream": self._error_payload(response),
                        },
                    )
            if attempt + 1 < attempts:
                time.sleep(self.config.retry_backoff * (2 ** attempt))

        raise ComplianceUnavailable(str(last_exc or "compliance request failed"))

    def health(self, correlation_id: str) -> dict[str, Any]:
        return self._request(
            "GET", "/health", correlation_id=correlation_id, retry_idempotent=True
        ).json()

    def submit_ifc(self, ifc_path: str | Path, *, plan_name: str | None,
                   manual_inputs: dict[str, Any] | None,
                   correlation_id: str) -> dict[str, Any]:
        path = Path(ifc_path)
        if not path.is_file():
            raise ComplianceClientError(f"IFC file not found: {path}")
        data: dict[str, str] = {}
        if plan_name:
            data["plan_name"] = plan_name
        if manual_inputs is not None:
            import json
            data["manual_inputs"] = json.dumps(manual_inputs, ensure_ascii=False, separators=(",", ":"))
        with path.open("rb") as handle:
            response = self._request(
                "POST", "/analyze-ifc", correlation_id=correlation_id,
                # POST upload is deliberately NOT retried: it is not idempotent.
                files={"file": (path.name, handle, "application/x-step")},
                data=data,
            )
        payload = response.json()
        if not isinstance(payload, dict) or not _JOB_RE.match(str(payload.get("job_id", ""))):
            raise ComplianceProtocolError(
                "Compliance engine returned an invalid job response.",
                details={"payload": payload},
            )
        return payload

    def get_job(self, job_id: str, *, correlation_id: str) -> dict[str, Any]:
        if not _JOB_RE.match(job_id or ""):
            raise ComplianceClientError("Invalid compliance job id.", details={"job_id": job_id})
        payload = self._request(
            "GET", f"/jobs/{job_id}", correlation_id=correlation_id,
            retry_idempotent=True,
        ).json()
        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            raise ComplianceProtocolError("Invalid compliance job status response.")
        return payload

    def wait_for_job(self, job_id: str, *, correlation_id: str,
                     timeout_seconds: float = 60.0,
                     poll_interval_seconds: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            job = self.get_job(job_id, correlation_id=correlation_id)
            if str(job.get("status")) in _TERMINAL:
                return job
            if time.monotonic() >= deadline:
                raise ComplianceTimeout(
                    f"Compliance job {job_id} did not finish within {timeout_seconds:.1f}s.",
                    details={"job_id": job_id, "last_status": job.get("status")},
                )
            time.sleep(poll_interval_seconds)

    def download_report(self, job_id: str, kind: str, *, correlation_id: str) -> tuple[bytes, str, str]:
        if kind not in _REPORT_KINDS:
            raise ComplianceClientError("Invalid report kind.", details={"kind": kind})
        response = self._request(
            "GET", f"/jobs/{job_id}/report/{kind}", correlation_id=correlation_id,
            retry_idempotent=True, stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.config.max_report_bytes:
                raise ComplianceProtocolError("Compliance report exceeds configured size limit.")
            chunks.append(chunk)
        disposition = response.headers.get("Content-Disposition", "")
        filename = f"compliance_{job_id}.{kind}"
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        return b"".join(chunks), filename, response.headers.get("Content-Type", "application/octet-stream")
