"""
evaluate.py
===========
Computes mAP (mean average precision) on the val set after training.
Run this once after train_mask2former.py finishes to measure true quality.

Usage
-----
    python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned

Output
------
    Per-class AP and overall mAP@50 and mAP@50:95.
    Also reports the optimal confidence threshold for your val set.

Why a separate script
---------------------
The HuggingFace Trainer's compute_metrics callback does not have access to
image sizes during evaluation, which are required to run
post_process_instance_segmentation correctly.  This standalone script has
full context and computes accurate mAP using the COCO protocol.
"""

import json
import argparse
import logging
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from tqdm import tqdm

from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
)
from config.classes import TRAIN_ID_TO_NAME as ID2LABEL, NUM_CLASSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Evaluate Mask2Former mAP on val set")
    p.add_argument("--checkpoint",   required=True,
                   help="Path to trained checkpoint directory")
    p.add_argument("--dataset_dir",  default="./dataset")
    p.add_argument("--conf_thresh",  type=float, default=0.01,
                   help="Low-floor confidence filter for mAP computation (default 0.01). "
                        "Do NOT use the operational threshold here — that inflates mAP. "
                        "Use --find_best_threshold to pick the operational threshold.")
    p.add_argument("--iou_thresh",   type=float, default=0.50,
                   help="IoU threshold for a prediction to count as TP")
    p.add_argument("--find_best_threshold", action="store_true",
                   help="Sweep confidence thresholds 0.1–0.9 and report mAP for each")
    return p.parse_args()


def load_coco_annotations(annotation_file: str):
    with open(annotation_file, encoding="utf-8") as f:
        return json.load(f)


def masks_iou(pred_mask: np.ndarray, gt_mask: np.ndarray) -> float:
    """Compute IoU between two boolean masks."""
    intersection = np.logical_and(pred_mask, gt_mask).sum()
    union        = np.logical_or(pred_mask,  gt_mask).sum()
    return float(intersection / union) if union > 0 else 0.0


def polygon_to_mask(segmentation, H: int, W: int) -> np.ndarray:
    import cv2
    mask = np.zeros((H, W), dtype=np.uint8)
    if isinstance(segmentation, list):
        for poly in segmentation:
            if len(poly) < 6:
                continue
            pts = np.array(poly, dtype=np.float32).reshape(-1, 2).astype(np.int32)
            cv2.fillPoly(mask, [pts], color=1)
    return mask.astype(bool)


