"""Rejection of the removed flat ``building_params`` public input.

Phase 9 removed the flat ``building_params`` input in favour of Manual
Inputs v1.0. The independent post-release review found the removed path
*half-alive*: a flat block embedded inside ``bim_data`` was neither
rejected nor inert — keys the deterministic agents consume (for example
``ceiling_height_mm``) still reached verdict resolution while bypassing
every Manual-Inputs-v1 validation (types, ranges, cross-field rules,
provenance). Keys nothing consumes were silently ignored. Both behaviours
violate the remediation hard rules (no silent behaviour change, no
provenance-free values driving verdicts).

This module is the single authority that closes the public input path.
Empty/marker-only blocks are tolerated solely for compatibility with old raw
payloads; they carry no verdict-driving value.  A value-bearing enriched
``bim_data`` seam produced after Manual Inputs resolution is **output-only**
and must not be submitted as raw public input. Reuse a typed ``BuildingModel``
in-process, or submit the original raw ``bim_data`` together with Manual
Inputs v1 again. Any operator-style key is rejected loudly.
"""
from __future__ import annotations

from typing import Any, Mapping

from .parser import ManualInputsError

LEGACY_BUILDING_PARAMS_MESSAGE = (
    "building_params was removed in Phase 9; use manual_inputs "
    "schema_version 1.0"
)

_INTERNAL_MARKER_KEYS = {"_provided"}


def legacy_building_params_keys(bim_data: Mapping[str, Any]) -> list[str]:
    """Return operator-style keys of a removed flat block, if any.

    An absent block, an empty block, or a block containing only the
    internal ``_provided`` round-trip marker returns ``[]``.
    A non-mapping value is reported as a single pseudo-key so the caller
    still rejects it with a useful message.
    """
    block = bim_data.get("building_params")
    if block is None:
        return []
    if not isinstance(block, Mapping):
        return [f"<building_params must be an object, got {type(block).__name__}>"]
    return sorted(str(key) for key in block if key not in _INTERNAL_MARKER_KEYS)


def reject_legacy_building_params(bim_data: Mapping[str, Any]) -> None:
    """Raise ``ManualInputsError`` when input bim_data carries the removed field.

    Called at every public ingestion boundary of raw ``bim_data`` (HTTP API
    and the unified pipeline). Value-bearing internal seams produced by the
    pipeline are output-only and intentionally fail if resubmitted as raw
    input; only empty/marker-only legacy blocks are tolerated.
    """
    offending = legacy_building_params_keys(bim_data)
    if offending:
        raise ManualInputsError(
            f"{LEGACY_BUILDING_PARAMS_MESSAGE} (offending building_params "
            f"keys: {', '.join(offending)})"
        )
