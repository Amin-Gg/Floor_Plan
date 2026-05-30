"""
app/main.py
===========
FastAPI service that wraps the compliance pipeline as an async job queue.

Endpoints
---------
  POST /analyze                  submit bim_data → returns {job_id, status}
  GET  /jobs/{job_id}            poll job status + result summary
  GET  /jobs/{job_id}/report/{kind}   download a report (kind = html|pdf|bcf)
  GET  /health                   liveness probe

Run locally:
    CLAUSES_PATH=services/mabhas_clauses.json uvicorn app.main:app --reload

With a real worker (production):
    export CELERY_BROKER_URL=redis://localhost:6379/0
    celery -A app.tasks.celery_app worker --loglevel=info   # in one terminal
    uvicorn app.main:app                                     # in another
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.tasks import submit_job, get_job, report_path, BROKER_URL

app = FastAPI(
    title="Mabhas Compliance Service",
    version="1.0",
    description="Submit a floor-plan bim_data, get a Mabhas compliance report.",
)


# ── request/response models ───────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    bim_data: Dict[str, Any]
    meta: Optional[Dict[str, Any]] = None


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str


# ── content types for downloads ───────────────────────────────────────────────

_MEDIA = {
    "html": "text/html",
    "pdf":  "application/pdf",
    "bcf":  "application/octet-stream",
}


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": "celery" if BROKER_URL else "in-process"}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    if not req.bim_data or "rooms" not in req.bim_data:
        raise HTTPException(status_code=400,
                            detail="bim_data must include at least a 'rooms' list")
    meta = req.meta or {}
    job_id = submit_job(req.bim_data, meta)
    return AnalyzeResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    # Return a clean public view
    view = {
        "job_id":  job.get("job_id"),
        "status":  job.get("status"),
        "plan_name": job.get("plan_name"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }
    if job.get("status") == "completed":
        view["result"] = job.get("result")
    if job.get("status") == "failed":
        view["error"] = job.get("error")
    return view


@app.get("/jobs/{job_id}/report/{kind}")
def download_report(job_id: str, kind: str):
    if kind not in _MEDIA:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of {list(_MEDIA)}")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409,
                            detail=f"Job not completed (status: {job.get('status')})")
    path = report_path(job_id, kind)
    if path is None:
        raise HTTPException(status_code=404,
                            detail=f"No {kind} report available for this job")
    filename = f"compliance_{job_id}.{kind}"
    return FileResponse(path, media_type=_MEDIA[kind], filename=filename)
