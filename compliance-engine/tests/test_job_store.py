"""
tests/test_job_store.py
=======================
Regression tests for the two Celery-mode job-store bugs found in review:

1. STALENESS — the API process's in-memory ``_jobs`` holds the "queued"
   entry written at submit time, while a Celery worker (separate process)
   advances the job on DISK. ``get_job`` must treat disk as the source of
   truth and let it override the stale memory entry, or ``GET /jobs/{id}``
   reports "queued" forever in Celery mode.

2. PATH SAFETY — ``job_id`` comes straight from the URL and is joined into
   a filesystem path; anything that is not exactly a 12-hex-char id we
   minted must be rejected before touching the filesystem.

Plus the API-boundary validation of the manual ``building_params`` field on
``/analyze-ifc`` (threading of the validated dict into submit_ifc_job is
asserted by monkeypatching the submit seam — no compliance run needed).
"""

import json
import os

import pytest

import api.tasks as tasks


# ── helpers ──────────────────────────────────────────────────────────────────

import api.job_store as job_store_mod
from api.job_store import LocalJobStore, RedisJobStore, make_job_store


class FakeRedis:
    """Minimal in-memory stand-in covering exactly the commands RedisJobStore
    uses (hset/hgetall/set/get/expire/ping) — keeps these tests broker-free."""

    def __init__(self):
        self.h = {}          # key -> {field: bytes}
        self.kv = {}         # key -> bytes
        self.ttls = {}       # key -> seconds

    def hset(self, key, mapping=None):
        self.h.setdefault(key, {}).update(
            {k: (v if isinstance(v, bytes) else str(v).encode())
             for k, v in (mapping or {}).items()})

    def hgetall(self, key):
        return {k.encode() if isinstance(k, str) else k: v
                for k, v in self.h.get(key, {}).items()}

    def set(self, key, value, ex=None):
        self.kv[key] = value if isinstance(value, bytes) else str(value).encode()
        if ex:
            self.ttls[key] = ex

    def get(self, key):
        return self.kv.get(key)

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.h.pop(k, None)

    def ping(self):
        return True


@pytest.fixture()
def local_store(tmp_path):
    return LocalJobStore(str(tmp_path))


@pytest.fixture()
def redis_store():
    return RedisJobStore("redis://unused", ttl_seconds=123, client=FakeRedis())


@pytest.fixture()
def job_store(tmp_path, monkeypatch):
    """Route api.tasks through a fresh LocalJobStore for API-level tests."""
    store = LocalJobStore(str(tmp_path))
    monkeypatch.setattr(tasks, "_STORE", store)
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path))
    yield tmp_path


