"""Production security boundary for the Stage-1 Flask API.

This module intentionally uses only the Python standard library and Flask so it
is available before TensorFlow/PyTorch are imported.  It provides:

* constant-time API-key authentication (env or Docker secret file)
* per-identity token-bucket rate limiting
* bounded concurrent heavy requests
* strict Host/Content-Length/correlation-id validation
* security response headers

The limiter is process-local by design.  The production deployment uses one
Gunicorn worker per GPU; horizontal replicas must additionally enforce a shared
limit at the ingress/reverse-proxy layer.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from flask import Response, g, jsonify, request

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def read_secret(name: str, default: str = "") -> str:
    """Read ``NAME_FILE`` first, then ``NAME``; never log the returned value."""
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Unable to read secret file for {name}: {file_name}") from exc
    return os.getenv(name, default).strip()


def parse_secret_list(name: str) -> tuple[str, ...]:
    raw = read_secret(name)
    values = []
    for item in raw.replace("\n", ",").split(","):
        value = item.strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _constant_time_member(candidate: str, values: Iterable[str]) -> bool:
    matched = False
    for value in values:
        matched = hmac.compare_digest(candidate.encode(), value.encode()) or matched
    return matched


@dataclass(frozen=True)
class RatePolicy:
    per_minute: int
    burst: int


@dataclass
class _Bucket:
    tokens: float
    updated: float
    last_seen: float


class TokenBucketLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def consume(self, identity: str, category: str, policy: RatePolicy) -> tuple[bool, int, float]:
        now = time.monotonic()
        rate = max(1, policy.per_minute) / 60.0
        capacity = float(max(1, policy.burst))
        key = (identity, category)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=capacity, updated=now, last_seen=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
            bucket.updated = now
            bucket.last_seen = now
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            remaining = max(0, int(bucket.tokens))
            retry_after = 0.0 if allowed else max(0.05, (1.0 - bucket.tokens) / rate)
            if now - self._last_cleanup > 300:
                cutoff = now - 900
                self._buckets = {k: v for k, v in self._buckets.items() if v.last_seen >= cutoff}
                self._last_cleanup = now
        return allowed, remaining, retry_after


class HeavyRequestGate:
    def __init__(self, capacity: int) -> None:
        self._sem = threading.BoundedSemaphore(max(1, capacity))

    def acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def validate_stage1_production_security() -> None:
    if os.getenv("APP_ENV", "development").lower() != "production":
        return
    keys = parse_secret_list("FLOORPLAN_API_KEYS")
    if not keys:
        raise RuntimeError(
            "FLOORPLAN_API_KEYS or FLOORPLAN_API_KEYS_FILE is required in production"
        )
    if any(len(key) < 32 for key in keys):
        raise RuntimeError("Every production FLOORPLAN_API_KEYS entry must be at least 32 characters")
    origins = os.getenv("APP_CORS_ORIGINS", "").strip()
    if not origins or "*" in {x.strip() for x in origins.split(",")}:
        raise RuntimeError("APP_CORS_ORIGINS must be a non-wildcard allow-list in production")
    hosts = os.getenv("APP_ALLOWED_HOSTS", "").strip()
    if not hosts:
        raise RuntimeError("APP_ALLOWED_HOSTS is required in production")
    compliance_key = read_secret("COMPLIANCE_API_KEY")
    if not compliance_key or len(compliance_key) < 32:
        raise RuntimeError("COMPLIANCE_API_KEY/FILE with at least 32 characters is required")


def install_flask_security(app) -> None:
    environment = os.getenv("APP_ENV", "development").lower()
    production = environment == "production"
    keys = parse_secret_list("FLOORPLAN_API_KEYS")
    auth_enabled = production or bool(keys) or os.getenv("SECURITY_REQUIRE_AUTH", "0") == "1"
    allowed_hosts = {
        item.strip().lower() for item in os.getenv("APP_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
        if item.strip()
    }
    max_body_bytes = _int_env("MAX_REQUEST_BODY_MB", 64) * 1024 * 1024
    limiter = TokenBucketLimiter()
    heavy_gate = HeavyRequestGate(_int_env("MAX_CONCURRENT_HEAVY_REQUESTS", 1))
    policies = {
        "heavy": RatePolicy(_int_env("RATE_LIMIT_HEAVY_PER_MINUTE", 6), _int_env("RATE_LIMIT_HEAVY_BURST", 2)),
        "poll": RatePolicy(_int_env("RATE_LIMIT_POLL_PER_MINUTE", 180), _int_env("RATE_LIMIT_POLL_BURST", 60)),
        "general": RatePolicy(_int_env("RATE_LIMIT_GENERAL_PER_MINUTE", 120), _int_env("RATE_LIMIT_GENERAL_BURST", 30)),
    }
    unauthenticated = {"/livez", "/readyz"}
    heavy_prefixes = (
        "/analyze", "/analyze_accuracy", "/export/", "/compliance/jobs/ifc",
        "/compliance/jobs/from-analysis",
    )

    def error(status: int, code: str, message: str, details: dict | None = None) -> Response:
        response = jsonify({
            "success": False,
            "request_id": getattr(g, "request_id", "n/a"),
            "error": {
                "code": code,
                "message": message,
                "status": status,
                "type": "SecurityError",
                "details": details or {},
            },
        })
        response.status_code = status
        return response

    @app.before_request
    def _security_before_request():
        if request.method == "OPTIONS":
            return None
        path = request.path
        host = (request.host or "").split(":", 1)[0].lower()
        if production and host not in allowed_hosts:
            return error(400, "invalid_host", "Request Host is not in the production allow-list.")
        incoming_id = (
            request.headers.get("X-Correlation-ID")
            or request.headers.get("X-Request-ID")
            or ""
        )
        if len(incoming_id) > 128 or _CONTROL_RE.search(incoming_id):
            return error(400, "invalid_correlation_id", "Correlation ID contains invalid characters.")
        content_length = request.content_length
        if content_length is not None and content_length > max_body_bytes:
            return error(413, "payload_too_large", "Request body exceeds the configured limit.", {"limit_bytes": max_body_bytes})

        candidate = ""
        if path not in unauthenticated and auth_enabled:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                candidate = auth[7:].strip()
            candidate = request.headers.get("X-API-Key", "").strip() or candidate
            if not candidate or not _constant_time_member(candidate, keys):
                response = error(401, "authentication_required", "A valid API key is required.")
                response.headers["WWW-Authenticate"] = "Bearer"
                return response
            g.auth_identity = secret_fingerprint(candidate)
        else:
            remote = request.remote_addr or "unknown"
            g.auth_identity = f"ip:{remote}"

        category = "heavy" if request.method == "POST" and path.startswith(heavy_prefixes) else (
            "poll" if request.method == "GET" and path.startswith("/compliance/jobs/") else "general"
        )
        allowed, remaining, retry_after = limiter.consume(g.auth_identity, category, policies[category])
        g.rate_limit_category = category
        g.rate_limit_remaining = remaining
        if not allowed:
            response = error(429, "rate_limit_exceeded", "Too many requests.", {"category": category})
            response.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
            return response
        if category == "heavy":
            if not heavy_gate.acquire():
                response = error(429, "server_busy", "The inference/export queue is at capacity.")
                response.headers["Retry-After"] = "2"
                return response
            g.heavy_gate_acquired = True
        return None


    @app.teardown_request
    def _security_teardown(_exc):
        if getattr(g, "heavy_gate_acquired", False):
            heavy_gate.release()
            g.heavy_gate_acquired = False

    @app.after_request
    def _security_after_request(response):
        if getattr(g, "heavy_gate_acquired", False):
            heavy_gate.release()
            g.heavy_gate_acquired = False
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if hasattr(g, "rate_limit_remaining"):
            response.headers["X-RateLimit-Remaining"] = str(g.rate_limit_remaining)
            response.headers["X-RateLimit-Category"] = str(g.rate_limit_category)
        return response
