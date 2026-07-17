"""Stage-1 orchestration endpoints for the public compliance-engine API."""
from __future__ import annotations

import json
import os
import tempfile
from io import BytesIO
from pathlib import Path

from flask import g, jsonify, send_file, url_for
from flask_openapi3 import APIBlueprint, Tag

from schemas import (
    ComplianceFromAnalysisRequest,
    ComplianceIFCForm,
    ComplianceJobResponse,
    ComplianceJobStatusResponse,
    ComplianceWaitQuery,
    ErrorResponse,
    JobPath,
    ReportPath,
)
from services.compliance_client import (
    ComplianceClient,
    ComplianceClientError,
    ComplianceProtocolError,
    ComplianceTimeout,
    ComplianceUnavailable,
)
from services.ifc_workflow import AnalysisFileError, create_ifc_artifact, load_analysis_bim
from stage1_contracts import ManualInputsError
from utils.error_handlers import (
    ConflictError,
    GatewayTimeoutError,
    NotFoundError,
    UpstreamServiceError,
    UpstreamUnavailableError,
    ValidationError,
)
from validation.report import IfcContractError
from export.ifc_exporter import IfcExportError

bp = APIBlueprint("compliance", __name__)
TAG = Tag(name="Compliance", description="Orchestration proxy to the compliance engine")


def _client() -> ComplianceClient:
    return ComplianceClient()


def _correlation_id() -> str:
    return getattr(g, "request_id", "n/a")


def _map_client_error(exc: Exception):
    if isinstance(exc, ComplianceTimeout):
        raise GatewayTimeoutError(str(exc), details=getattr(exc, "details", {})) from exc
    if isinstance(exc, ComplianceUnavailable):
        raise UpstreamUnavailableError(str(exc), details=getattr(exc, "details", {})) from exc
    if isinstance(exc, ComplianceProtocolError):
        details = getattr(exc, "details", {})
        upstream = details.get("upstream", {}) if isinstance(details, dict) else {}
        status = upstream.get("status") if isinstance(upstream, dict) else None
        if status == 404:
            raise NotFoundError(str(exc), details=details) from exc
        if status == 409:
            raise ConflictError(str(exc), details=details) from exc
        if status in {400, 422}:
            raise ValidationError(str(exc), details=details) from exc
        raise UpstreamServiceError(str(exc), details=details) from exc
    if isinstance(exc, ComplianceClientError):
        raise ValidationError(str(exc), details=getattr(exc, "details", {})) from exc
    raise exc


def _job_envelope(payload: dict):
    job_id = payload["job_id"]
    return {
        "success": True,
        "request_id": _correlation_id(),
        "correlation_id": payload.get("correlation_id") or _correlation_id(),
        "job_id": job_id,
        "status": payload.get("status", "queued"),
        "status_url": url_for("compliance.compliance_job_status", job_id=job_id, _external=False),
        "reports": {
            kind: url_for(
                "compliance.compliance_report", job_id=job_id, kind=kind, _external=False
            )
            for kind in ("json", "html", "pdf", "bcf")
        },
    }


@bp.get(
    "/compliance/health",
    tags=[TAG],
    summary="Probe the compliance engine",
    responses={502: ErrorResponse, 503: ErrorResponse, 504: ErrorResponse},
)
def compliance_health():
    try:
        payload = _client().health(_correlation_id())
    except Exception as exc:
        _map_client_error(exc)
    return jsonify({
        "success": True,
        "request_id": _correlation_id(),
        "correlation_id": payload.get("correlation_id", _correlation_id()),
        "engine": payload,
    })


