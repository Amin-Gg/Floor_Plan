#!/usr/bin/env python3
"""Run sealed detectors on a Phase-8 manifest and write versioned predictions.

This command requires the external Mask R-CNN weights and, when enabled, YOLO
weights. It does not calculate metrics; use run_phase8_evaluation.py afterward.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset import load_dataset  # noqa: E402
from services.detection_pipeline import PRIMARY_CLASSES, run_detectors  # noqa: E402
from services.preprocessing import decide_office_enhancement  # noqa: E402


def file_sha(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_mask(mask: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="Defaults to <dataset-root>/predictions/<variant>")
    parser.add_argument("--scored-manifest", type=Path, help="Write a manifest that references the generated predictions")
    parser.add_argument("--variant", choices=["raw", "morphology_auto", "morphology_force"], default="raw")
    parser.add_argument("--scale-from-ground-truth", action="store_true", help="Testing only; never use for blind scale evaluation")
    parser.add_argument("--scale-predictions", type=Path, help="JSON object mapping sample_id to predicted mm_per_pixel")
    args = parser.parse_args()

    # Explicit initialisation keeps OpenAPI/import paths lightweight.
    from models.mask_rcnn_model import get_weights_path, initialize_model
    from models.yolo_detector import _resolve_weights_path as yolo_weights_path, initialize_yolo

    initialize_model()
    initialize_yolo()
    dataset = load_dataset(args.dataset)
    scale_predictions = json.loads(args.scale_predictions.read_text(encoding="utf-8")) if args.scale_predictions else {}
    if not isinstance(scale_predictions, dict):
        raise ValueError("--scale-predictions must contain a JSON object")
    dataset_root = dataset.manifest_path.parent.resolve()
    out = (args.out or (dataset_root / "predictions" / args.variant)).resolve()
    try:
        out.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError("--out must stay inside the dataset root so prediction paths remain portable") from exc
    out.mkdir(parents=True, exist_ok=True)
    model_weight = Path(get_weights_path()) if get_weights_path() else None
    yolo_weight = Path(yolo_weights_path())

    for sample in dataset.samples:
        if sample.image_path is None:
            raise ValueError(f"Sample {sample.sample_id} has no image_path")
        pil = Image.open(sample.image_path).convert("RGB")
        decision = decide_office_enhancement(
            pil,
            mode={"raw": "disabled", "morphology_auto": "auto", "morphology_force": "force"}[args.variant],
        )
        image_rgb = np.asarray(pil)
        image = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
        if decision.office_enhancement_applied:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            kernel = np.ones((2, 2), np.uint8)
            enhanced = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
            image = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
        bundle = run_detectors(image)
        primary = bundle["primary"]
        instances = []
        masks_dir = out / "masks" / sample.sample_id
        for index, class_id in enumerate(primary.get("class_ids", [])):
            cid = int(class_id)
            if cid == 0:
                continue
            y1, x1, y2, x2 = [float(v) for v in primary["rois"][index]]
            mask_rel = None
            masks = primary.get("masks")
            if masks is not None and getattr(masks, "ndim", 0) == 3 and index < masks.shape[2]:
                mask_path = masks_dir / f"primary_{index}.png"
                save_mask(masks[:, :, index], mask_path)
                mask_rel = os.path.relpath(mask_path, out)
            row = {
                "id": f"primary_{index}",
                "class_name": PRIMARY_CLASSES[cid],
                "confidence": float(primary["scores"][index]),
                "bbox_xyxy": [x1, y1, x2, y2],
                "attributes": {"source": "mask_rcnn"},
            }
            if mask_rel:
                row["mask"] = {"type": "png", "path": mask_rel}
            instances.append(row)
        for index, row in enumerate(bundle.get("supplementary", [])):
            y1, x1, y2, x2 = row["bbox"]
            name = str(row.get("element_type", "")).lower()
            class_name = {"stairs": "stairs", "column": "column", "railing": "railing"}.get(name)
            if class_name:
                instances.append({
                    "id": f"yolo_{index}",
                    "class_name": class_name,
                    "confidence": float(row["confidence"]),
                    "bbox_xyxy": [float(x1), float(y1), float(x2), float(y2)],
                    "attributes": {"source": "yolo", "geometry_quality": "approximate"},
                })
        prediction = {
            "schema_version": "1.0",
            "sample_id": sample.sample_id,
            "model": {
                "variant": args.variant,
                "mask_rcnn_weights_sha256": file_sha(model_weight),
                "yolo_weights_sha256": file_sha(yolo_weight),
                "detector_status": bundle["detector_status"],
                "preprocessing": decision.to_dict() if hasattr(decision, "to_dict") else decision.__dict__,
            },
            "instances": instances,
            "scale": (
                {"mm_per_pixel": float(scale_predictions[sample.sample_id]), "source": "external_scale_estimator"}
                if sample.sample_id in scale_predictions
                else {"mm_per_pixel": sample.mm_per_pixel, "source": "ground_truth_testing_only"}
                if args.scale_from_ground_truth and sample.mm_per_pixel
                else {}
            ),
            "verdicts": {},
        }
        (out / f"{sample.sample_id}.json").write_text(json.dumps(prediction, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    original = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
    by_id = {row["sample_id"]: row for row in original["samples"]}
    for sample in dataset.samples:
        row = by_id[sample.sample_id]
        row.setdefault("predictions", {})[args.variant] = str((out / f"{sample.sample_id}.json").relative_to(dataset_root))
        row.setdefault("prediction_sha256", {})[args.variant] = file_sha(out / f"{sample.sample_id}.json")
    scored_manifest = (args.scored_manifest or (dataset_root / f"dataset.{args.variant}.json")).resolve()
    try:
        scored_manifest.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError("--scored-manifest must stay inside the dataset root") from exc
    scored_manifest.write_text(json.dumps(original, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"samples": len(dataset.samples), "variant": args.variant, "out": str(out), "scored_manifest": str(scored_manifest)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
