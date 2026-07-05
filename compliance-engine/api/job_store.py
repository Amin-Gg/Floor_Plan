"""
api/job_store.py
================
Job status + report-artifact persistence behind one small interface.

Why this exists (review → operator decision, 2026-07): job state used to
live in per-process memory mirrored to the local filesystem. That is fine
for the single-process dev mode, but in the Celery architecture the API
replicas and workers are separate processes — often separate containers —
and filesystem mirroring forces a shared volume and still leaves per-process
memory to reconcile. Redis is already in the stack as the Celery broker, so
job state and finished report artifacts now live there: any API replica can
serve any job, no shared volume required.

Two backends:

* ``RedisJobStore``  — production. Status is a Redis hash per job
  (field values JSON-encoded so nested ``result`` dicts survive), report
  artifacts are byte blobs. Everything carries a TTL (``JOB_TTL_SECONDS``,
  default 7 days) so finished jobs age out instead of accumulating.
* ``LocalJobStore``  — dev / tests / no-broker thread mode. Preserves the
  previous semantics exactly: in-process dict + on-disk ``status.json``
  mirror (disk wins on read — the staleness fix), artifacts served straight
  from the job directory.

Selection (``make_job_store``): explicit ``JOB_STORE_REDIS_URL`` wins; else
a redis-scheme ``CELERY_BROKER_URL`` is reused (state DB defaults to the
broker's); else local. ``JOB_STORE=local`` forces local regardless.

Job-id validation stays in the caller (api.tasks) — ids reaching a store
are already guaranteed to be our minted 12-hex form.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional, Tuple

_TTL_DEFAULT = 7 * 24 * 3600  # seconds; finished jobs age out after a week

# Report kinds we persist as artifacts, with their download filenames.
ARTIFACT_KINDS = ("html", "pdf", "bcf")


# ─────────────────────────────────────────────────────────────────────────────
# Local backend (dev / thread mode) — previous behavior, unchanged semantics
# ─────────────────────────────────────────────────────────────────────────────

class LocalJobStore:
    """In-process dict + on-disk mirror. Disk wins on read (staleness fix)."""

    def __init__(self, results_dir: str):
        self.results_dir = results_dir
        os.makedirs(results_dir, exist_ok=True)
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- status ---------------------------------------------------------------
    def job_dir(self, job_id: str) -> str:
        d = os.path.join(self.results_dir, job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def set_fields(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.setdefault(job_id, {"job_id": job_id})
            job.update(fields)
            with open(os.path.join(self.job_dir(job_id), "status.json"), "w") as f:
                json.dump(job, f)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        # Disk is the source of truth: another process (a worker sharing the
        # volume) may have advanced the job while this process's memory still
        # holds the submit-time entry. Read disk first, merge over memory.
        disk: Optional[Dict[str, Any]] = None
        status_file = os.path.join(self.results_dir, job_id, "status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file) as f:
                    disk = json.load(f)
            except (json.JSONDecodeError, OSError):
                disk = None  # partial write in flight — fall back to memory
        with self._lock:
            mem = dict(self._jobs[job_id]) if job_id in self._jobs else None
        if disk and mem:
            return {**mem, **disk}
        return disk or mem

    # -- uploads -----------------------------------------------------------------
    def store_upload(self, job_id: str, path: str) -> None:
        """Local mode: the worker shares this filesystem — nothing to do."""

    def fetch_upload(self, job_id: str, dest_dir: str) -> Optional[str]:
        """Local mode: no stored copy; the task's original path is used."""
        return None

    # -- artifacts --------------------------------------------------------------
    def store_artifacts(self, job_id: str, out_dir: str,
                        reports: Dict[str, str]) -> None:
        """Local mode: reports already live in the job directory — no-op."""

    def get_artifact(self, job_id: str, kind: str) -> Optional[Tuple[bytes, str]]:
        job = self.get(job_id)
        if not job or job.get("status") != "completed":
            return None
        fname = (job.get("result", {}).get("reports", {}) or {}).get(kind)
        if not fname:
            return None
        path = os.path.join(self.results_dir, job_id, os.path.basename(fname))
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read(), os.path.basename(fname)


# ─────────────────────────────────────────────────────────────────────────────
# Redis backend (production / Celery architecture)
# ─────────────────────────────────────────────────────────────────────────────