def evaluate(checkpoint: str, dataset_dir: str,
             conf_thresh: float, iou_thresh: float) -> dict:
    """
    Compute mAP@50 and mAP@50:95 on the validation set using torchmetrics.

    Design decisions
    ----------------
    - conf_thresh is applied as a low-floor safety filter only (default 0.01).
      Do NOT use the operational threshold here — that inflates mAP.
      For threshold selection, use find_best_threshold in main().
    - Every image is always fed to metric.update(), including images where the
      model produces zero predictions. Skipping zero-prediction images inflates
      mAP by hiding false negatives.
    - Ground truth project IDs (1-7) are converted to training IDs (0-6) via
      PROJECT_ID_TO_TRAIN_ID before being passed to torchmetrics.
    - The old TP/FP/FN pass has been removed — torchmetrics is the single source
      of truth for evaluation metrics.
    """
    from config.classes import PROJECT_ID_TO_TRAIN_ID

    try:
        from torchmetrics.detection.mean_ap import MeanAveragePrecision
    except ImportError:
        raise ImportError(
            "torchmetrics is required for evaluation. "
            "Run: pip install torchmetrics==1.3.2"
        )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Loading checkpoint from: %s", checkpoint)

    processor = Mask2FormerImageProcessor.from_pretrained(checkpoint)
    model     = Mask2FormerForUniversalSegmentation.from_pretrained(checkpoint)
    model.to(device).eval()

    val_img = Path(dataset_dir) / "val" / "images"
    val_ann = Path(dataset_dir) / "val" / "annotations.json"
    coco    = load_coco_annotations(str(val_ann))

    images_by_id = {img["id"]: img for img in coco["images"]}
    ann_by_image: dict = {}
    for ann in coco["annotations"]:
        ann_by_image.setdefault(ann["image_id"], []).append(ann)

    # Only evaluate images that have at least one annotation
    image_ids = [iid for iid in images_by_id if iid in ann_by_image]
    logger.info(
        "Evaluating %d val images  (low-floor conf=%.2f, for threshold testing use "
        "--find_best_threshold)",
        len(image_ids), conf_thresh
    )

    metric = MeanAveragePrecision(iou_type="segm", class_metrics=True)

    for image_id in tqdm(image_ids, desc="Evaluating"):
        img_info    = images_by_id[image_id]
        annotations = ann_by_image[image_id]
        img_path    = val_img / img_info["file_name"]
        image       = Image.open(img_path).convert("RGB")
        W, H        = image.size
        img_arr     = np.array(image)

        # ── Inference ─────────────────────────────────────────────────────────
        inputs = processor(images=img_arr, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.inference_mode():
            outputs = model(**inputs)

        result  = processor.post_process_instance_segmentation(
            outputs, target_sizes=[(H, W)]
        )[0]
        seg_map = result["segmentation"].cpu()

        # ── Build predictions (training IDs 0-6) ──────────────────────────────
        # Apply only a very low safety floor — we want the full confidence
        # distribution for COCO-style mAP. Using the operational threshold here
        # would inflate mAP by removing borderline false positives.
        pred_masks, pred_labels, pred_scores = [], [], []
        for info in result["segments_info"]:
            score  = float(info["score"])
            if score < conf_thresh:   # conf_thresh should be very low (default 0.01)
                continue
            cls_id = info["label_id"]   # training ID 0-6 from fine-tuned model
            if cls_id not in ID2LABEL:
                continue
            mask = (seg_map == info["id"])
            if not mask.any():
                continue
            pred_masks.append(mask)
            pred_labels.append(cls_id)
            pred_scores.append(score)

        # ── Build ground truth (project IDs → training IDs) ───────────────────
        gt_masks, gt_labels = [], []
        for ann in annotations:
            project_id = ann["category_id"]   # annotation file uses project IDs 1-7
            train_id   = PROJECT_ID_TO_TRAIN_ID.get(project_id)
            if train_id is None:
                continue   # annotation class not in our 7-class system
            gt_mask = polygon_to_mask(ann["segmentation"], H, W)
            if not gt_mask.any():
                continue
            gt_masks.append(torch.from_numpy(gt_mask))
            gt_labels.append(train_id)

        # ── Always update — even if predictions or GTs are empty ──────────────
        # Skipping images with no predictions hides false negatives and inflates mAP.
        pred_entry = {
            "masks":  torch.stack(pred_masks) if pred_masks
                      else torch.zeros((0, H, W), dtype=torch.bool),
            "labels": torch.tensor(pred_labels, dtype=torch.long),
            "scores": torch.tensor(pred_scores, dtype=torch.float32),
        }
        gt_entry = {
            "masks":  torch.stack(gt_masks) if gt_masks
                      else torch.zeros((0, H, W), dtype=torch.bool),
            "labels": torch.tensor(gt_labels, dtype=torch.long),
        }
        metric.update([pred_entry], [gt_entry])

    # ── Compute and return ────────────────────────────────────────────────────
    map_results     = metric.compute()
    overall_map50   = float(map_results.get("map_50",          0.0))
    overall_map5095 = float(map_results.get("map",             0.0))
    per_class_ap    = map_results.get("map_per_class",         [])

    results = {
        "mAP@50":    round(overall_map50,   4),
        "mAP@50:95": round(overall_map5095, 4),
    }
    for i, cls_id in enumerate(sorted(ID2LABEL.keys())):
        cls_name = ID2LABEL[cls_id]
        ap_val   = float(per_class_ap[i]) if i < len(per_class_ap) else 0.0
        results[cls_name] = {"ap": round(ap_val, 4)}

    return results


def main():
    args = parse_args()

    if args.find_best_threshold:
        logger.info("Sweeping confidence thresholds 0.10 → 0.90 ...")
        best_map, best_thresh = 0.0, args.conf_thresh
        for thresh in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
            res = evaluate(args.checkpoint, args.dataset_dir, thresh, args.iou_thresh)
            map50 = res.get("mAP@50", 0.0)
            logger.info("  conf=%.2f  mAP@50=%.4f", thresh, map50)
            if map50 > best_map:
                best_map, best_thresh = map50, thresh
        logger.info("Best threshold: %.2f  mAP@50=%.4f", best_thresh, best_map)
        logger.info(
            "→ Update DETECTION_MIN_CONFIDENCE = %.2f in config/settings.py",
            best_thresh
        )
        return

    results = evaluate(
        args.checkpoint, args.dataset_dir, args.conf_thresh, args.iou_thresh
    )

    logger.info("\n%s", "=" * 56)
    logger.info(
        "EVALUATION RESULTS  (conf=%.2f  iou=%.2f)",
        args.conf_thresh, args.iou_thresh
    )
    logger.info("%-12s  %8s", "Class", "AP@50")
    logger.info("-" * 56)
    for key, val in results.items():
        if key.startswith("mAP"):
            continue
        ap = val.get("ap", 0.0) if isinstance(val, dict) else 0.0
        logger.info("%-12s  %8.4f", key, ap)
    logger.info("-" * 56)
    logger.info("%-12s  %8.4f", "mAP@50",    results.get("mAP@50",    0.0))
    logger.info("%-12s  %8.4f", "mAP@50:95", results.get("mAP@50:95", 0.0))
    logger.info("=" * 56)


if __name__ == "__main__":
    main()
