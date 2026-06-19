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
from typing import Dict

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

    # H1: class_metrics=True gives per-class AP; extended_summary=True additionally
    # exposes the raw COCO `precision` tensor (T, R, K, A, M) so we can read the
    # AP@50 slice (T index 0) per class instead of only the IoU-averaged map.
    metric = MeanAveragePrecision(
        iou_type="segm", class_metrics=True, extended_summary=True
    )

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

    results = {
        "mAP@50":    round(overall_map50,   4),
        "mAP@50:95": round(overall_map5095, 4),
    }

    # H1 FIX — per-class AP must be aligned by the metric's `classes` field.
    # torchmetrics returns map_per_class / the precision tensor indexed by the
    # set of classes ACTUALLY PRESENT in the data (e.g. length 3 if only 3 of
    # the 15 classes appear), NOT by a dense 0..NUM_CLASSES-1 range. The old
    # code zipped map_per_class against sorted(ID2LABEL.keys()), so whenever a
    # class was missing from val, every later class's AP was attributed to the
    # WRONG class name (and absent classes were silently reported as 0.0000,
    # indistinguishable from a class the model scored zero on).
    #
    # We now:
    #   1. Map results strictly by `classes[i] -> value[i]`.
    #   2. Report true AP@50 per class from the precision tensor (T index 0 =
    #      IoU 0.50), falling back to the IoU-averaged map_per_class if the
    #      extended summary is unavailable in the installed torchmetrics.
    #   3. Mark classes with no ground truth in val as present=False, so "absent
    #      from val" is never confused with "scored 0.0".
    present_classes = [int(c) for c in map_results.get("classes", [])]
    map_per_class   = map_results.get("map_per_class", [])

    # Per-class AP@50 from the precision tensor, if available.
    ap50_by_class: Dict[int, float] = {}
    precision = map_results.get("precision", None)
    if precision is not None and getattr(precision, "ndim", 0) == 5:
        # precision shape: (T iou, R recall, K class, A area, M maxDet)
        # AP@50 per class = mean of precision[T=0, :, k, A=0(all), M=-1(max)]
        # over the valid (>-1) recall entries — the COCO averaging convention.
        try:
            for k, cls_id in enumerate(present_classes):
                p = precision[0, :, k, 0, -1]
                p = p[p > -1]
                ap50_by_class[cls_id] = float(p.mean()) if p.numel() > 0 else float("nan")
        except Exception as exc:   # noqa: BLE001 — never let reporting crash eval
            logger.warning("Per-class AP@50 extraction failed (%s); "
                           "falling back to IoU-averaged per-class AP.", exc)
            ap50_by_class = {}

    # IoU-averaged per-class AP (0.50:0.95), correctly aligned — used as the
    # fallback and reported alongside AP@50 for context.
    map5095_by_class: Dict[int, float] = {
        int(cls_id): float(map_per_class[i])
        for i, cls_id in enumerate(present_classes)
        if i < len(map_per_class)
    }

    for cls_id in sorted(ID2LABEL.keys()):
        cls_name = ID2LABEL[cls_id]
        present  = cls_id in present_classes
        ap50     = ap50_by_class.get(cls_id, map5095_by_class.get(cls_id, float("nan")))
        ap5095   = map5095_by_class.get(cls_id, float("nan"))
        results[cls_name] = {
            "ap":         round(ap50,   4) if present and not np.isnan(ap50)   else None,
            "ap_50_95":   round(ap5095, 4) if present and not np.isnan(ap5095) else None,
            "present":    present,
        }

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
    logger.info("%-12s  %8s  %10s", "Class", "AP@50", "AP@.5:.95")
    logger.info("-" * 56)
    for key, val in results.items():
        if key.startswith("mAP"):
            continue
        if not isinstance(val, dict):
            continue
        # H1: a class absent from the val set is shown as "—", never as 0.0000.
        # "absent from val" and "scored zero" are different facts and the
        # class-omission decision depends on telling them apart.
        if not val.get("present", True):
            logger.info("%-12s  %8s  %10s", key, "— absent", "—")
            continue
        ap50   = val.get("ap")
        ap5095 = val.get("ap_50_95")
        ap50_s   = f"{ap50:.4f}"   if isinstance(ap50,   (int, float)) else "n/a"
        ap5095_s = f"{ap5095:.4f}" if isinstance(ap5095, (int, float)) else "n/a"
        logger.info("%-12s  %8s  %10s", key, ap50_s, ap5095_s)
    logger.info("-" * 56)
    logger.info("%-12s  %8.4f", "mAP@50",    results.get("mAP@50",    0.0))
    logger.info("%-12s  %8.4f", "mAP@50:95", results.get("mAP@50:95", 0.0))
    logger.info("=" * 56)


if __name__ == "__main__":
    main()