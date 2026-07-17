#!/usr/bin/env python3
"""Generate a tiny deterministic synthetic dataset for evaluator contract tests.

This dataset is NOT evidence of real model quality. It exists to validate metric
math, A/B comparison, calibration, scale, slice, and verdict-impact plumbing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "phase8" / "reference_dataset"
CLASSES = ["wall", "window", "door", "stairs", "column", "railing"]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def polygon(box: list[int]) -> dict:
    x1, y1, x2, y2 = box
    return {"type": "polygon", "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]}


def instance(identifier: str, class_name: str, box: list[int], *, angle: float = 0.0, centerline=None) -> dict:
    attrs = {"orientation_deg": angle}
    if centerline is not None:
        attrs["centerline"] = centerline
    return {"id": identifier, "class_name": class_name, "bbox_xyxy": box, "mask": polygon(box), "attributes": attrs}


def prediction(identifier: str, class_name: str, box: list[int], confidence: float, *, angle: float = 0.0, centerline=None, mask=True) -> dict:
    row = {"id": identifier, "class_name": class_name, "confidence": confidence, "bbox_xyxy": box, "attributes": {"orientation_deg": angle}}
    if centerline is not None:
        row["attributes"]["centerline"] = centerline
    if mask:
        row["mask"] = polygon(box)
    return row


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    samples = []
    specs = [
        {
            "id": "synthetic_residential_001",
            "slices": {"plan_style": "residential", "scan_quality": "clean", "language": "fa"},
            "gt": [
                instance("gt_wall", "wall", [10, 10, 190, 30], centerline=[[10, 20], [190, 20]]),
                instance("gt_window", "window", [70, 10, 110, 30]),
                instance("gt_door", "door", [135, 10, 165, 45], angle=90),
                instance("gt_column", "column", [30, 70, 50, 90]),
            ],
            "baseline": [
                prediction("p_wall", "wall", [12, 12, 186, 31], 0.92, centerline=[[12, 22], [186, 22]]),
                prediction("p_window", "window", [80, 10, 112, 29], 0.62),
                prediction("p_door", "door", [140, 12, 170, 43], 0.82, angle=45),
                prediction("p_false", "column", [120, 80, 145, 105], 0.70),
            ],
            "candidate": [
                prediction("p_wall", "wall", [10, 10, 190, 30], 0.96, centerline=[[10, 20], [190, 20]]),
                prediction("p_window", "window", [70, 10, 110, 30], 0.91),
                prediction("p_door", "door", [135, 10, 165, 45], 0.93, angle=90),
                prediction("p_column", "column", [30, 70, 50, 90], 0.88),
            ],
            "gt_verdicts": {"door_width": "PASS", "window_width": "PASS", "column_clearance": "FAIL"},
            "baseline_verdicts": {"door_width": "PASS", "window_width": "NEEDS_REVIEW", "column_clearance": "PASS"},
            "candidate_verdicts": {"door_width": "PASS", "window_width": "PASS", "column_clearance": "FAIL"},
            "scale_gt": 5.0,
            "scale_baseline": 5.4,
            "scale_candidate": 5.02,
        },
        {
            "id": "synthetic_office_002",
            "slices": {"plan_style": "office", "scan_quality": "noisy", "language": "en"},
            "gt": [
                instance("gt_wall", "wall", [10, 150, 190, 170], centerline=[[10, 160], [190, 160]]),
                instance("gt_stairs", "stairs", [20, 40, 80, 110]),
                instance("gt_railing", "railing", [90, 45, 100, 120], angle=90),
                instance("gt_door", "door", [130, 135, 160, 175], angle=90),
            ],
            "baseline": [
                prediction("p_wall", "wall", [15, 152, 185, 171], 0.88, centerline=[[15, 162], [185, 162]]),
                prediction("p_stairs", "stairs", [26, 46, 75, 105], 0.58, mask=False),
                prediction("p_door", "door", [132, 137, 159, 172], 0.55, angle=0),
            ],
            "candidate": [
                prediction("p_wall", "wall", [10, 150, 190, 170], 0.95, centerline=[[10, 160], [190, 160]]),
                prediction("p_stairs", "stairs", [20, 40, 80, 110], 0.87, mask=False),
                prediction("p_railing", "railing", [90, 45, 100, 120], 0.84, angle=90, mask=False),
                prediction("p_door", "door", [130, 135, 160, 175], 0.90, angle=90),
            ],
            "gt_verdicts": {"stair_width": "FAIL", "railing_presence": "PASS", "door_width": "PASS"},
            "baseline_verdicts": {"stair_width": "NEEDS_REVIEW", "railing_presence": "NOT_EVALUATED", "door_width": "NEEDS_REVIEW"},
            "candidate_verdicts": {"stair_width": "FAIL", "railing_presence": "PASS", "door_width": "PASS"},
            "scale_gt": 4.0,
            "scale_baseline": 4.3,
            "scale_candidate": 4.01,
        },
    ]
    for spec in specs:
        sample_id = spec["id"]
        image_path = OUT / "images" / f"{sample_id}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(image)
        for row in spec["gt"]:
            draw.rectangle(row["bbox_xyxy"], outline="black", width=2)
        image.save(image_path)
        annotation_path = OUT / "annotations" / f"{sample_id}.json"
        write_json(annotation_path, {"schema_version": "1.0", "sample_id": sample_id, "instances": spec["gt"], "verdicts": spec["gt_verdicts"]})
        prediction_paths = {}
        for variant in ("baseline", "candidate"):
            prediction_path = OUT / "predictions" / variant / f"{sample_id}.json"
            write_json(prediction_path, {
                "schema_version": "1.0",
                "sample_id": sample_id,
                "model": {"model_id": f"synthetic-{variant}", "variant": variant, "purpose": "contract_test_only"},
                "instances": spec[variant],
                "scale": {"mm_per_pixel": spec[f"scale_{variant}"], "source": "synthetic"},
                "verdicts": spec[f"{variant}_verdicts"],
            })
            prediction_paths[variant] = str(prediction_path.relative_to(OUT))
        samples.append({
            "sample_id": sample_id,
            "width": 200,
            "height": 200,
            "image_path": str(image_path.relative_to(OUT)),
            "image_sha256": sha(image_path),
            "annotations_path": str(annotation_path.relative_to(OUT)),
            "annotations_sha256": sha(annotation_path),
            "prediction_sha256": {variant: sha(OUT / prediction_paths[variant]) for variant in prediction_paths},
            "label_status": "synthetic_contract_test",
            "mm_per_pixel": spec["scale_gt"],
            "slices": spec["slices"],
            "predictions": prediction_paths,
        })
    write_json(OUT / "dataset.json", {
        "schema_version": "1.0",
        "dataset_id": "phase8-synthetic-reference-v1",
        "split": "synthetic",
        "label_status": "synthetic_contract_test",
        "classes": CLASSES,
        "purpose": "metric and contract acceptance only; never cite as real model accuracy",
        "samples": samples,
    })


if __name__ == "__main__":
    build()
    print(OUT / "dataset.json")