class RedisJobStore:
    """Job hash + artifact blobs in Redis, all with a TTL.

    Keys:
        compliance:job:{id}                 hash; values JSON-encoded
        compliance:job:{id}:artifact:{kind} bytes of the finished report
        compliance:job:{id}:artname:{kind}  its download filename
    """

    PREFIX = "compliance:job:"

    def __init__(self, url: str, ttl_seconds: int = _TTL_DEFAULT,
                 client: Any = None):
        if client is None:
            import redis  # already a dependency (Celery broker)
            client = redis.Redis.from_url(url, decode_responses=False)
        self.r = client
        self.ttl = int(ttl_seconds)

    # -- keys -------------------------------------------------------------------
    def _k(self, job_id: str) -> str:
        return f"{self.PREFIX}{job_id}"

    def _ak(self, job_id: str, kind: str) -> str:
        return f"{self.PREFIX}{job_id}:artifact:{kind}"

    def _nk(self, job_id: str, kind: str) -> str:
        return f"{self.PREFIX}{job_id}:artname:{kind}"

    def _uk(self, job_id: str) -> str:
        return f"{self.PREFIX}{job_id}:upload"

    # -- status -------------------------------------------------------------------
    def set_fields(self, job_id: str, **fields: Any) -> None:
        key = self._k(job_id)
        mapping = {"job_id": json.dumps(job_id)}
        mapping.update({k: json.dumps(v) for k, v in fields.items()})
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, self.ttl)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        raw = self.r.hgetall(self._k(job_id))
        if not raw:
            return None
        out: Dict[str, Any] = {}
        for k, v in raw.items():
            k = k.decode() if isinstance(k, bytes) else k
            v = v.decode() if isinstance(v, bytes) else v
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[k] = v  # tolerate foreign writers
        return out

    # -- uploads ---------------------------------------------------------------------
    def store_upload(self, job_id: str, path: str) -> None:
        """Stash the uploaded IFC so a worker in ANOTHER container (no shared
        volume) can fetch it. Upload size is already capped at the API
        boundary (50 MB), well within Redis value limits; the blob carries
        the same TTL as the job and is deleted once the worker fetched it."""
        with open(path, "rb") as f:
            self.r.set(self._uk(job_id), f.read(), ex=self.ttl)
        self.r.set(self._uk(job_id) + ":name",
                   os.path.basename(path).encode(), ex=self.ttl)

    def fetch_upload(self, job_id: str, dest_dir: str) -> Optional[str]:
        data = self.r.get(self._uk(job_id))
        if data is None:
            return None
        name = self.r.get(self._uk(job_id) + ":name")
        if isinstance(name, bytes):
            name = name.decode()
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name or f"{job_id}.ifc")
        with open(dest, "wb") as f:
            f.write(data)
        # One consumer per upload — free the memory immediately.
        self.r.delete(self._uk(job_id))
        self.r.delete(self._uk(job_id) + ":name")
        return dest

    # -- artifacts -------------------------------------------------------------------
    def store_artifacts(self, job_id: str, out_dir: str,
                        reports: Dict[str, str]) -> None:
        """Push the finished report files into Redis so ANY api replica can
        serve downloads — the worker's local out_dir is scratch after this."""
        for kind, fname in (reports or {}).items():
            if kind not in ARTIFACT_KINDS or not fname:
                continue
            path = os.path.join(out_dir, os.path.basename(fname))
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                self.r.set(self._ak(job_id, kind), f.read(), ex=self.ttl)
            self.r.set(self._nk(job_id, kind),
                       os.path.basename(fname).encode(), ex=self.ttl)

    def get_artifact(self, job_id: str, kind: str) -> Optional[Tuple[bytes, str]]:
        data = self.r.get(self._ak(job_id, kind))
        if data is None:
            return None
        name = self.r.get(self._nk(job_id, kind))
        if isinstance(name, bytes):
            name = name.decode()
        return data, (name or f"compliance_{job_id}.{kind}")


# ─────────────────────────────────────────────────────────────────────────────
# Backend selection
# ─────────────────────────────────────────────────────────────────────────────

def make_job_store(results_dir: str,
                   broker_url: Optional[str] = None) -> "LocalJobStore | RedisJobStore":
    """Pick the backend from the environment.

    Priority: JOB_STORE=local (forced) > JOB_STORE_REDIS_URL >
    redis-scheme CELERY_BROKER_URL > LocalJobStore.
    """
    if os.environ.get("JOB_STORE", "").lower() == "local":
        return LocalJobStore(results_dir)
    ttl = int(os.environ.get("JOB_TTL_SECONDS", _TTL_DEFAULT))
    url = os.environ.get("JOB_STORE_REDIS_URL") or ""
    if not url:
        b = broker_url or os.environ.get("CELERY_BROKER_URL") or ""
        if b.startswith(("redis://", "rediss://", "unix://")):
            url = b
    if url:
        try:
            store = RedisJobStore(url, ttl_seconds=ttl)
            store.r.ping()
            return store
        except Exception as exc:  # unreachable Redis → degrade loudly to local
            import logging
            logging.getLogger(__name__).warning(
                "Job store: Redis at %s unreachable (%s) — falling back to "
                "LocalJobStore. Multi-worker status will NOT be consistent.",
                url, exc)
    return LocalJobStore(results_dir)
