#!/usr/bin/env python3
"""Phase-8 acceptance for evaluation correctness and claim safety."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset import load_dataset  # noqa: E402
from evaluation.metrics import compare_variants, evaluate_dataset  # noqa: E402
from evaluation.policy import apply_policy, load_policy  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "release" / "local" / "ml-evaluation")
    args = parser.parse_args()
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    generation = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_phase8_reference_dataset.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    dataset_path = ROOT / "examples" / "phase8" / "reference_dataset" / "dataset.json"
    dataset = load_dataset(dataset_path)
    baseline = evaluate_dataset(dataset, variant="baseline")
    candidate = evaluate_dataset(dataset, variant="candidate")
    policy = load_policy(ROOT / "config" / "phase8_evaluation_policy.json")
    baseline_gate = apply_policy(baseline, policy)
    candidate_gate = apply_policy(candidate, policy)
    comparison = compare_variants({"baseline": baseline, "candidate": candidate}, "baseline")

    inference_help = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_phase8_inference.py"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    route_text = (ROOT / "routes" / "accuracy_routes.py").read_text(encoding="utf-8")
    service_text = (ROOT / "services" / "accuracy_service.py").read_text(encoding="utf-8")
    required_artifacts = [
        ROOT / "weights" / "maskrcnn_15_epochs.h5",
        ROOT / "weights" / "yolo_best.pt",
    ]
    missing_artifacts = [str(path.relative_to(ROOT)) for path in required_artifacts if not path.is_file()]
    real_dataset_env = None
    import os
    if os.getenv("PHASE8_REAL_DATASET"):
        real_dataset_env = os.getenv("PHASE8_REAL_DATASET")

    checks = {
        "reference_dataset_generation": generation.returncode == 0 and dataset_path.is_file(),
        "reference_dataset_is_non_empirical": dataset.empirical_claims_allowed is False,
        "baseline_false_pass_detected": baseline["verdict_impact"]["critical_false_pass"] == 1,
        "candidate_has_no_false_pass": candidate["verdict_impact"]["critical_false_pass"] == 0,
        "candidate_improves_macro_f1": candidate["summary"]["macro_f1"] > baseline["summary"]["macro_f1"],
        "candidate_improves_map": candidate["summary"]["map_50_95"] > baseline["summary"]["map_50_95"],
        "candidate_improves_calibration": candidate["calibration_overall"]["ece"] < baseline["calibration_overall"]["ece"],
        "candidate_improves_scale": candidate["scale"]["relative_error"]["mean"] < baseline["scale"]["relative_error"]["mean"],
        "synthetic_baseline_gate_blocked": baseline_gate["passed"] is False,
        "synthetic_candidate_gate_blocked": candidate_gate["passed"] is False,
        "inference_cli_imports_without_ml_runtime": inference_help.returncode == 0,
        "legacy_route_explicitly_not_accuracy": "not ground-truth accuracy" in route_text,
        "confidence_report_has_no_accuracy_claim": '"accuracy_claim": False' in service_text,
        "dataset_schema_present": (ROOT / "contracts" / "phase8" / "ml_evaluation_dataset_v1.schema.json").is_file(),
        "prediction_schema_present": (ROOT / "contracts" / "phase8" / "ml_evaluation_predictions_v1.schema.json").is_file(),
        "annotation_schema_present": (ROOT / "contracts" / "phase8" / "ml_evaluation_annotations_v1.schema.json").is_file(),
    }
    infrastructure_passed = all(checks.values())
    empirical_status = {
        "status": "blocked_external_evidence" if missing_artifacts or not real_dataset_env else "ready_to_run",
        "completed": False,
        "real_dataset_manifest": real_dataset_env,
        "missing_artifacts": missing_artifacts,
        "reason": (
            "Real model weights and an adjudicated holdout dataset were not available in the audit environment. "
            "Synthetic metrics validate evaluator behavior only."
        ),
        "release_gate": "blocked",
    }
    result = {
        "schema_version": "phase8-acceptance-v1",
        "passed": infrastructure_passed,
        "checks": checks,
        "reference_metrics": {"baseline": baseline["summary"], "candidate": candidate["summary"]},
        "reference_verdict_impact": {"baseline": baseline["verdict_impact"], "candidate": candidate["verdict_impact"]},
        "comparison": comparison,
        "empirical_evaluation": empirical_status,
        "claim_policy": {
            "synthetic_metrics_publishable_as_model_accuracy": False,
            "human_verified_holdout_required": True,
            "critical_false_pass_allowed": 0,
        },
    }
    write_json(out / "reference_baseline.metrics.json", baseline)
    write_json(out / "reference_candidate.metrics.json", candidate)
    write_json(out / "reference_baseline.gate.json", baseline_gate)
    write_json(out / "reference_candidate.gate.json", candidate_gate)
    write_json(out / "acceptance_result.json", result)
    write_json(out / "empirical_evaluation_status.json", empirical_status)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if infrastructure_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
