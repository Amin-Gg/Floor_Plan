"""Build auditable measurement provenance without affecting contract hashes."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Mapping


def _record(*, value: Any, unit: str, source: str, confidence: float | None,
            raw_pixel_measurement: float | None, raw_measurement_source: str | None,
            scale: Mapping[str, Any], context: Mapping[str, Any],
            override_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "source": source,
        "confidence": confidence,
        "raw_pixel_measurement": raw_pixel_measurement,
        "raw_measurement_source": raw_measurement_source,
        "scale_evidence_id": (scale.get("evidence_ids") or [None])[0],
        "scale_evidence_sha256": scale.get("evidence_sha256"),
        "transformation_chain": [
            "source_image_pixels",
            f"multiply_by_{scale.get('mm_per_pixel')}_mm_per_pixel",
            "canonical_bim_mm",
            "ifc_project_units_mm",
        ] if raw_pixel_measurement is not None else ["operator_or_system_value", "canonical_bim_mm", "ifc_project_units_mm"],
        "model_version": context.get("model_version") or os.environ.get("MODEL_VERSION", "unknown"),
        "weight_version": context.get("weight_version") or os.environ.get("MODEL_WEIGHTS_VERSION", "unknown"),
        "request_id": context.get("request_id"),
        "override_history": list(override_history or []),
        "timestamp": context.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }


def build_measurement_provenance(bim_data: dict[str, Any], *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    context = dict(context or {})
    scale = dict(bim_data.get("scale") or {})
    mmpp = scale.get("mm_per_pixel")
    scale_conf = scale.get("confidence")
    for collection in ("walls", "doors", "windows"):
        for row in bim_data.get(collection, []) or []:
            resolutions = dict(row.get("_manual_input_resolution") or {})
            records: dict[str, Any] = {}
            fields = {
                "walls": ("thickness", "height"),
                "doors": ("width", "height"),
                "windows": ("width", "height", "sill_height"),
            }[collection]
            for field in fields:
                value = row.get(field)
                resolution = dict(resolutions.get(field) or {})
                source = resolution.get("source")
                if not source:
                    source = "measured_from_image" if field in {"width", "thickness"} else "system_fallback"
                confidence = resolution.get("confidence")
                raw = None
                raw_source = None
                if source in {"model_property", "measured_from_image"} and mmpp and value is not None:
                    raw = float(value) / float(mmpp)
                    raw_source = "reconstructed_from_canonical_mm"
                    if confidence is None:
                        confidence = scale_conf
                records[field] = _record(
                    value=value, unit="mm", source=source, confidence=confidence,
                    raw_pixel_measurement=raw, raw_measurement_source=raw_source,
                    scale=scale, context=context,
                    override_history=resolution.get("override_history"),
                )
            row["_measurement_provenance"] = records
    provenance_context = {
        "schema_version": "1.0",
        "request_id": context.get("request_id"),
        "model_version": context.get("model_version") or os.environ.get("MODEL_VERSION", "unknown"),
        "weight_version": context.get("weight_version") or os.environ.get("MODEL_WEIGHTS_VERSION", "unknown"),
        "timestamp": context.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }
    bim_data["_provenance_context"] = provenance_context
    for collection in ("walls", "doors", "windows", "rooms", "stairs", "slabs"):
        for row in bim_data.get(collection, []) or []:
            row["_provenance_context"] = dict(provenance_context)
    return bim_data
