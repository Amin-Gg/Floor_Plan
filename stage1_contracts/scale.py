"""Evidence-based pixel-to-millimetre scale contract."""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .manual_inputs import canonical_json_sha256


class ScaleEvidenceError(ValueError):
    pass


SOURCES = {
    "user_dimension": 0.98,
    "recognized_scale_bar": 0.90,
    "recognized_dimension_text": 0.82,
    "document_metadata": 0.65,
    "default_unverified": 0.15,
}


def _finite(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ScaleEvidenceError(f"{path} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScaleEvidenceError(f"{path} must be a finite number") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ScaleEvidenceError(f"{path} must be {'positive and ' if positive else ''}finite")
    return number


def assess_scale_evidence(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "" or raw == {}:
        raw = {"schema_version": "1.0", "mm_per_pixel": 1.0,
               "source": "default_unverified", "evidence": []}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScaleEvidenceError(
                f"scale_evidence is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from exc
    if not isinstance(raw, Mapping):
        raise ScaleEvidenceError("scale_evidence must be a JSON object")
    data = dict(raw)
    allowed = {"schema_version", "mm_per_pixel", "source", "evidence"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ScaleEvidenceError(f"Unknown key(s) in scale_evidence: {unknown}")
    if str(data.get("schema_version", "1.0")) != "1.0":
        raise ScaleEvidenceError("Unsupported scale_evidence schema_version")
    mmpp = _finite(data.get("mm_per_pixel", 1.0), "scale_evidence.mm_per_pixel", positive=True)
    if mmpp > 100:
        raise ScaleEvidenceError("scale_evidence.mm_per_pixel must be <= 100")
    source = str(data.get("source", "default_unverified"))
    if source not in SOURCES:
        raise ScaleEvidenceError(f"Unknown scale source {source!r}; supported: {sorted(SOURCES)}")
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        raise ScaleEvidenceError("scale_evidence.evidence must be an array")

    clean: list[dict[str, Any]] = []
    evidence_scores: list[float] = []
    consistency_errors: list[float] = []
    ids: set[str] = set()
    allowed_ev = {"id", "kind", "raw_pixel_measurement", "real_world_length_mm",
                  "extracted_text", "metadata_value", "evidence_confidence",
                  "model_version", "weight_version"}
    for index, row in enumerate(evidence):
        if not isinstance(row, Mapping):
            raise ScaleEvidenceError(f"scale_evidence.evidence[{index}] must be an object")
        item = dict(row)
        unknown = sorted(set(item) - allowed_ev)
        if unknown:
            raise ScaleEvidenceError(f"Unknown key(s) in scale_evidence.evidence[{index}]: {unknown}")
        eid = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        if not eid or not kind:
            raise ScaleEvidenceError(f"scale_evidence.evidence[{index}] requires non-empty id and kind")
        if eid in ids:
            raise ScaleEvidenceError(f"Duplicate scale evidence id {eid!r}")
        ids.add(eid)
        cleaned = {"id": eid, "kind": kind}
        for key in ("raw_pixel_measurement", "real_world_length_mm"):
            if item.get(key) is not None:
                cleaned[key] = _finite(item[key], f"scale_evidence.evidence[{index}].{key}", positive=True)
        for key in ("extracted_text", "metadata_value", "model_version", "weight_version"):
            if item.get(key) is not None:
                cleaned[key] = str(item[key])
        score = 1.0
        if item.get("evidence_confidence") is not None:
            score = _finite(item["evidence_confidence"], f"scale_evidence.evidence[{index}].evidence_confidence")
            if not 0 <= score <= 1:
                raise ScaleEvidenceError("evidence_confidence must be between 0 and 1")
        cleaned["evidence_confidence"] = score
        evidence_scores.append(score)
        if "raw_pixel_measurement" in cleaned and "real_world_length_mm" in cleaned:
            observed = cleaned["real_world_length_mm"] / cleaned["raw_pixel_measurement"]
            consistency_errors.append(abs(observed - mmpp) / mmpp)
            cleaned["observed_mm_per_pixel"] = observed
        clean.append(cleaned)

    if source == "default_unverified":
        if clean:
            raise ScaleEvidenceError("default_unverified must not claim scale evidence")
    elif not clean:
        raise ScaleEvidenceError(f"scale source {source!r} requires at least one evidence item")

    if source in {"user_dimension", "recognized_scale_bar", "recognized_dimension_text"}:
        if not any("raw_pixel_measurement" in row and "real_world_length_mm" in row for row in clean):
            raise ScaleEvidenceError(f"scale source {source!r} requires raw_pixel_measurement and real_world_length_mm")
    if consistency_errors and max(consistency_errors) > 0.03:
        raise ScaleEvidenceError(
            f"scale evidence conflicts with mm_per_pixel; maximum relative error is {max(consistency_errors):.3f}"
        )

    evidence_factor = min(evidence_scores) if evidence_scores else 1.0
    consistency_factor = max(0.0, 1.0 - (max(consistency_errors) if consistency_errors else 0.0) * 5.0)
    confidence = round(SOURCES[source] * evidence_factor * consistency_factor, 3)
    reasons: list[str] = []
    if source == "default_unverified":
        reasons.append("no scale evidence was supplied; dimensional values are unverified")
    if confidence < 0.75:
        reasons.append("computed scale confidence is below the trusted dimensional threshold")
    result = {
        "schema_version": "1.0",
        "mm_per_pixel": mmpp,
        "source": source,
        "confidence": confidence,
        "needs_review": confidence < 0.75,
        "evidence": clean,
        "evidence_ids": [row["id"] for row in clean],
        "reasons": reasons,
    }
    result["evidence_sha256"] = canonical_json_sha256({
        "schema_version": "1.0", "mm_per_pixel": mmpp,
        "source": source, "evidence": clean,
    })
    return result
