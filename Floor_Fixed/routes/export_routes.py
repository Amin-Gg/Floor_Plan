"""
routes/export_routes.py
=======================
Flask blueprint for the /export/ifc endpoint.

Accepts POST with:
    {
        "analysis_file":   "final5.json",       ← filename in outputs/json/
        "building_params": {                    ← all optional, uses defaults if omitted
            "project_name":       "Block 4 - Unit 12",
            "project_address":    "Tehran, District 2",
            "wall_height":        3000,
            "floor_thickness":    250,
            "door_height":        2100,
            "window_sill_height": 900,
            "window_height":      1400
        }
    }

Returns a downloadable .ifc file (application/octet-stream).

Or POST with multipart/form-data with a "bim_json" file upload directly —
useful when you have the JSON locally and want to convert it without
storing it on the server first.
"""

import os
import json
import logging
import tempfile
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file

from config.constants import JSON_OUTPUT_DIR, OUTPUTS_DIR
from export.ifc_exporter import bim_json_to_ifc, DEFAULTS

logger = logging.getLogger(__name__)

bp = Blueprint("export", __name__)

IFC_OUTPUT_DIR = os.path.join(OUTPUTS_DIR, "ifc")
os.makedirs(IFC_OUTPUT_DIR, exist_ok=True)