@bp.post(
    "/compliance/jobs/from-analysis",
    tags=[TAG],
    summary="Export a saved analysis and submit the IFC to compliance",
    responses={202: ComplianceJobResponse, 400: ErrorResponse, 404: ErrorResponse, 503: ErrorResponse},
)
def submit_compliance_from_analysis(body: ComplianceFromAnalysisRequest):
    artifact = None
    try:
        bim_data = load_analysis_bim(body.analysis_file)
        artifact = create_ifc_artifact(
            bim_data,
            manual_inputs=body.manual_inputs,
            ifc_metadata=body.ifc_metadata.model_dump(),
            request_id=_correlation_id(),
        )
        payload = _client().submit_ifc(
            artifact.path,
            plan_name=body.plan_name or body.analysis_file,
            # The official Stage-1 flow embeds and verifies manual inputs in IFC;
            # no second geometry-changing override is sent to the engine.
            manual_inputs=None,
            correlation_id=_correlation_id(),
        )
        return jsonify(_job_envelope(payload)), 202
    except FileNotFoundError as exc:
        raise NotFoundError("Analysis file not found.", details={"analysis_file": body.analysis_file}) from exc
    except (AnalysisFileError, ManualInputsError, IfcContractError, IfcExportError) as exc:
        raise ValidationError(str(exc)) from exc
    except Exception as exc:
        _map_client_error(exc)
    finally:
        if artifact is not None:
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass


@bp.post(
    "/compliance/jobs/ifc",
    tags=[TAG],
    summary="Submit an existing IFC to the compliance engine",
    responses={202: ComplianceJobResponse, 400: ErrorResponse, 503: ErrorResponse},
)
def submit_compliance_ifc(form: ComplianceIFCForm):
    upload = form.ifc_file
    filename = os.path.basename(upload.filename or "plan.ifc")
    if not filename.lower().endswith((".ifc", ".ifczip")):
        raise ValidationError("ifc_file must have .ifc or .ifczip extension")
    suffix = ".ifczip" if filename.lower().endswith(".ifczip") else ".ifc"
    tmp_path = None
    limit = int(os.getenv("COMPLIANCE_IFC_UPLOAD_MB", "50")) * 1024 * 1024
    written = 0
    try:
        with tempfile.NamedTemporaryFile(prefix="stage1_compliance_", suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValidationError(
                        "IFC upload exceeds the configured size limit.",
                        details={"limit_bytes": limit},
                    )
                tmp.write(chunk)
        payload = _client().submit_ifc(
            tmp_path,
            plan_name=form.plan_name or filename,
            manual_inputs=form.manual_inputs,
            correlation_id=_correlation_id(),
        )
        return jsonify(_job_envelope(payload)), 202
    except Exception as exc:
        _map_client_error(exc)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


@bp.get(
    "/compliance/jobs/<job_id>",
    tags=[TAG],
    summary="Poll compliance job status",
    responses={200: ComplianceJobStatusResponse, 404: ErrorResponse, 503: ErrorResponse},
)
def compliance_job_status(path: JobPath):
    try:
        job = _client().get_job(path.job_id, correlation_id=_correlation_id())
    except Exception as exc:
        _map_client_error(exc)
    return jsonify({
        "success": True,
        "request_id": _correlation_id(),
        "correlation_id": job.get("correlation_id") or _correlation_id(),
        "job": job,
    })


@bp.get(
    "/compliance/jobs/<job_id>/wait",
    tags=[TAG],
    summary="Wait a bounded time for a compliance job",
    responses={200: ComplianceJobStatusResponse, 504: ErrorResponse},
)
def compliance_job_wait(path: JobPath, query: ComplianceWaitQuery):
    try:
        job = _client().wait_for_job(
            path.job_id,
            correlation_id=_correlation_id(),
            timeout_seconds=query.timeout_seconds,
            poll_interval_seconds=query.poll_interval_seconds,
        )
    except Exception as exc:
        _map_client_error(exc)
    return jsonify({
        "success": True,
        "request_id": _correlation_id(),
        "correlation_id": job.get("correlation_id") or _correlation_id(),
        "job": job,
    })


@bp.get(
    "/compliance/jobs/<job_id>/report/<kind>",
    tags=[TAG],
    summary="Download a compliance report",
    responses={404: ErrorResponse, 409: ErrorResponse, 503: ErrorResponse},
)
def compliance_report(path: ReportPath):
    try:
        data, filename, content_type = _client().download_report(
            path.job_id, path.kind, correlation_id=_correlation_id()
        )
    except Exception as exc:
        _map_client_error(exc)
    return send_file(
        BytesIO(data),
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
    )
