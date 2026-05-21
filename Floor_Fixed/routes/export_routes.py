"""
routes/export_routes.py
=======================
Flask blueprint for the /export/ifc endpoint.

Two modes
---------
Mode A — reference a saved analysis JSON by filename:
    POST /export/ifc
    Content-Type: application/json
    {
        "analysis_file":    "final5.json",
        "building_params":  { "wall_height": 3000 }
    }

Mode B — upload a bim_data JSON file directly:
    POST /export/ifc
    Content-Type: multipart/form-data
    Fields:
        bim_json:         (file) the bim_data dict as a JSON file
        building_params:  (string) JSON string of parameter overrides

Both modes return a downloadable .ifc file (application/octet-stream).

GET /export/ifc/parameters
    Returns all supported building_params with defaults, types, and descriptions.
"""

import os
import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file, g

from config.constants import JSON_OUTPUT_DIR, OUTPUTS_DIR
from export.ifc_exporter import bim_json_to_ifc, DEFAULTS
from utils.error_handlers import ValidationError, NotFoundError, APIError
from utils.validators import validate_building_params

logger = logging.getLogger(__name__)

bp = Blueprint("export", __name__)

IFC_OUTPUT_DIR = os.path.join(OUTPUTS_DIR, "ifc")
os.makedirs(IFC_OUTPUT_DIR, exist_ok=True)


@bp.route("/export/ifc", methods=["POST"])
def export_ifc():
    """Convert a previously analyzed floor plan JSON to an IFC4 file."""

    bim_data        = None
    building_params = {}

    # ── Mode B: direct bim_json file upload ──────────────────────────────────
    if "bim_json" in request.files:
        upload = request.files["bim_json"]
        if not upload or upload.filename == "":
            raise ValidationError(
                "File field 'bim_json' is present but no file was selected."
            )
        try:
            payload = json.loads(upload.read())
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"The uploaded bim_json file contains invalid JSON: {exc}",
                details={"filename": upload.filename},
            ) from exc

        bim_data = _extract_bim_data(payload)

        raw_params = request.form.get("building_params", "{}")
        try:
            building_params = validate_building_params(json.loads(raw_params))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"building_params field contains invalid JSON: {exc}"
            ) from exc

    # ── Mode A: reference a previously saved analysis file ───────────────────
    else:
        body = request.get_json(force=True, silent=True)
        if not body:
            raise ValidationError(
                "Request body is empty or not valid JSON. "
                "Send a JSON body with 'analysis_file', or upload a 'bim_json' file.",
                details={
                    "mode_a": "POST /export/ifc  Content-Type: application/json  "
                              "Body: {\"analysis_file\": \"final5.json\", \"building_params\": {...}}",
                    "mode_b": "POST /export/ifc  Content-Type: multipart/form-data  "
                              "Fields: bim_json=(file), building_params=(JSON string)",
                },
            )

        analysis_file   = body.get("analysis_file")
        raw_params      = body.get("building_params", {})
        building_params = validate_building_params(raw_params)

        if not analysis_file:
            raise ValidationError(
                "Missing required field 'analysis_file'. "
                "Provide the filename returned by /analyze (e.g. 'final5.json').",
                details={"received_keys": list(body.keys())},
            )

        if not isinstance(analysis_file, str) or "/" in analysis_file or "\\" in analysis_file:
            raise ValidationError(
                "analysis_file must be a plain filename with no path separators.",
                details={"received": analysis_file},
            )

        json_path = os.path.join(JSON_OUTPUT_DIR, analysis_file)
        if not os.path.isfile(json_path):
            raise NotFoundError(
                f"Analysis file not found: '{analysis_file}'",
                details={
                    "filename":       analysis_file,
                    "search_directory": JSON_OUTPUT_DIR,
                    "hint": "Use the 'analysis_file' value from the /analyze response.",
                },
            )

        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        bim_data = _extract_bim_data(payload)

    # ── Validate bim_data was found ───────────────────────────────────────────
    if not bim_data:
        raise ValidationError(
            "Could not locate bim_data in the provided JSON. "
            "The input must be the full response from /analyze, or the bim_data "
            "section extracted from it.",
        )

    n_walls   = len(bim_data.get("walls",   []))
    n_doors   = len(bim_data.get("doors",   []))
    n_windows = len(bim_data.get("windows", []))
    n_rooms   = len(bim_data.get("rooms",   []))

    if n_walls == 0:
        raise ValidationError(
            "bim_data contains no walls. Cannot generate an IFC model from empty data.",
            details={"walls": n_walls, "doors": n_doors, "windows": n_windows, "rooms": n_rooms},
        )

    logger.info(
        "[%s] IFC export: %d walls, %d doors, %d windows, %d rooms",
        getattr(g, "request_id", "-"), n_walls, n_doors, n_windows, n_rooms,
    )

    # ── Generate IFC file ─────────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ifc_name  = f"floorplan_{timestamp}.ifc"
    ifc_path  = os.path.join(IFC_OUTPUT_DIR, ifc_name)

    try:
        bim_json_to_ifc(bim_data, building_params, ifc_path)
    except ImportError as exc:
        raise APIError(
            "ifcopenshell is not installed on this server.",
            details={"solution": "pip install ifcopenshell"},
        ) from exc

    logger.info("[%s] IFC file generated: %s", getattr(g, "request_id", "-"), ifc_path)

    return send_file(
        ifc_path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=ifc_name,
    )


