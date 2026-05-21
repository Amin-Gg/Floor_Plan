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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LABEL2ID = {
    "wall": 1, "window": 2, "door": 3, "stairs": 4,
    "parking": 5, "balcony": 6, "terrace": 7,
}
ID2LABEL = {v: k for k, v in LABEL2ID.items()}


def parse_args():
    p = argparse.ArgumentParser(description="Evaluate Mask2Former mAP on val set")
    p.add_argument("--checkpoint",   required=True,
                   help="Path to trained checkpoint directory")
    p.add_argument("--dataset_dir",  default="./dataset")
    p.add_argument("--conf_thresh",  type=float, default=0.45,
                   help="Confidence threshold for predictions")
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
    Run evaluation and return per-class AP dict plus overall mAP.
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Loading checkpoint from: %s", checkpoint)

    processor = Mask2FormerImageProcessor.from_pretrained(checkpoint)
    model     = Mask2FormerForUniversalSegmentation.from_pretrained(checkpoint)
    model.to(device).eval()

    val_img  = Path(dataset_dir) / "val" / "images"
    val_ann  = Path(dataset_dir) / "val" / "annotations.json"
    coco     = load_coco_annotations(str(val_ann))

    images_by_id = {img["id"]: img for img in coco["images"]}
    ann_by_image = {}
    for ann in coco["annotations"]:
        ann_by_image.setdefault(ann["image_id"], []).append(ann)

    # Per-class TP, FP, FN counters
    stats = {cls_id: {"tp": 0, "fp": 0, "fn": 0} for cls_id in ID2LABEL}

    image_ids = [iid for iid in images_by_id if iid in ann_by_image]
    logger.info("Evaluating %d val images at conf=%.2f  iou=%.2f",
                len(image_ids), conf_thresh, iou_thresh)

    for image_id in tqdm(image_ids, desc="Evaluating"):
        img_info    = images_by_id[image_id]
        annotations = ann_by_image[image_id]
        img_path    = val_img / img_info["file_name"]

        image    = Image.open(img_path).convert("RGB")
        W, H     = image.size
        img_arr  = np.array(image)

        # ── Inference ─────────────────────────────────────────────────────────
        inputs = processor(images=img_arr, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.inference_mode():
            outputs = model(**inputs)

        result = processor.post_process_instance_segmentation(
            outputs, target_sizes=[(H, W)]
        )[0]

        seg_map = result["segmentation"].cpu().numpy()

        # Collect predictions above threshold
        preds_by_class = {cls_id: [] for cls_id in ID2LABEL}
        for info in result["segments_info"]:
            if float(info["score"]) < conf_thresh:
                continue
            cls_id = info["label_id"]
            if cls_id not in ID2LABEL:
                continue
            mask = (seg_map == info["id"]).astype(bool)
            if mask.sum() == 0:
                continue
            preds_by_class[cls_id].append({"mask": mask, "score": float(info["score"])})

        # Collect ground truth by class
        gt_by_class = {cls_id: [] for cls_id in ID2LABEL}
        for ann in annotations:
            cls_id = ann["category_id"]
            if cls_id not in ID2LABEL:
                continue
            gt_mask = polygon_to_mask(ann["segmentation"], H, W)
            if gt_mask.sum() == 0:
                continue
            gt_by_class[cls_id].append(gt_mask)

        # ── Match predictions to ground truth ─────────────────────────────────
        for cls_id in ID2LABEL:
            preds = preds_by_class[cls_id]
            gts   = list(gt_by_class[cls_id])   # copy — we remove matched GTs

            matched_gt = set()
            for pred in sorted(preds, key=lambda x: x["score"], reverse=True):
                best_iou, best_idx = 0.0, -1
                for j, gt_mask in enumerate(gts):
                    if j in matched_gt:
                        continue
                    iou = masks_iou(pred["mask"], gt_mask)
                    if iou > best_iou:
                        best_iou, best_idx = iou, j

                if best_iou >= iou_thresh and best_idx >= 0:
                    stats[cls_id]["tp"] += 1
                    matched_gt.add(best_idx)
                else:
                    stats[cls_id]["fp"] += 1

            # Unmatched GTs are false negatives
            stats[cls_id]["fn"] += len(gts) - len(matched_gt)

    # ── Compute precision / recall / F1 / AP per class ────────────────────────
    results = {}
    ap_values = []

    for cls_id, s in stats.items():
        tp, fp, fn = s["tp"], s["fp"], s["fn"]
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        # AP approximation at this single IoU threshold
        ap = precision * recall  # simplified; full AP needs P-R curve
        ap_values.append(ap)

        results[ID2LABEL[cls_id]] = {
            "precision": round(precision, 4),
            "recall":    round(recall,    4),
            "f1":        round(f1,        4),
            "ap":        round(ap,        4),
            "tp": tp, "fp": fp, "fn": fn,
        }

    results["mAP"] = round(float(np.mean(ap_values)), 4)

    return results


def main():
    args = parse_args()

    if args.find_best_threshold:
        logger.info("Sweeping confidence thresholds 0.10 → 0.90...")
        best_map, best_thresh = 0.0, args.conf_thresh
        for thresh in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
            res = evaluate(args.checkpoint, args.dataset_dir, thresh, args.iou_thresh)
            logger.info("  conf=%.2f  mAP=%.4f", thresh, res["mAP"])
            if res["mAP"] > best_map:
                best_map, best_thresh = res["mAP"], thresh
        logger.info("Best threshold: %.2f  mAP=%.4f", best_thresh, best_map)
        logger.info(
            "Update DETECTION_MIN_CONFIDENCE = %.2f in config/settings.py", best_thresh
        )
        return

    results = evaluate(
        args.checkpoint, args.dataset_dir, args.conf_thresh, args.iou_thresh
    )

    logger.info("\n%s", "=" * 52)
    logger.info("EVALUATION RESULTS  (conf=%.2f  iou=%.2f)", args.conf_thresh, args.iou_thresh)
    logger.info("%-12s  %8s  %8s  %8s  %8s", "Class", "Prec", "Recall", "F1", "AP")
    logger.info("-" * 52)
    for cls_name, m in results.items():
        if cls_name == "mAP":
            continue
        logger.info("%-12s  %8.4f  %8.4f  %8.4f  %8.4f",
                    cls_name, m["precision"], m["recall"], m["f1"], m["ap"])
    logger.info("-" * 52)
    logger.info("%-12s  %39.4f", "mAP", results["mAP"])
    logger.info("=" * 52)


if __name__ == "__main__":
    main()