@bp.route("/export/ifc", methods=["POST"])
def export_ifc():
    """
    Convert a previously analyzed floor plan JSON to IFC4.

    Two modes:
    ── Mode A (JSON body): provide the analysis_file name ──────────────────
        Content-Type: application/json
        Body: {"analysis_file": "final5.json", "building_params": {...}}

    ── Mode B (file upload): upload the bim_data JSON directly ─────────────
        Content-Type: multipart/form-data
        Fields:
            bim_json: (file) — the bim_data dict as a JSON file
            building_params: (string) — JSON string of parameter overrides
    """
    try:
        bim_data       = None
        building_params = {}

        # ── Mode B: direct JSON file upload ──────────────────────────────────
        if "bim_json" in request.files:
            raw = request.files["bim_json"].read()
            payload = json.loads(raw)
            bim_data = _extract_bim_data(payload)

            bp_str = request.form.get("building_params", "{}")
            building_params = json.loads(bp_str)

        # ── Mode A: reference a previously saved analysis file ────────────────
        else:
            body = request.get_json(force=True)
            if not body:
                return jsonify({
                    "error": "Request body is empty. "
                             "Send JSON with 'analysis_file' or upload a 'bim_json' file."
                }), 400

            analysis_file = body.get("analysis_file")
            building_params = body.get("building_params", {})

            if not analysis_file:
                return jsonify({
                    "error": "Missing 'analysis_file'. "
                             "Provide the filename returned by /analyze (e.g. 'final5.json')."
                }), 400

            json_path = os.path.join(JSON_OUTPUT_DIR, analysis_file)
            if not os.path.isfile(json_path):
                return jsonify({
                    "error": f"Analysis file not found: {analysis_file}",
                    "json_directory": JSON_OUTPUT_DIR
                }), 404

            with open(json_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            bim_data = _extract_bim_data(payload)

        if not bim_data:
            return jsonify({
                "error": "Could not find bim_data in the provided JSON. "
                         "The input must be the full response from /analyze."
            }), 400

        # Check for any element content
        n_walls   = len(bim_data.get("walls",   []))
        n_doors   = len(bim_data.get("doors",   []))
        n_windows = len(bim_data.get("windows", []))
        n_rooms   = len(bim_data.get("rooms",   []))

        logger.info(
            f"IFC export requested: {n_walls} walls, {n_doors} doors, "
            f"{n_windows} windows, {n_rooms} rooms"
        )

        # ── Generate IFC file ─────────────────────────────────────────────────
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        ifc_name   = f"floorplan_{timestamp}.ifc"
        ifc_path   = os.path.join(IFC_OUTPUT_DIR, ifc_name)

        bim_json_to_ifc(bim_data, building_params, ifc_path)

        logger.info(f"IFC file generated: {ifc_path}")

        # ── Return file as download ───────────────────────────────────────────
        return send_file(
            ifc_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=ifc_name
        )

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400
    except ImportError as e:
        return jsonify({
            "error": str(e),
            "solution": "Run: pip install ifcopenshell"
        }), 500
    except Exception as e:
        logger.exception("IFC export failed")
        return jsonify({"error": str(e)}), 500


@bp.route("/export/ifc/parameters", methods=["GET"])
def get_ifc_parameters():
    """
    Returns the full list of supported building_params with their defaults.
    Useful for building a form in a front-end or Dynamo UI.

    Response:
    {
        "parameters": {
            "project_name":       {"default": "Floor Plan Project", "type": "string",
                                   "description": "Name shown in the IFC project header"},
            "wall_height":        {"default": 2800.0, "type": "number",
                                   "unit": "mm", "description": "Clear wall height..."},
            ...
        }
    }
    """
    parameters = {
        "project_name": {
            "default":     DEFAULTS["project_name"],
            "type":        "string",
            "description": "Project name stored in the IFC file header"
        },
        "project_address": {
            "default":     DEFAULTS["project_address"],
            "type":        "string",
            "description": "Building postal address (optional)"
        },
        "building_name": {
            "default":     DEFAULTS["building_name"],
            "type":        "string",
            "description": "Name of the IfcBuilding entity"
        },
        "storey_name": {
            "default":     DEFAULTS["storey_name"],
            "type":        "string",
            "description": "Name of the floor storey (e.g. 'Ground Floor', 'First Floor')"
        },
        "storey_elevation": {
            "default":     DEFAULTS["storey_elevation"],
            "type":        "number",
            "unit":        "mm",
            "description": "Elevation of this floor above site datum (0 for ground floor)"
        },
        "wall_height": {
            "default":     DEFAULTS["wall_height"],
            "type":        "number",
            "unit":        "mm",
            "description": "Clear wall height from finished floor level to underside of slab. "
                           "Typical Iranian residential: 2800 mm. Commercial: 3000-3600 mm."
        },
        "floor_thickness": {
            "default":     DEFAULTS["floor_thickness"],
            "type":        "number",
            "unit":        "mm",
            "description": "Structural slab thickness. Typical: 200 mm."
        },
        "door_height": {
            "default":     DEFAULTS["door_height"],
            "type":        "number",
            "unit":        "mm",
            "description": "Clear door opening height. Standard: 2100 mm."
        },
        "window_sill_height": {
            "default":     DEFAULTS["window_sill_height"],
            "type":        "number",
            "unit":        "mm",
            "description": "Height from finished floor to bottom of window opening. "
                           "Standard residential: 900 mm. Kitchen: 1050 mm."
        },
        "window_height": {
            "default":     DEFAULTS["window_height"],
            "type":        "number",
            "unit":        "mm",
            "description": "Clear window opening height. Standard: 1200 mm. "
                           "Head height = sill_height + window_height."
        },
    }

    return jsonify({
        "parameters":   parameters,
        "usage_example": {
            "analysis_file":    "final5.json",
            "building_params":  {
                "project_name":       "Block 4 - Unit 12",
                "wall_height":        3000,
                "window_sill_height": 1050,
                "window_height":      1200,
                "door_height":        2100
            }
        }
    })


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_bim_data(payload: dict) -> dict:
    """
    Extract the bim_data dict from the API payload.
    Handles two cases:
        1. The full /analyze response (contains key "bim_data" nested inside)
        2. The bim_data dict itself (user already extracted it)
    """
    # Case 1: full analysis JSON (has top-level "bim_data" key in wall_analysis)
    if "bim_data" in payload:
        return payload["bim_data"]

    # Case 2: wall_analysis structure saved by save_wall_analysis
    if "walls" in payload and "doors" in payload:
        return payload

    # Case 3: the saved wall_analysis JSON has bim_data nested deeper
    # The save_wall_analysis call wraps everything, so navigate down
    for key in payload:
        if isinstance(payload[key], dict) and "bim_data" in payload[key]:
            return payload[key]["bim_data"]

    return {}
