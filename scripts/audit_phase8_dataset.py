#!/usr/bin/env python3
"""Audit label quality, class/slice coverage, duplicates, and release readiness."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset import DatasetContractError, load_dataset  # noqa: E402


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", type=Path, action="append", default=[], help="Other split manifests for leakage checks")
    args = parser.parse_args()
    try:
        dataset = load_dataset(args.dataset)
    except DatasetContractError as exc:
        print(json.dumps({"passed": False, "error": str(exc)}), file=sys.stderr)
        return 2
    class_counts = Counter()
    slices: dict[str, Counter[str]] = defaultdict(Counter)
    label_status = Counter()
    images = []
    for sample in dataset.samples:
        label_status[sample.label_status] += 1
        class_counts.update(row["class_name"] for row in sample.annotations["instances"])
        for key, value in sample.slices.items():
            slices[key][value] += 1
        if sample.image_path:
            images.append({"sample_id": sample.sample_id, "path": str(sample.image_path), "sha256": sha(sample.image_path)})
    own_hashes = {row["sha256"]: row["sample_id"] for row in images}
    leakage = []
    for compare_path in args.compare:
        other = load_dataset(compare_path)
        for sample in other.samples:
            if sample.image_path:
                digest = sha(sample.image_path)
                if digest in own_hashes:
                    leakage.append({
                        "sha256": digest,
                        "current_sample": own_hashes[digest],
                        "other_dataset": other.dataset_id,
                        "other_split": other.split,
                        "other_sample": sample.sample_id,
                    })
    payload = {
        "schema_version": "1.0",
        "passed": not leakage,
        "dataset_id": dataset.dataset_id,
        "split": dataset.split,
        "samples": len(dataset.samples),
        "instances": sum(class_counts.values()),
        "class_counts": dict(sorted(class_counts.items())),
        "label_status": dict(sorted(label_status.items())),
        "slice_coverage": {key: dict(sorted(value.items())) for key, value in sorted(slices.items())},
        "empirical_claims_allowed": dataset.empirical_claims_allowed,
        "prediction_variants": list(dataset.prediction_variants()),
        "images": images,
        "cross_split_leakage": leakage,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