@bp.route("/export/ifc/parameters", methods=["GET"])
def get_ifc_parameters():
    """
    Returns all supported building_params with their defaults, types, units,
    and descriptions.  Useful for building a form in a front-end or Dynamo UI.
    """
    parameters = {
        "project_name": {
            "default":     DEFAULTS["project_name"],
            "type":        "string",
            "description": "Project name stored in the IFC file header.",
        },
        "project_address": {
            "default":     DEFAULTS["project_address"],
            "type":        "string",
            "description": "Building postal address (optional).",
        },
        "building_name": {
            "default":     DEFAULTS["building_name"],
            "type":        "string",
            "description": "Name of the IfcBuilding entity.",
        },
        "storey_name": {
            "default":     DEFAULTS["storey_name"],
            "type":        "string",
            "description": "Name of the floor storey (e.g. 'Ground Floor', 'First Floor').",
        },
        "storey_elevation": {
            "default":     DEFAULTS["storey_elevation"],
            "type":        "number",
            "unit":        "mm",
            "min":         -5000,
            "max":         50000,
            "description": "Elevation of this floor above site datum. 0 for ground floor.",
        },
        "wall_height": {
            "default":     DEFAULTS["wall_height"],
            "type":        "number",
            "unit":        "mm",
            "min":         500,
            "max":         6000,
            "description": "Clear wall height from finished floor to underside of slab. "
                           "Typical residential: 2800 mm. Commercial: 3000–3600 mm.",
        },
        "floor_thickness": {
            "default":     DEFAULTS["floor_thickness"],
            "type":        "number",
            "unit":        "mm",
            "min":         50,
            "max":         600,
            "description": "Structural slab thickness. Typical: 200 mm.",
        },
        "door_height": {
            "default":     DEFAULTS["door_height"],
            "type":        "number",
            "unit":        "mm",
            "min":         1800,
            "max":         3000,
            "description": "Clear door opening height. Standard: 2100 mm.",
        },
        "window_sill_height": {
            "default":     DEFAULTS["window_sill_height"],
            "type":        "number",
            "unit":        "mm",
            "min":         0,
            "max":         2000,
            "description": "Height from finished floor to bottom of window opening. "
                           "Standard residential: 900 mm. Kitchen: 1050 mm.",
        },
        "window_height": {
            "default":     DEFAULTS["window_height"],
            "type":        "number",
            "unit":        "mm",
            "min":         200,
            "max":         3000,
            "description": "Clear window opening height. Standard: 1200 mm. "
                           "Head height = window_sill_height + window_height.",
        },
    }

    return jsonify({
        "parameters":    parameters,
        "usage_example": {
            "analysis_file":   "final5.json",
            "building_params": {
                "project_name":       "Block 4 - Unit 12",
                "wall_height":        3000,
                "window_sill_height": 1050,
                "window_height":      1200,
                "door_height":        2100,
            },
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Internal helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_bim_data(payload: dict) -> dict:
    """
    Extract the bim_data dict from an API payload, handling three cases:

    1. Payload IS bim_data (has 'walls' and 'doors' keys directly).
    2. Payload is a full /analyze response (has 'bim_data' key at top level).
    3. Payload is a saved wall_analysis JSON where bim_data is nested deeper.
    """
    if not isinstance(payload, dict):
        return {}

    # Case 1 — payload is already bim_data
    if "walls" in payload and "doors" in payload:
        return payload

    # Case 2 — top-level 'bim_data' key
    if "bim_data" in payload:
        return payload["bim_data"]

    # Case 3 — bim_data nested one level deeper (saved wall_analysis format)
    for value in payload.values():
        if isinstance(value, dict) and "bim_data" in value:
            return value["bim_data"]

    return {}
