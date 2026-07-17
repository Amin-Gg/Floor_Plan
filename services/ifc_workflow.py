"""Reusable Stage-1 IFC workflow used by export and orchestration routes."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config.constants import JSON_OUTPUT_DIR, OUTPUTS_DIR
from export.ifc_exporter import bim_json_to_ifc
from stage1_contracts import build_measurement_provenance, resolve_manual_inputs
from validation import merge_reports, validate_bim_data, validate_ifc_file

IFC_OUTPUT_DIR = Path(OUTPUTS_DIR) / "ifc"
IFC_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class AnalysisFileError(ValueError):
    pass


@dataclass
class IFCArtifact:
    path: Path
    filename: str
    bim_data: dict[str, Any]
    manual_meta: dict[str, Any]
    validation: dict[str, Any]


def extract_bim_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if "walls" in payload and "doors" in payload:
        return payload
    if isinstance(payload.get("bim_data"), dict):
        return payload["bim_data"]
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("bim_data"), dict):
            return value["bim_data"]
    return {}


def load_analysis_bim(analysis_file: str) -> dict[str, Any]:
    name = str(analysis_file or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise AnalysisFileError("analysis_file must be a plain filename")
    path = Path(JSON_OUTPUT_DIR) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisFileError(f"Analysis JSON is unreadable: {exc}") from exc
    bim_data = extract_bim_data(payload)
    if not bim_data:
        raise AnalysisFileError("Could not locate bim_data in the analysis file")
    return bim_data


def prepare_bim_data(
    bim_data: dict[str, Any],
    manual_inputs: dict[str, Any] | str | None,
    *,
    request_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Preserve an already-resolved manifest only when no new manual payload is supplied.
    if manual_inputs is not None or not isinstance(bim_data.get("manual_inputs"), dict):
        resolved, manual_meta = resolve_manual_inputs(bim_data, manual_inputs)
        resolved = build_measurement_provenance(
            resolved,
            context={
                "request_id": request_id,
                "model_version": "stage1_export",
                "weight_version": os.environ.get("MODEL_WEIGHTS_VERSION", "unknown"),
            },
        )
        return resolved, manual_meta
    return dict(bim_data), dict(bim_data["manual_inputs"])


def validate_prepared_bim(bim_data: dict[str, Any]):
    if not bim_data.get("walls"):
        raise AnalysisFileError("bim_data contains no walls")
    return validate_bim_data(bim_data)


def create_ifc_artifact(
    bim_data: dict[str, Any],
    *,
    manual_inputs: dict[str, Any] | str | None,
    ifc_metadata: dict[str, Any],
    request_id: str | None,
) -> IFCArtifact:
    prepared, manual_meta = prepare_bim_data(
        bim_data, manual_inputs, request_id=request_id
    )
    pre_report = validate_prepared_bim(prepared)
    if pre_report.blocked:
        raise AnalysisFileError(
            "Model failed pre-export validation: "
            + ", ".join(i.code for i in pre_report.issues if i.severity == "critical")
        )

    filename = (
        f"floorplan_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}.ifc"
    )
    path = IFC_OUTPUT_DIR / filename
    bim_json_to_ifc(prepared, ifc_metadata, str(path))
    post_report = validate_ifc_file(str(path))
    combined = merge_reports("export", pre_report, post_report)
    return IFCArtifact(
        path=path,
        filename=filename,
        bim_data=prepared,
        manual_meta=manual_meta,
        validation=combined,
    )
