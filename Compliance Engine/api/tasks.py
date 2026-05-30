"""
app/tasks.py
============
Celery task definition + a job store abstraction.

The service works in TWO modes, chosen automatically:
  * BROKER PRESENT  → real Celery worker runs jobs async (production).
  * NO BROKER       → jobs run in a background thread, status tracked in memory
                      (development / testing / single-machine demo).

Either way the API code is identical; it just calls submit_job() / get_job().
"""

from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.pipeline import run_pipeline, load_clauses

# ── Configuration ─────────────────────────────────────────────────────────────
BROKER_URL    = os.environ.get("CELERY_BROKER_URL", "")   # e.g. redis://localhost:6379/0
RESULTS_DIR   = os.environ.get("RESULTS_DIR", "/tmp/compliance_jobs")
CLAUSES_PATH  = os.environ.get("CLAUSES_PATH", "")        # path to mabhas_clauses.json
os.makedirs(RESULTS_DIR, exist_ok=True)

# Cache the clause corpus once at import.
_CLAUSES = load_clauses(CLAUSES_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# In-memory job store (used when no broker; also mirrors Celery state on disk)
# ═══════════════════════════════════════════════════════════════════════════

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job_dir(job_id: str) -> str:
    d = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        # also persist to disk so status survives restarts
        with open(os.path.join(_job_dir(job_id), "status.json"), "w") as f:
            json.dump(job, f)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    # fall back to disk (e.g. after a restart)
    status_file = os.path.join(RESULTS_DIR, job_id, "status.json")
    if os.path.exists(status_file):
        with open(status_file) as f:
            return json.load(f)
    return None


# ═══════════════════════════════════════════════════════════════════════════
# The actual work (shared by both modes)
# ═══════════════════════════════════════════════════════════════════════════

def _execute(job_id: str, bim_data: Dict[str, Any], meta: Dict[str, Any]) -> None:
    _set_job(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        out = run_pipeline(bim_data, _CLAUSES, out_dir=_job_dir(job_id), meta=meta)
        _set_job(job_id, status="completed",
                 finished_at=datetime.now().isoformat(), result=out)
    except Exception as exc:
        _set_job(job_id, status="failed",
                 finished_at=datetime.now().isoformat(),
                 error=str(exc), traceback=traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════════════
# Celery wiring (only if a broker is configured)
# ═══════════════════════════════════════════════════════════════════════════

celery_app = None
if BROKER_URL:
    from celery import Celery
    celery_app = Celery("compliance", broker=BROKER_URL, backend=BROKER_URL)

    @celery_app.task(name="run_compliance_job")
    def _celery_run(job_id: str, bim_data: Dict[str, Any], meta: Dict[str, Any]) -> None:
        _execute(job_id, bim_data, meta)


# ═══════════════════════════════════════════════════════════════════════════
# Public submit/poll API used by the FastAPI layer
# ═══════════════════════════════════════════════════════════════════════════

def submit_job(bim_data: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """Create a job, start it (async via Celery or a thread), return its id."""
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="queued", created_at=datetime.now().isoformat(),
             plan_name=meta.get("plan_name", "Floor plan"))

    if celery_app is not None:
        _celery_run.delay(job_id, bim_data, meta)        # real async
    else:
        # No broker → run in a daemon thread so the API returns immediately.
        t = threading.Thread(target=_execute, args=(job_id, bim_data, meta), daemon=True)
        t.start()

    return job_id


def report_path(job_id: str, kind: str) -> Optional[str]:
    """Resolve the on-disk path of a generated report file for download."""
    job = get_job(job_id)
    if not job or job.get("status") != "completed":
        return None
    fname = (job.get("result", {}).get("reports", {}) or {}).get(kind)
    if not fname:
        return None
    path = os.path.join(RESULTS_DIR, job_id, fname)
    return path if os.path.exists(path) else None
