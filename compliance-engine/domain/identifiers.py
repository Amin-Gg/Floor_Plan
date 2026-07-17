"""Deterministic model and element identity helpers.

Identity is intentionally independent from display names. IFC GlobalId,
source/detector ID and engine internal ID are separate values.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SIMSYS_ID_NAMESPACE = uuid.UUID("d11d08ee-4a63-5e55-9e24-66b54d1f01a5")


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def canonical_json(value: Any) -> str:
    """Canonical JSON used for non-file model fingerprints."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_file(path: str | Path) -> str:
    return fingerprint_bytes(Path(path).read_bytes())


def fingerprint_data(value: Any) -> str:
    return fingerprint_bytes(canonical_json(value).encode("utf-8"))


def _quantize(value: Any, quantum: float = 1.0) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return ""
    if quantum <= 0:
        quantum = 1.0
    return str(int(round(number / quantum)))


def stable_geometry_key(
    element_type: str,
    *,
    storey_id: Optional[str] = None,
    point: Optional[Sequence[Any]] = None,
    start: Optional[Sequence[Any]] = None,
    end: Optional[Sequence[Any]] = None,
    quantum_mm: float = 1.0,
) -> str:
    """Build ADR-001's rounded fallback geometry key."""
    chunks = [element_type or "element", storey_id or ""]
    for label, seq in (("p", point), ("a", start), ("b", end)):
        if seq is None:
            continue
        chunks.append(label + ":" + ",".join(_quantize(v, quantum_mm) for v in seq))
    return "|".join(chunks)


@dataclass(frozen=True)
class ElementIdentity:
    internal_id: str
    ifc_guid: Optional[str] = None
    source_id: Optional[str] = None
    model_name: Optional[str] = None
    used_geometry_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "internal_id": self.internal_id,
            "ifc_guid": self.ifc_guid,
            "source_id": self.source_id,
            "model_name": self.model_name,
            "used_geometry_fallback": self.used_geometry_fallback,
        }


def build_element_identity(
    *,
    model_fingerprint: str,
    element_type: str,
    source_type: str,
    ifc_guid: Optional[str] = None,
    source_id: Optional[str] = None,
    model_name: Optional[str] = None,
    geometry_key: Optional[str] = None,
    existing_internal_id: Optional[str] = None,
) -> ElementIdentity:
    """Create a stable ElementIdentity using the ADR-001 precedence."""
    if existing_internal_id:
        internal_id = str(existing_internal_id)
        fallback = False
    elif ifc_guid:
        internal_id = str(uuid.uuid5(SIMSYS_ID_NAMESPACE, f"{model_fingerprint}:{ifc_guid}"))
        fallback = False
    elif source_id:
        internal_id = str(uuid.uuid5(
            SIMSYS_ID_NAMESPACE,
            f"{model_fingerprint}:{source_type}:{source_id}",
        ))
        fallback = False
    else:
        key = geometry_key or f"{element_type}:unidentified"
        internal_id = str(uuid.uuid5(
            SIMSYS_ID_NAMESPACE,
            f"{model_fingerprint}:{element_type}:{key}",
        ))
        fallback = True
    return ElementIdentity(
        internal_id=internal_id,
        ifc_guid=str(ifc_guid) if ifc_guid else None,
        source_id=str(source_id) if source_id else None,
        model_name=model_name,
        used_geometry_fallback=fallback,
    )


def identity_from_bim_data(
    row: Mapping[str, Any],
    *,
    model_fingerprint: str,
    element_type: str,
    source_type: str,
    model_name: Optional[str] = None,
    geometry_key: Optional[str] = None,
) -> ElementIdentity:
    embedded = row.get("_identity") if isinstance(row.get("_identity"), Mapping) else {}
    provenance = row.get("_provenance") if isinstance(row.get("_provenance"), Mapping) else {}
    ifc_guid = row.get("ifc_guid") or embedded.get("ifc_guid")
    if "source_id" in row or "source_id" in embedded:
        source_id = row.get("source_id") if "source_id" in row else embedded.get("source_id")
    elif provenance.get("id"):
        source_id = provenance.get("id")
    elif ifc_guid and row.get("id") == ifc_guid:
        source_id = None
    else:
        source_id = row.get("id")
    return build_element_identity(
        model_fingerprint=model_fingerprint,
        element_type=element_type,
        source_type=source_type,
        ifc_guid=ifc_guid,
        source_id=source_id,
        model_name=row.get("model_name") or embedded.get("model_name") or model_name,
        geometry_key=geometry_key,
        existing_internal_id=row.get("internal_id") or embedded.get("internal_id"),
    )
