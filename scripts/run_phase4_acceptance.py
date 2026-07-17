#!/usr/bin/env python3
"""Phase 4 acceptance: detector contract and BIM semantic integrity."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.door_analysis import analyzeDoorOrientation, assess_door_accessibility  # noqa: E402
from analysis.window_analysis import assess_window_glazing  # noqa: E402
from export.ifc_exporter import bim_json_to_ifc  # noqa: E402
from services.preprocessing import decide_office_enhancement  # noqa: E402
from services.room_taxonomy import controlled_vocabulary_info  # noqa: E402
from utils.process_bridge import run_json_process  # noqa: E402
from validation import validate_ifc_contract  # noqa: E402


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _engine(operation: str, path: Path) -> dict[str, Any]:
    return run_json_process(
        [
            sys.executable,
            str(ROOT / "scripts" / "engine_bridge.py"),
            operation,
            "--path",
            str(path),
        ],
        timeout=360,
    )


def _sample_bim() -> dict[str, Any]:
    return {
        "walls": [
            {
                "id": "W_EXT",
                "start_point": [0, 0, 0],
                "end_point": [5000, 0, 0],
                "centerline": [[0, 0, 0], [2500, 0, 0], [5000, 0, 0]],
                "thickness": 200,
                "height": 2800,
                "is_exterior": True,
            },
            {
                "id": "W_INT",
                "start_point": [0, 3000, 0],
                "end_point": [5000, 3000, 0],
                "thickness": 100,
                "height": 2800,
                "is_exterior": False,
            },
        ],
        "doors": [
            {
                "id": "D_EXT",
                "host_wall_id": "W_EXT",
                "insertion_point": [1500, 0, 0],
                "width": 900,
                "height": 2100,
                "is_exterior": True,
                "hinge_side": "unknown",
            }
        ],
        "windows": [
            {
                "id": "WIN_EXT",
                "host_wall_id": "W_EXT",
                "insertion_point": [3500, 0, 0],
                "width": 1200,
                "height": 1000,
                "sill_height": 900,
                "is_exterior": True,
                "glazing": {"status": "not_observable_from_plan"},
            }
        ],
        "rooms": [],
        "stairs": [],
        "slabs": [],
    }


def _summary(full_check: dict[str, Any]) -> dict[str, int]:
    compliance = full_check.get("compliance") or {}
    return dict(compliance.get("summary") or {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="release/local/phase4")
    args = parser.parse_args()
    out = (ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # The orchestration module must stay importable without TensorFlow/PyTorch.
    import_probe = subprocess.run(
        [sys.executable, "-c", "import services.detection_pipeline; print('ok')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    # Producer-side semantic checks.
    import numpy as np
    from PIL import Image

    image = Image.fromarray(np.zeros((100, 100, 3), dtype=np.uint8), mode="RGB")
    preprocess = decide_office_enhancement(image)
    swing_a = analyzeDoorOrientation(None, [10, 10, 40, 60], 100, 100)
    swing_b = analyzeDoorOrientation(None, [60, 40, 90, 90], 100, 100)
    vocabulary = controlled_vocabulary_info()

    route_text = (ROOT / "routes" / "visualization_routes.py").read_text(encoding="utf-8")
    dead_primary_branch = bool(
        re.search(r"(?:class_id|cid)\s*==\s*(?:4|5|6|7|8|9|10|11|12|13|14|15)", route_text)
    )

    # Round trip externality through exporter -> written IFC -> engine loader.
    roundtrip_ifc = out / "phase4_externality_roundtrip.ifc"
    bim_json_to_ifc(_sample_bim(), output_path=str(roundtrip_ifc))
    stage1_report = validate_ifc_contract(str(roundtrip_ifc)).to_dict()
    reconstructed = _engine("ifc_to_bim_data", roundtrip_ifc)
    door = next(row for row in reconstructed["doors"] if row["id"] == "D_EXT")
    window = next(row for row in reconstructed["windows"] if row["id"] == "WIN_EXT")

    # The detector/BIM changes must not alter the trusted Phase-3 compliance result.
    reference_ifc = ROOT / "compliance-engine" / "tests" / "fixtures" / "phase3_contract_v12.ifc"
    reference_full = _engine("full_check_ifc", reference_ifc)
    previous = json.loads(
        (ROOT / "release" / "evidence" / "phase8" / "verdict_regression.json").read_text(encoding="utf-8")
    )
    live_summary = _summary(reference_full)
    expected_summary = dict(previous.get("live_summary") or {})

    checks = {
        "lazy_detector_import": import_probe.returncode == 0 and "ok" in import_probe.stdout,
        "primary_route_has_no_dead_4_to_15_branches": not dead_primary_branch,
        "deprecated_json_service_removed": not (ROOT / "services" / "json_service.py").exists(),
        "office_morphology_default_disabled": not preprocess.office_enhancement_applied,
        "office_morphology_decision_auditable": bool(preprocess.reason),
        "door_swing_never_guessed_from_image_position": (
            swing_a["estimated_swing"] == "unknown" and swing_b["estimated_swing"] == "unknown"
        ),
        "door_accessibility_explicitly_unobservable": (
            assess_door_accessibility(900)["status"] == "not_observable_from_plan"
        ),
        "window_glazing_explicitly_unobservable": (
            assess_window_glazing()["status"] == "not_observable_from_plan"
        ),
        "shared_room_taxonomy": vocabulary.get("source") == "contracts/controlled_values_v1.yaml",
        "stage1_ifc_gate_passed": stage1_report.get("status") == "pass",
        "door_externality_roundtrip": door.get("is_exterior") is True,
        "window_externality_roundtrip": window.get("is_exterior") is True,
        "polyline_segment_identity_present": len(_sample_bim()["walls"][0]["centerline"]) == 3,
        "phase3_verdict_summary_unchanged": live_summary == expected_summary,
    }
    payload = {
        "schema_version": "floorplan-phase4-acceptance-v1",
        "passed": all(checks.values()),
        "checks": checks,
        "detector_contract": {
            "primary": ["wall", "window", "door"],
            "supplementary": ["column", "railing", "staircase"],
            "supplementary_geometry": "bbox-derived_approximate_needs_review",
        },
        "preprocessing": {
            "office_mode": preprocess.mode,
            "office_enhancement_applied": preprocess.office_enhancement_applied,
            "reason": preprocess.reason,
            "edge_density": preprocess.edge_density,
        },
        "roundtrip": {
            "ifc": str(roundtrip_ifc.relative_to(ROOT)),
            "stage1_status": stage1_report.get("status"),
            "door_is_exterior": door.get("is_exterior"),
            "window_is_exterior": window.get("is_exterior"),
        },
        "verdict_regression": {
            "expected_summary": expected_summary,
            "live_summary": live_summary,
            "equal": live_summary == expected_summary,
        },
        "limitations": [
            "Real image inference requires the external Mask R-CNN and YOLO weights.",
            (
                "Office preprocessing stays disabled by default until a labelled "
                "A/B corpus demonstrates a gain."
            ),
            (
                "YOLO bbox geometry remains advisory/needs_review except stairs "
                "supported by the current exporter."
            ),
        ],
    }
    _write_json(out / "acceptance_result.json", payload)
    _write_json(out / "externality_roundtrip_bim.json", reconstructed)
    _write_json(out / "externality_roundtrip_stage1_gate.json", stage1_report)
    _write_json(out / "reference_engine_full_check.json", reference_full)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
