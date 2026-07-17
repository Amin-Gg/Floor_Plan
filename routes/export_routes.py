"""IFC export HTTP endpoints with explicit JSON and multipart contracts."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import after_this_request, g, jsonify, send_file
from flask_openapi3 import APIBlueprint, Tag

from export.ifc_exporter import DEFAULTS, IfcExportError
from schemas import (
    ErrorResponse,
    ExportIFCRequest,
    ExportIFCUploadForm,
)
from services.ifc_workflow import (
    AnalysisFileError,
    create_ifc_artifact,
    extract_bim_data,
    load_analysis_bim,
    prepare_bim_data,
    validate_prepared_bim,
)
from stage1_contracts import ManualInputsError
from utils.error_handlers import APIError, NotFoundError, ValidationError
from validation.report import IfcContractError

logger = logging.getLogger(__name__)
bp = APIBlueprint("export", __name__)
TAG = Tag(name="Export", description="Validated IFC Contract 1.2 generation")


def _metadata(raw) -> dict:
    if raw is None:
        return {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise ValidationError("ifc_metadata must be an object")
    allowed = {"project_name", "project_address", "building_name", "storey_name", "storey_elevation"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError("Unknown ifc_metadata fields", details={"fields": unknown})
    return raw


def _map_export_error(exc: Exception):
    if isinstance(exc, FileNotFoundError):
        raise NotFoundError("Analysis file not found.", details={"analysis_file": str(exc)}) from exc
    if isinstance(exc, (AnalysisFileError, ManualInputsError)):
        raise ValidationError(str(exc)) from exc
    if isinstance(exc, IfcContractError):
        raise ValidationError(
            "Generated IFC failed the contract gate.",
            details={"validation": exc.report_dict()},
        ) from exc
    if isinstance(exc, IfcExportError):
        raise ValidationError(
            "IFC export was aborted because geometry would be lost.",
            details={"failures": exc.failures},
        ) from exc
    if isinstance(exc, ImportError):
        raise APIError("ifcopenshell is not installed.") from exc
    raise exc


def _send_artifact(artifact):
    @after_this_request
    def cleanup(response):
        try:
            artifact.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete temporary IFC %s: %s", artifact.path, exc)
        return response

    response = send_file(
        artifact.path,
        mimetype="application/x-step",
        as_attachment=True,
        download_name=artifact.filename,
    )
    counts = artifact.validation.get("counts", {})
    response.headers["X-Model-Validation-Status"] = artifact.validation.get("status", "unknown")
    response.headers["X-Model-Validation-Critical"] = str(counts.get("critical", 0))
    response.headers["X-Model-Validation-Warnings"] = str(counts.get("warning", 0))
    response.headers["X-Manual-Inputs-SHA256"] = str(artifact.manual_meta.get("input_sha256", ""))
    response.headers["X-Manual-Inputs-Resolved-SHA256"] = str(artifact.manual_meta.get("resolved_sha256", ""))
    return response


@bp.post(
    "/export/ifc",
    tags=[TAG],
    summary="Export a saved analysis as IFC4",
    responses={400: ErrorResponse, 404: ErrorResponse, 500: ErrorResponse},
)
def export_ifc(body: ExportIFCRequest):
    """Canonical JSON endpoint. Multipart uploads use `/export/ifc/upload`."""
    try:
        bim_data = load_analysis_bim(body.analysis_file)
        if body.validate_only:
            prepared, manual_meta = prepare_bim_data(
                bim_data, body.manual_inputs, request_id=getattr(g, "request_id", None)
            )
            report = validate_prepared_bim(prepared)
            return jsonify({
                "success": not report.blocked,
                "request_id": getattr(g, "request_id", "-"),
                "validation": report.to_dict(),
                "manual_inputs": manual_meta,
            }), (400 if report.blocked else 200)
        artifact = create_ifc_artifact(
            bim_data,
            manual_inputs=body.manual_inputs,
            ifc_metadata=_metadata(body.ifc_metadata),
            request_id=getattr(g, "request_id", None),
        )
        return _send_artifact(artifact)
    except Exception as exc:
        _map_export_error(exc)


@bp.post(
    "/export/ifc/upload",
    tags=[TAG],
    summary="Export uploaded bim_data JSON as IFC4",
    responses={400: ErrorResponse, 500: ErrorResponse},
)
def export_ifc_upload(form: ExportIFCUploadForm):
    upload = form.bim_json
    try:
        payload = json.loads(upload.read())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("bim_json contains invalid JSON.") from exc
    bim_data = extract_bim_data(payload)
    if not bim_data:
        raise ValidationError("Could not locate bim_data in uploaded JSON.")
    try:
        if form.validate_only:
            prepared, manual_meta = prepare_bim_data(
                bim_data, form.manual_inputs, request_id=getattr(g, "request_id", None)
            )
            report = validate_prepared_bim(prepared)
            return jsonify({
                "success": not report.blocked,
                "request_id": getattr(g, "request_id", "-"),
                "validation": report.to_dict(),
                "manual_inputs": manual_meta,
            }), (400 if report.blocked else 200)
        artifact = create_ifc_artifact(
            bim_data,
            manual_inputs=form.manual_inputs,
            ifc_metadata=_metadata(form.ifc_metadata),
            request_id=getattr(g, "request_id", None),
        )
        return _send_artifact(artifact)
    except Exception as exc:
        _map_export_error(exc)


@bp.get("/export/ifc/parameters", tags=[TAG], summary="Get IFC input contracts")
def get_ifc_parameters():
    return jsonify({
        "ifc_contract": "contracts/ifc_contract_v1_2.json",
        "manual_inputs_contract": "contracts/manual_inputs_v1.json",
        "scale_evidence_contract": "contracts/scale_evidence_v1.json",
        "defaults": {
            "project_name": DEFAULTS["project_name"],
            "project_address": DEFAULTS["project_address"],
            "building_name": DEFAULTS["building_name"],
            "storey_name": DEFAULTS["storey_name"],
            "storey_elevation": DEFAULTS["storey_elevation"],
            "wall_height": DEFAULTS["wall_height"],
            "floor_thickness": DEFAULTS["floor_thickness"],
            "door_height": DEFAULTS["door_height"],
            "window_sill_height": DEFAULTS["window_sill_height"],
            "window_height": DEFAULTS["window_height"],
        },
    })
