"""
api/main.py
===========
FastAPI service that wraps the compliance pipeline as an async job queue.

Endpoints
---------
  POST /analyze                  submit bim_data → returns {job_id, status}
  GET  /jobs/{job_id}            poll job status + result summary
  GET  /jobs/{job_id}/report/{kind}   download a report (kind = html|pdf|bcf)
  GET  /health                   liveness probe

Run locally:
    CLAUSES_PATH=services/mabhas_clauses.json uvicorn api.main:app --reload

With a real worker (production):
    export CELERY_BROKER_URL=redis://localhost:6379/0
    celery -A api.tasks.celery_app worker --loglevel=info   # in one terminal
    uvicorn api.main:app                                     # in another
"""

from __future__ import annotations

import os
import shutil
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from api.pipeline import parse_building_params, BuildingParamsError
from api.tasks import (submit_job, submit_ifc_job, get_job, get_report,
                       clause_status, EmptyClauseCorpusError, BROKER_URL,
                       INCOMING_DIR)

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
    cs = clause_status()
    # Degraded (but live) when the clause corpus is empty — never report a
    # healthy compliance service that would produce empty reports (Issue 9).
    status = "ok" if cs["clause_count"] > 0 or cs["allow_empty"] else "degraded"
    return {
        "status": status,
        "mode": "celery" if BROKER_URL else "in-process",
        **cs,
    }


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    if not req.bim_data or "rooms" not in req.bim_data:
        raise HTTPException(status_code=400,
                            detail="bim_data must include at least a 'rooms' list")
    if "building_params" in req.bim_data:
        # Validate at the boundary (400 now) rather than failing the async job.
        try:
            req.bim_data["building_params"] = parse_building_params(
                req.bim_data["building_params"])
        except BuildingParamsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    meta = req.meta or {}
    try:
        job_id = submit_job(req.bim_data, meta)
    except EmptyClauseCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return AnalyzeResponse(job_id=job_id, status="queued")


# Uploaded IFC size cap (bytes). Enriched Step-1 plans are single-storey and
# small; 50 MB leaves generous headroom while bounding memory/disk per request.
MAX_IFC_UPLOAD_MB = int(os.environ.get("MAX_IFC_UPLOAD_MB", "50"))


@app.post("/analyze-ifc", response_model=AnalyzeResponse)
def analyze_ifc(file: UploadFile = File(...),
                plan_name: Optional[str] = Form(None),
                building_params: Optional[str] = Form(
                    None,
                    description="Optional JSON object of operator-supplied "
                                "building parameters (mm), e.g. "
                                '{"ceiling_height_mm": 2900}. Values passed '
                                "here override any embedded in the IFC "
                                "contract Pset.")) -> AnalyzeResponse:
    """Issue 3 — primary IFC compliance path: upload a Step-1 enriched plan.ifc,
    the engine ingests it, runs compliance, and produces reports."""
    name = file.filename or "plan.ifc"
    if not name.lower().endswith((".ifc", ".ifczip")):
        raise HTTPException(status_code=400, detail="file must be an .ifc")
    try:
        bp = parse_building_params(building_params)
    except BuildingParamsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    limit = MAX_IFC_UPLOAD_MB * 1024 * 1024
    dest = os.path.join(INCOMING_DIR, f"{uuid.uuid4().hex[:12]}_{os.path.basename(name)}")
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"IFC upload exceeds {MAX_IFC_UPLOAD_MB} MB limit")
                out.write(chunk)
    except HTTPException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    finally:
        file.file.close()
    meta = {"plan_name": plan_name or name}
    try:
        job_id = submit_ifc_job(dest, meta, building_params=bp)
    except EmptyClauseCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
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
    art = get_report(job_id, kind)
    if art is None:
        raise HTTPException(status_code=404,
                            detail=f"No {kind} report available for this job")
    data, fname = art
    return Response(content=data, media_type=_MEDIA[kind],
                    headers={"Content-Disposition":
                             f'attachment; filename="{fname}"'})
