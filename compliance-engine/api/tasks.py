"""
api/tasks.py
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
import re
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from api.pipeline import configure_advisory, load_clauses, clause_health
from services.validation_pipeline import (PipelineRequest,
                                          run_validation_pipeline)

# ── Configuration ─────────────────────────────────────────────────────────────
BROKER_URL    = os.environ.get("CELERY_BROKER_URL", "")   # e.g. redis://localhost:6379/0
RESULTS_DIR   = os.environ.get("RESULTS_DIR", "/tmp/compliance_jobs")
CLAUSES_PATH  = os.environ.get("CLAUSES_PATH", "")        # path to mabhas_clauses.json
# Issue 9 — refuse to run compliance against an empty corpus unless explicitly
# allowed (test mode). Set ALLOW_EMPTY_CLAUSES=1 to bypass in tests/demos.
ALLOW_EMPTY_CLAUSES = os.environ.get("ALLOW_EMPTY_CLAUSES", "0") == "1"
INCOMING_DIR  = os.path.join(RESULTS_DIR, "_incoming")
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(INCOMING_DIR, exist_ok=True)

# Cache the clause corpus once at import.
_CLAUSES = load_clauses(CLAUSES_PATH)


class EmptyClauseCorpusError(RuntimeError):
    """Raised when a job is submitted but no regulation clauses are loaded."""


def clause_status() -> Dict[str, Any]:
    """Clause-corpus health for the /health endpoint (Issue 9)."""
    h = clause_health(CLAUSES_PATH)
    h["allow_empty"] = ALLOW_EMPTY_CLAUSES
    return h


def _guard_clauses() -> None:
    if not _CLAUSES and not ALLOW_EMPTY_CLAUSES:
        raise EmptyClauseCorpusError(
            "Compliance clause corpus is empty — refusing to run a job that would "
            "produce a misleading empty report. Set CLAUSES_PATH to a valid "
            "mabhas_clauses.json (or ALLOW_EMPTY_CLAUSES=1 for test mode).")


# ═══════════════════════════════════════════════════════════════════════════
# Job store — Redis in the Celery architecture, local memory+disk in dev.
# See api/job_store.py for the backends and the selection rules.
# ═══════════════════════════════════════════════════════════════════════════

from api.job_store import make_job_store  # noqa: E402

_STORE = make_job_store(RESULTS_DIR, broker_url=BROKER_URL)


def _job_dir(job_id: str) -> str:
    """Per-job scratch directory the pipeline writes reports into. In Redis
    mode this is worker-local scratch: artifacts are pushed into Redis after
    completion, so nothing here needs to be shared between containers."""
    d = os.path.join(RESULTS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _set_job(job_id: str, **fields) -> None:
    _STORE.set_fields(job_id, **fields)


_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    # job_id comes straight from the URL — never let it touch the store (or,
    # in local mode, the filesystem) unless it looks exactly like an id we
    # minted (uuid4().hex[:12]).
    if not _JOB_ID_RE.match(job_id or ""):
        return None
    return _STORE.get(job_id)


# ═══════════════════════════════════════════════════════════════════════════
# The actual work (shared by both modes)
# ═══════════════════════════════════════════════════════════════════════════

def _execute(job_id: str, bim_data: Dict[str, Any], meta: Dict[str, Any],
             manual_inputs: Optional[Dict[str, Any]] = None) -> None:
    _set_job(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        execution = run_validation_pipeline(configure_advisory(PipelineRequest(
            source_type="bim_data", bim_data=bim_data, clauses=_CLAUSES,
            out_dir=_job_dir(job_id), metadata=meta, manual_inputs=manual_inputs,
        )))
        out = execution.to_api_response()
        _STORE.store_artifacts(job_id, _job_dir(job_id), execution.reports)
        _set_job(job_id, status="completed",
                 finished_at=datetime.now().isoformat(), result=out)
    except Exception as exc:
        _set_job(job_id, status="failed",
                 finished_at=datetime.now().isoformat(),
                 error=str(exc), traceback=traceback.format_exc())


def _execute_ifc(job_id: str, ifc_path: str, meta: Dict[str, Any],
                 manual_inputs: Optional[Dict[str, Any]] = None) -> None:
    """Run the compliance pipeline from an uploaded IFC file (Issue 3)."""
    _set_job(job_id, status="running", started_at=datetime.now().isoformat())
    try:
        if not os.path.exists(ifc_path):
            # Different container than the API (no shared volume) — pull the
            # upload from the job store into local scratch.
            fetched = _STORE.fetch_upload(job_id, _job_dir(job_id))
            if fetched is None:
                raise FileNotFoundError(
                    f"uploaded IFC not found at {ifc_path} and no stored "
                    f"copy in the job store — API and worker share neither "
                    f"a volume nor a Redis job store")
            ifc_path = fetched
        execution = run_validation_pipeline(configure_advisory(PipelineRequest(
            source_type="ifc", ifc_path=ifc_path, clauses=_CLAUSES,
            out_dir=_job_dir(job_id), metadata=meta, manual_inputs=manual_inputs,
        )))
        out = execution.to_api_response()
        _STORE.store_artifacts(job_id, _job_dir(job_id), execution.reports)
        if execution.blocked:
            _set_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                     error=execution.blocked_reason, result=out)
        else:
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
    def _celery_run(job_id: str, bim_data: Dict[str, Any], meta: Dict[str, Any],
                    manual_inputs: Optional[Dict[str, Any]] = None) -> None:
        _execute(job_id, bim_data, meta, manual_inputs)

    @celery_app.task(name="run_compliance_job_ifc")
    def _celery_run_ifc(job_id: str, ifc_path: str, meta: Dict[str, Any],
                        manual_inputs: Optional[Dict[str, Any]] = None) -> None:
        _execute_ifc(job_id, ifc_path, meta, manual_inputs)


# ═══════════════════════════════════════════════════════════════════════════
# Public submit/poll API used by the FastAPI layer
# ═══════════════════════════════════════════════════════════════════════════

def submit_job(bim_data: Dict[str, Any], meta: Dict[str, Any],
               manual_inputs: Optional[Dict[str, Any]] = None) -> str:
    """Create a job, start it (async via Celery or a thread), return its id."""
    _guard_clauses()
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="queued", created_at=datetime.now().isoformat(),
             plan_name=meta.get("plan_name", "Floor plan"))

    if celery_app is not None:
        _celery_run.delay(job_id, bim_data, meta, manual_inputs)        # real async
    else:
        # No broker → run in a daemon thread so the API returns immediately.
        t = threading.Thread(target=_execute, args=(job_id, bim_data, meta, manual_inputs), daemon=True)
        t.start()

    return job_id


def submit_ifc_job(ifc_path: str, meta: Dict[str, Any],
                   manual_inputs: Optional[Dict[str, Any]] = None) -> str:
    """Create an IFC-based compliance job and return its id."""
    _guard_clauses()
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="queued", created_at=datetime.now().isoformat(),
             plan_name=meta.get("plan_name", "Floor plan (IFC)"))

    if celery_app is not None:
        # The worker may run in another container with NO shared volume: put
        # the upload into the job store (Redis blob; no-op in local mode) so
        # the worker can fetch it if the path below is not on its filesystem.
        _STORE.store_upload(job_id, ifc_path)
        _celery_run_ifc.delay(job_id, ifc_path, meta, manual_inputs)
    else:
        t = threading.Thread(target=_execute_ifc,
                             args=(job_id, ifc_path, meta, manual_inputs),
                             daemon=True)
        t.start()

    return job_id


def get_report(job_id: str, kind: str) -> Optional[tuple]:
    """Return (bytes, filename) of a finished report, from whichever backend
    holds it — Redis blob in production, the job directory in local mode.
    None when the job is missing, unfinished, or has no such report."""
    if not _JOB_ID_RE.match(job_id or ""):
        return None
    job = _STORE.get(job_id)
    if not job or job.get("status") != "completed":
        return None
    return _STORE.get_artifact(job_id, kind)
