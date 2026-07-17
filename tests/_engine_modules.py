"""Collision-safe Stage-1 ↔ Stage-2 test bridge.

The nested compliance engine and Stage 1 both expose top-level packages named
``services`` and ``validation``. Loading engine modules into the Stage-1 pytest
process can therefore make test order change import resolution. The bridge uses
an isolated Python subprocess and exchanges JSON only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from utils.process_bridge import run_json_process

_ROOT = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = _ROOT / "compliance-engine"
_BRIDGE = _ROOT / "scripts" / "engine_bridge.py"


def _ensure_layout() -> None:
    required = (
        _ENGINE_ROOT / "ingest" / "ifc_to_bim_data.py",
        _ENGINE_ROOT / "ingest" / "review_prepass.py",
        _BRIDGE,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Stage-2 integration dependency is incomplete. Missing: " + ", ".join(missing)
        )


def _call(operation: str, payload: Any = None, *extra: str) -> Any:
    _ensure_layout()
    command = [sys.executable, str(_BRIDGE), operation, *extra]
    return run_json_process(command, payload=payload, timeout=120)


def ifc_to_bim_data(path: str) -> dict[str, Any]:
    return _call("ifc_to_bim_data", None, "--path", str(path))


def precheck_ifc(path: str) -> dict[str, Any]:
    return _call("precheck_ifc", None, "--path", str(path))


def precheck_ifcs(paths: list[str]) -> list[dict[str, Any]]:
    resolved = [str(Path(path).resolve()) for path in paths]
    return _call("batch_precheck_ifc", resolved)


def full_check_ifc(path: str) -> dict[str, Any]:
    return _call("full_check_ifc", None, "--path", str(path))


def apply_review_prepass(bim_data: dict[str, Any], threshold: float | None = None) -> dict[str, Any]:
    extra: tuple[str, ...] = () if threshold is None else ("--threshold", str(threshold))
    updated = _call("apply_review_prepass", bim_data, *extra)
    bim_data.clear()
    bim_data.update(updated)
    return bim_data


def downgrade_flagged_findings(result: dict[str, Any], bim_data: dict[str, Any]) -> dict[str, Any]:
    updated = _call("downgrade_flagged_findings", {"result": result, "bim_data": bim_data})
    result.clear()
    result.update(updated)
    return result