def _write_disk_status(tmp_path, job_id: str, **fields):
    d = tmp_path / job_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {"job_id": job_id, **fields}
    (d / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


JOB = "0123456789ab"  # valid 12-hex id


# ── 1a. LocalJobStore (dev mode) — previous semantics preserved ──────────────

def test_disk_status_overrides_stale_memory(job_store):
    """The original Celery-mode bug shape: memory says 'queued' (submit-time
    entry), disk says 'completed' (another process's truth). Disk must win."""
    tasks._set_job(JOB, status="queued", plan_name="p")
    _write_disk_status(job_store, JOB, status="completed",
                       result={"summary": {"PASS": 1}})
    view = tasks.get_job(JOB)
    assert view["status"] == "completed"
    assert view["result"]["summary"]["PASS"] == 1


def test_memory_fields_survive_merge_when_disk_lacks_them(job_store):
    tasks._set_job(JOB, status="queued", plan_name="my plan")
    _write_disk_status(job_store, JOB, status="running")
    view = tasks.get_job(JOB)
    assert view["status"] == "running"        # disk wins on conflicts
    assert view["plan_name"] == "my plan"     # memory-only fields survive


def test_disk_only_job_readable_after_restart(local_store, tmp_path):
    _write_disk_status(tmp_path, JOB, status="completed", result={})
    assert local_store.get(JOB)["status"] == "completed"


def test_partial_disk_write_falls_back_to_memory(local_store, tmp_path):
    local_store.set_fields(JOB, status="running")
    (tmp_path / JOB / "status.json").write_text('{"status": "comp',
                                                encoding="utf-8")
    assert local_store.get(JOB)["status"] == "running"


def test_local_artifact_served_from_job_dir(local_store, tmp_path):
    d = tmp_path / JOB
    d.mkdir(exist_ok=True)
    (d / "report.html").write_bytes(b"<html>ok</html>")
    local_store.set_fields(JOB, status="completed",
                           result={"reports": {"html": "report.html"}})
    data, name = local_store.get_artifact(JOB, "html")
    assert data == b"<html>ok</html>" and name == "report.html"


def test_unknown_job_returns_none(job_store):
    assert tasks.get_job("ffffffffffff") is None


# ── 1b. RedisJobStore (production) ───────────────────────────────────────────

def test_redis_status_roundtrip_with_nested_result(redis_store):
    redis_store.set_fields(JOB, status="completed",
                           result={"summary": {"PASS": 2, "FAIL": 1}})
    view = redis_store.get(JOB)
    assert view["job_id"] == JOB
    assert view["status"] == "completed"
    assert view["result"]["summary"] == {"PASS": 2, "FAIL": 1}


def test_redis_incremental_field_updates_merge(redis_store):
    redis_store.set_fields(JOB, status="queued", plan_name="p1")
    redis_store.set_fields(JOB, status="running")
    view = redis_store.get(JOB)
    assert view["status"] == "running" and view["plan_name"] == "p1"


def test_redis_missing_job_is_none(redis_store):
    assert redis_store.get("ffffffffffff") is None


def test_redis_artifacts_roundtrip(redis_store, tmp_path):
    (tmp_path / "compliance_report.html").write_bytes(b"<html>r</html>")
    redis_store.store_artifacts(JOB, str(tmp_path),
                                {"html": "compliance_report.html",
                                 "pdf": "missing.pdf"})   # absent file skipped
    data, name = redis_store.get_artifact(JOB, "html")
    assert data == b"<html>r</html>" and name == "compliance_report.html"
    assert redis_store.get_artifact(JOB, "pdf") is None


def test_redis_everything_carries_ttl(redis_store, tmp_path):
    (tmp_path / "r.html").write_bytes(b"x")
    redis_store.set_fields(JOB, status="completed")
    redis_store.store_artifacts(JOB, str(tmp_path), {"html": "r.html"})
    fr = redis_store.r
    assert fr.ttls[redis_store._k(JOB)] == 123
    assert fr.ttls[redis_store._ak(JOB, "html")] == 123


# ── 1c. Backend selection ─────────────────────────────────────────────────────

def test_job_store_env_forces_local(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_STORE", "local")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    assert isinstance(make_job_store(str(tmp_path)), LocalJobStore)


def test_non_redis_broker_selects_local(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_STORE", raising=False)
    monkeypatch.delenv("JOB_STORE_REDIS_URL", raising=False)
    assert isinstance(
        make_job_store(str(tmp_path), broker_url="amqp://guest@mq//"),
        LocalJobStore)


def test_redis_broker_selects_redis(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_STORE", raising=False)
    monkeypatch.delenv("JOB_STORE_REDIS_URL", raising=False)

    class _FakeStore(RedisJobStore):
        def __init__(self, url, ttl_seconds):
            super().__init__(url, ttl_seconds, client=FakeRedis())

    monkeypatch.setattr(job_store_mod, "RedisJobStore", _FakeStore)
    store = make_job_store(str(tmp_path), broker_url="redis://broker:6379/0")
    assert isinstance(store, RedisJobStore)


def test_unreachable_redis_degrades_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_STORE", raising=False)
    monkeypatch.setenv("JOB_STORE_REDIS_URL", "redis://127.0.0.1:1/0")

    class _Boom(RedisJobStore):
        def __init__(self, url, ttl_seconds):
            raise ConnectionError("nope")

    monkeypatch.setattr(job_store_mod, "RedisJobStore", _Boom)
    assert isinstance(make_job_store(str(tmp_path)), LocalJobStore)


# ── 2. job-id guard (path traversal / store access) ───────────────────────────

@pytest.mark.parametrize("bad", [
    "..", "../..", "a/../../etc", "0123456789AB",  # uppercase not minted
    "0123456789abc",                                # wrong length
    "0123456789a",                                  # wrong length
    "..%2F..%2Fetc", "", None, "status", ".hidden",
])
def test_invalid_job_ids_rejected_before_store(job_store, bad):
    assert tasks.get_job(bad) is None


def test_traversal_id_cannot_read_outside_results_dir(job_store, tmp_path):
    outside = tmp_path.parent / "status.json"
    outside.write_text('{"status": "secret"}', encoding="utf-8")
    try:
        assert tasks.get_job("..") is None
    finally:
        outside.unlink(missing_ok=True)


# ── 3. API boundary: /jobs + /analyze-ifc building_params ────────────────────

@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from api import main as api_main
    return TestClient(api_main.app), api_main


def test_jobs_endpoint_404_for_invalid_id(client):
    c, _ = client
    assert c.get("/jobs/0123456789AB").status_code == 404
    assert c.get("/jobs/deadbeef").status_code == 404


def _post_ifc(c, params=None):
    files = {"file": ("plan.ifc", b"ISO-10303-21; DUMMY", "application/octet-stream")}
    data = {}
    if params is not None:
        data["building_params"] = params
    return c.post("/analyze-ifc", files=files, data=data)


def test_analyze_ifc_rejects_malformed_params_json(client):
    c, _ = client
    r = _post_ifc(c, "{not json")
    assert r.status_code == 400
    assert "building_params" in r.json()["detail"]


def test_analyze_ifc_rejects_non_object_params(client):
    c, _ = client
    assert _post_ifc(c, "[1, 2]").status_code == 400


def test_analyze_ifc_rejects_out_of_range_params(client):
    c, _ = client
    r = _post_ifc(c, json.dumps({"wall_height": 99999}))
    assert r.status_code == 400
    assert "wall_height" in r.json()["detail"]


def test_analyze_ifc_rejects_unknown_param_keys(client):
    c, _ = client
    r = _post_ifc(c, json.dumps({"walll_height": 2800}))
    assert r.status_code == 400


def test_analyze_ifc_threads_validated_params_to_submit(client, monkeypatch):
    c, api_main = client
    captured = {}

    def fake_submit(ifc_path, meta, building_params=None):
        captured["params"] = building_params
        return "0123456789ab"

    monkeypatch.setattr(api_main, "submit_ifc_job", fake_submit)
    r = _post_ifc(c, json.dumps({"wall_height": 3000, "window_sill_height": 950}))
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == "0123456789ab"
    # Section-1 spellings are accepted and normalized to the canonical
    # _mm engine vocabulary at the boundary.
    assert captured["params"] == {"wall_height_mm": 3000.0,
                                  "window_sill_height_mm": 950.0}


def test_analyze_ifc_no_params_passes_empty_dict(client, monkeypatch):
    """parse_building_params' documented contract: absent input -> {} (falsy),
    which BimAdapter treats identically to None (no operator assertions)."""
    c, api_main = client
    captured = {"params": "sentinel"}

    def fake_submit(ifc_path, meta, building_params=None):
        captured["params"] = building_params
        return "0123456789ab"

    monkeypatch.setattr(api_main, "submit_ifc_job", fake_submit)
    assert _post_ifc(c).status_code == 200
    assert captured["params"] == {}


# ── Upload transport through the store (container-isolated workers) ──────────

def test_redis_upload_stash_and_fetch(redis_store, tmp_path):
    src = tmp_path / "up.ifc"
    src.write_bytes(b"ISO-10303-21; DATA")
    redis_store.store_upload(JOB, str(src))
    dest_dir = tmp_path / "worker_scratch"
    fetched = redis_store.fetch_upload(JOB, str(dest_dir))
    assert fetched and fetched.endswith("up.ifc")
    assert open(fetched, "rb").read() == b"ISO-10303-21; DATA"
    # single-consumer: the blob is freed after the fetch
    assert redis_store.fetch_upload(JOB, str(dest_dir)) is None


def test_local_upload_is_passthrough(local_store, tmp_path):
    src = tmp_path / "up.ifc"
    src.write_bytes(b"x")
    local_store.store_upload(JOB, str(src))          # no-op, must not raise
    assert local_store.fetch_upload(JOB, str(tmp_path)) is None
