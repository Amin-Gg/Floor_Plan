#!/usr/bin/env python3
"""Evaluate one or more precomputed detector variants against ground truth."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset import DatasetContractError, load_dataset  # noqa: E402
from evaluation.metrics import EvaluationConfig, compare_variants, evaluate_dataset  # noqa: E402
from evaluation.policy import apply_policy, load_policy  # noqa: E402
from evaluation.reporting import render_markdown  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", action="append", dest="variants")
    parser.add_argument("--baseline-variant")
    parser.add_argument("--policy", type=Path, default=ROOT / "config" / "phase8_evaluation_policy.json")
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--no-hash-verification", action="store_true")
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset, verify_hashes=not args.no_hash_verification)
        variants = args.variants or list(dataset.prediction_variants())
        if not variants:
            raise DatasetContractError("No prediction variants were selected")
        policy = load_policy(args.policy)
        config = EvaluationConfig(confidence_threshold=args.confidence, operating_iou=args.iou)
        args.out.mkdir(parents=True, exist_ok=True)
        reports = {}
        gates = {}
        for variant in variants:
            report = evaluate_dataset(dataset, variant=variant, config=config)
            gate = apply_policy(report, policy)
            reports[variant] = report
            gates[variant] = gate
            write_json(args.out / f"{variant}.metrics.json", report)
            write_json(args.out / f"{variant}.gate.json", gate)
            (args.out / f"{variant}.report.md").write_text(render_markdown(report, gate), encoding="utf-8")
        baseline = args.baseline_variant or variants[0]
        comparison = compare_variants(reports, baseline) if len(reports) > 1 else {"baseline": baseline, "comparisons": {}}
        write_json(args.out / "variant_comparison.json", comparison)
        summary = {
            "schema_version": "1.0",
            "dataset_id": dataset.dataset_id,
            "split": dataset.split,
            "empirical_claims_allowed": dataset.empirical_claims_allowed,
            "variants": {
                name: {
                    "summary": report["summary"],
                    "gate_passed": gates[name]["passed"],
                    "critical_false_pass": report["verdict_impact"]["critical_false_pass"],
                }
                for name, report in reports.items()
            },
            "comparison": comparison,
        }
        write_json(args.out / "evaluation_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (DatasetContractError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
