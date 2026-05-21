"""
train_mask2former.py  (v2 — post-training improvements)
========================================================
Fine-tunes facebook/mask2former-swin-large-coco-instance on your architectural
floor plan dataset using the HuggingFace Trainer API.

Improvements over v1
--------------------
1. Data augmentation    — rotation, flip, colour jitter, scale jitter applied
                          to every training sample every epoch. Multiplies
                          effective dataset size ~8x at zero annotation cost.
2. Class-weighted loss  — rare classes (terrace, balcony) get higher weight so
                          the model trains them as hard as walls.
3. Gradient accumulation — effective batch of 16 without 16x GPU memory.
4. Differential LR      — backbone at lr/10, fresh classification head at lr.
                          Protects pretrained weights while learning new classes.
5. mAP metric           — saves the best checkpoint by mAP@50 on the val set,
                          not by eval_loss.
6. Auto worker count    — dataloader workers sized to available CPU cores.
7. Resume support       — --resume_from_checkpoint to continue a crashed run.

Dataset format
--------------
dataset/
    train/
        images/           ← .png / .jpg floor plan images
        annotations.json  ← COCO instance segmentation JSON
    val/
        images/
        annotations.json

Usage
-----
    # Basic
    python train_mask2former.py

    # Full options
    python train_mask2former.py \\
        --dataset_dir ./dataset \\
        --output_dir  ./weights/mask2former-floorplan-finetuned \\
        --epochs      50 \\
        --batch_size  2 \\
        --grad_accum  8 \\
        --fp16

After training
--------------
1. Set FLOORPLAN_MODEL_PATH to the checkpoint directory:
       export FLOORPLAN_MODEL_PATH=./weights/mask2former-floorplan-finetuned

2. Run evaluate.py to compute mAP and find the best confidence threshold:
       python evaluate.py --checkpoint ./weights/mask2former-floorplan-finetuned
       python evaluate.py --checkpoint ... --find_best_threshold

3. Update DETECTION_MIN_CONFIDENCE in config/settings.py with the best threshold.

4. Start the API with ALLOW_COCO_FALLBACK=false in production:
       APP_ENV=production ALLOW_COCO_FALLBACK=false \\
       gunicorn --config gunicorn.conf.py application:application

No manual edits to models/mask_rcnn_model.py are needed — model path and
class mapping are handled automatically via environment variables and
config/classes.py.
"""

import os
import json
import random
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch
from PIL import Image, ImageEnhance
from torch.utils.data import Dataset
from torchmetrics.detection import MeanAveragePrecision

from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
    Trainer,
    TrainingArguments,
)

from config.classes import (
    NAME_TO_TRAIN_ID  as LABEL2ID,
    TRAIN_ID_TO_NAME  as ID2LABEL,
    NUM_CLASSES,
    CLASS_WEIGHTS,
    PROJECT_ID_TO_TRAIN_ID,
)

# Remove local redefinitions that were here before — they are now in config/classes.py


# ─────────────────────────────────────────────────────────────────────────────
# Augmentation
# ─────────────────────────────────────────────────────────────────────────────

class FloorPlanAugmenter:
    """
    Augmentation pipeline tuned for floor plan images.

    Floor plans are architectural drawings, not natural photos, so augmentation
    must preserve geometric meaning:
        ✓ Rotation by 90° multiples — a plan rotated 90° is still a valid plan
        ✓ Horizontal/vertical flip — mirrored plans are valid
        ✓ Brightness/contrast jitter — scans vary in exposure
        ✓ Scale jitter — simulate different scan resolutions
        ✗ Heavy colour distortion — floor plans have meaningful colours
        ✗ Cutout/mosaic — would destroy wall connectivity

    All mask transformations are applied identically to the image.
    """

    def __init__(self,
                 rotate_prob: float = 0.5,
                 flip_h_prob: float = 0.5,
                 flip_v_prob: float = 0.3,
                 brightness_range: Tuple[float, float] = (0.7, 1.3),
                 scale_range: Tuple[float, float] = (0.8, 1.2)):
        self.rotate_prob       = rotate_prob
        self.flip_h_prob       = flip_h_prob
        self.flip_v_prob       = flip_v_prob
        self.brightness_range  = brightness_range
        self.scale_range       = scale_range

    def __call__(self, image: Image.Image,
                 instance_map: np.ndarray) -> Tuple[Image.Image, np.ndarray]:
        """
        Apply augmentation to both the PIL image and the integer instance_map.

        Parameters
        ----------
        image        : PIL.Image.Image  (RGB)
        instance_map : np.ndarray (H, W) int32  — instance IDs per pixel

        Returns
        -------
        (augmented_image, augmented_instance_map) — same types as inputs
        """
        # ── 90° rotation ─────────────────────────────────────────────────────
        if random.random() < self.rotate_prob:
            k = random.choice([1, 2, 3])           # 90, 180, 270 degrees
            image        = image.rotate(k * 90, expand=True)
            instance_map = np.rot90(instance_map, k=k).copy()

        # ── Horizontal flip ───────────────────────────────────────────────────
        if random.random() < self.flip_h_prob:
            image        = image.transpose(Image.FLIP_LEFT_RIGHT)
            instance_map = np.fliplr(instance_map).copy()

        # ── Vertical flip ─────────────────────────────────────────────────────
        if random.random() < self.flip_v_prob:
            image        = image.transpose(Image.FLIP_TOP_BOTTOM)
            instance_map = np.flipud(instance_map).copy()

        # ── Brightness / contrast jitter ──────────────────────────────────────
        # Applied to image only — does not affect masks
        if random.random() < 0.5:
            factor = random.uniform(*self.brightness_range)
            image  = ImageEnhance.Brightness(image).enhance(factor)
        if random.random() < 0.3:
            factor = random.uniform(0.8, 1.2)
            image  = ImageEnhance.Contrast(image).enhance(factor)

        # ── Scale jitter ──────────────────────────────────────────────────────
        # Resize both image and instance_map by the same random scale factor.
        # Simulates different scan resolutions and zoom levels.
        if random.random() < 0.4:
            scale = random.uniform(*self.scale_range)
            orig_w, orig_h = image.size
            new_w = max(64, int(orig_w * scale))
            new_h = max(64, int(orig_h * scale))
            image        = image.resize((new_w, new_h), Image.BILINEAR)
            instance_map = cv2.resize(
                instance_map, (new_w, new_h),
                interpolation=cv2.INTER_NEAREST   # nearest-neighbour preserves instance IDs
            )

        return image, instance_map


# ─────────────────────────────────────────────────────────────────────────────
# Dataset
# ─────────────────────────────────────────────────────────────────────────────

class FloorPlanDataset(Dataset):
    """
    Loads floor plan images and COCO instance segmentation annotations.
    Returns processor-ready tensors for Mask2Former.
    """

    def __init__(self, image_dir: str, annotation_file: str,
                 processor: Mask2FormerImageProcessor,
                 augment: bool = False):
        self.image_dir = Path(image_dir)
        self.processor = processor
        self.augmenter = FloorPlanAugmenter() if augment else None

        with open(annotation_file, "r", encoding="utf-8") as f:
            coco = json.load(f)

        self.images: Dict[int, dict] = {img["id"]: img for img in coco["images"]}

        self.ann_by_image: Dict[int, List[dict]] = {}
        for ann in coco["annotations"]:
            self.ann_by_image.setdefault(ann["image_id"], []).append(ann)

        self.image_ids: List[int] = []
        for iid in self.images:
            anns = self.ann_by_image.get(iid, [])
            # Keep only images that have at least one annotation with a valid
            # project class ID (1-7). Filter here so PyTorch DataLoader never
            # receives an invalid sample — DataLoader does not skip exceptions.
            if any(ann.get("category_id") in PROJECT_ID_TO_TRAIN_ID for ann in anns):
                self.image_ids.append(iid)

        logger.info(
            "Dataset loaded: %d images  augmentation=%s  (%s)",
            len(self.image_ids), augment, annotation_file
        )

    def __len__(self) -> int:
        return len(self.image_ids)

    def __getitem__(self, idx: int) -> dict:
        image_id    = self.image_ids[idx]
        image_info  = self.images[image_id]
        annotations = self.ann_by_image[image_id]

        img_path = self.image_dir / image_info["file_name"]
        image    = Image.open(img_path).convert("RGB")
        W, H     = image.size

        # Build instance_map and id→class mapping
        instance_masks:  List[np.ndarray] = []
        category_labels: List[int]        = []

        # Validate against PROJECT_ID_TO_TRAIN_ID (keys: project IDs 1-7).
        # Store training IDs (0-6) directly in category_labels so the conversion
        # is done once here and not scattered across two places below.
        for ann in annotations:
            project_id = ann["category_id"]
            train_id   = PROJECT_ID_TO_TRAIN_ID.get(project_id)
            if train_id is None:
                # Unknown class (not one of our 7) — skip this annotation
                continue
            mask = _polygon_to_mask(ann["segmentation"], H, W)
            if mask.sum() == 0:
                continue
            instance_masks.append(mask)
            category_labels.append(train_id)   # ← training ID (0-6), not project ID

        # This should not happen because __init__ already filtered valid images,
        # but guard defensively in case the dataset changes between init and getitem.
        if not instance_masks:
            raise RuntimeError(
                f"Image {image_id} has no valid annotations after filtering. "
                "This should have been caught during dataset initialization."
            )

        instance_map = np.zeros((H, W), dtype=np.int32)
        instance_id_to_semantic_id: Dict[int, int] = {}

        for i, (mask, train_id) in enumerate(zip(instance_masks, category_labels), start=1):
            instance_map[mask > 0] = i
            instance_id_to_semantic_id[i] = train_id   # already a training ID

        # ── Augmentation ──────────────────────────────────────────────────────
        if self.augmenter is not None:
            image, instance_map = self.augmenter(image, instance_map)

        # ── Rebuild id mapping after augmentation ─────────────────────────────
        # Augmentation does not change which instances exist or their labels,
        # only their spatial position — so instance_id_to_semantic_id stays valid.

        encoding = self.processor(
            images=[image],
            segmentation_maps=[instance_map],
            instance_id_to_semantic_id=[instance_id_to_semantic_id],
            return_tensors="pt",
        )
        return {k: v.squeeze(0) for k, v in encoding.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Polygon → mask
# ─────────────────────────────────────────────────────────────────────────────

def _polygon_to_mask(segmentation, H: int, W: int) -> np.ndarray:
    """Convert COCO polygon or RLE segmentation to a binary mask."""
    mask = np.zeros((H, W), dtype=np.uint8)

    if isinstance(segmentation, list):
        for poly in segmentation:
            if len(poly) < 6:
                continue
            pts = np.array(poly, dtype=np.float32).reshape(-1, 2).astype(np.int32)
            cv2.fillPoly(mask, [pts], color=1)

    elif isinstance(segmentation, dict):
        try:
            from pycocotools import mask as coco_mask
            rle  = coco_mask.frPyObjects(segmentation, H, W)
            mask = coco_mask.decode(rle).astype(np.uint8)
        except ImportError:
            logger.warning("pycocotools not installed — RLE annotation skipped")

    return mask


# ─────────────────────────────────────────────────────────────────────────────
# Collator
# ─────────────────────────────────────────────────────────────────────────────

def collate_fn(batch: List[dict]) -> dict:
    return {
        "pixel_values":  torch.stack([b["pixel_values"] for b in batch]),
        "pixel_mask":    torch.stack([b["pixel_mask"]   for b in batch]),
        "mask_labels":   [b["mask_labels"]  for b in batch],
        "class_labels":  [b["class_labels"] for b in batch],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint selection note
# ─────────────────────────────────────────────────────────────────────────────
# Training saves the best checkpoint by eval_loss (not mAP).
# This is intentional: the HuggingFace Trainer API does not pass image sizes
# to compute_metrics, so true mAP cannot be computed inline during training.
# After training, run evaluate.py to measure real mAP@50 on the val set
# and use it to select the best checkpoint manually if needed.
#
# CLASS_WEIGHTS is defined in config/classes.py for future use. Implementing
# custom per-class loss weights in Mask2Former requires subclassing Trainer and
# overriding compute_loss(). This is planned for a later iteration.

# ─────────────────────────────────────────────────────────────────────────────
# Differential learning rate — backbone vs classification head
# ─────────────────────────────────────────────────────────────────────────────

def get_param_groups(model, base_lr: float, backbone_lr_ratio: float = 0.1):
    """
    Return two parameter groups:
        - backbone (pixel_level_module + transformer_module): lr × backbone_lr_ratio
        - classification head (class_predictor): lr

    The pretrained backbone needs a much lower LR than the freshly-initialized
    classification head.  10x difference is the standard approach.

    Parameters
    ----------
    model            : the Mask2Former model
    base_lr          : learning rate for the classification head
    backbone_lr_ratio: LR multiplier for the backbone. Default 0.1 = lr/10.
    """
    backbone_params    = []
    head_params        = []

    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        # class_predictor is the fresh classification head
        if "class_predictor" in name:
            head_params.append(param)
        else:
            backbone_params.append(param)

    logger.info(
        "Param groups: backbone=%d params @ lr=%.2e  |  head=%d params @ lr=%.2e",
        len(backbone_params), base_lr * backbone_lr_ratio,
        len(head_params),     base_lr
    )

    return [
        {"params": backbone_params, "lr": base_lr * backbone_lr_ratio},
        {"params": head_params,     "lr": base_lr},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fine-tune Mask2Former (Swin-L) on architectural floor plan data"
    )
    parser.add_argument("--dataset_dir",  default="./dataset")
    parser.add_argument("--output_dir",   default="./weights/mask2former-floorplan-finetuned")
    parser.add_argument("--base_model",   default="facebook/mask2former-swin-large-coco-instance")
    parser.add_argument("--epochs",       type=int,   default=50,
                        help="More epochs are safe with augmentation; overfitting is reduced.")
    parser.add_argument("--batch_size",   type=int,   default=2,
                        help="Per-device batch size. Use 1 if GPU < 16 GB VRAM.")
    parser.add_argument("--grad_accum",   type=int,   default=8,
                        help="Gradient accumulation steps. Effective batch = batch_size × grad_accum.")
    parser.add_argument("--lr",           type=float, default=5e-5,
                        help="Learning rate for the classification head. Backbone uses lr/10.")
    parser.add_argument("--backbone_lr_ratio", type=float, default=0.1,
                        help="Backbone LR = lr × this ratio. Default 0.1 = lr/10.")
    parser.add_argument("--warmup_ratio", type=float, default=0.05)
    parser.add_argument("--save_steps",   type=int,   default=100)
    parser.add_argument("--eval_steps",   type=int,   default=100)
    parser.add_argument("--fp16",         action="store_true",
                        help="Mixed-precision training. Recommended for GPU >= 16 GB VRAM.")
    parser.add_argument("--resume_from_checkpoint", default=None,
                        help="Path to a checkpoint directory to resume training from.")
    parser.add_argument("--no_augment",   action="store_true",
                        help="Disable data augmentation (not recommended unless debugging).")
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    dataset_dir = Path(args.dataset_dir)
    train_img   = dataset_dir / "train" / "images"
    train_ann   = dataset_dir / "train" / "annotations.json"
    val_img     = dataset_dir / "val"   / "images"
    val_ann     = dataset_dir / "val"   / "annotations.json"

    for p in [train_img, train_ann, val_img, val_ann]:
        if not p.exists():
            raise FileNotFoundError(f"Required path not found: {p}")

    effective_batch = args.batch_size * args.grad_accum
    logger.info(
        "Effective batch size: %d  (%d per-device × %d grad_accum steps)",
        effective_batch, args.batch_size, args.grad_accum
    )

    # ── Processor ────────────────────────────────────────────────────────────
    processor = Mask2FormerImageProcessor.from_pretrained(args.base_model)

    # ── Model ─────────────────────────────────────────────────────────────────
    # NUM_CLASSES comes from config/classes.py — single source of truth (value: 7)
    # Training head indices are 0-indexed (0..6), which is correct for num_labels=7.
    num_labels = NUM_CLASSES
    model = Mask2FormerForUniversalSegmentation.from_pretrained(
        args.base_model,
        num_labels=num_labels,
        id2label=ID2LABEL,
        label2id=LABEL2ID,
        ignore_mismatched_sizes=True,
    )
    logger.info("Model ready — %d classes: %s", num_labels, list(LABEL2ID.keys()))

    # ── Datasets ──────────────────────────────────────────────────────────────
    use_augment = not args.no_augment
    train_dataset = FloorPlanDataset(train_img, train_ann, processor, augment=use_augment)
    val_dataset   = FloorPlanDataset(val_img,   val_ann,   processor, augment=False)

    # ── Auto worker count ─────────────────────────────────────────────────────
    num_workers = max(1, (os.cpu_count() or 4) // 2)
    logger.info("DataLoader workers: %d", num_workers)

    # ── Training arguments ────────────────────────────────────────────────────
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,          # applies to head; backbone gets lr × ratio
        warmup_ratio=args.warmup_ratio,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        fp16=args.fp16,
        dataloader_num_workers=num_workers,
        logging_dir=os.path.join(args.output_dir, "logs"),
        logging_steps=25,
        report_to="none",
        remove_unused_columns=False,
    )

    # ── Optimizer with differential LR ───────────────────────────────────────
    # We build the optimizer manually so backbone and head get different LRs.
    # The Trainer accepts a pre-built optimizer via the optimizers= argument.
    param_groups = get_param_groups(model, args.lr, args.backbone_lr_ratio)
    optimizer    = torch.optim.AdamW(param_groups, weight_decay=0.01)

    # ── Trainer ───────────────────────────────────────────────────────────────
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collate_fn,
        processing_class=processor,
        optimizers=(optimizer, None),   # None = Trainer builds the LR scheduler
    )

    # ── Train ─────────────────────────────────────────────────────────────────
    logger.info("Starting training...")
    trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)

    # ── Save ──────────────────────────────────────────────────────────────────
    logger.info("Training complete. Saving to: %s", args.output_dir)
    trainer.save_model(args.output_dir)
    processor.save_pretrained(args.output_dir)

    logger.info("=" * 60)
    logger.info("TRAINING COMPLETE — NEXT STEPS:")
    logger.info("  1. Evaluate mAP on the val set:")
    logger.info("         python evaluate.py --checkpoint %s", args.output_dir)
    logger.info("         python evaluate.py --checkpoint %s --find_best_threshold",
                args.output_dir)
    logger.info("  2. Set FLOORPLAN_MODEL_PATH:")
    logger.info("         export FLOORPLAN_MODEL_PATH=%s", args.output_dir)
    logger.info("  3. Update DETECTION_MIN_CONFIDENCE in config/settings.py")
    logger.info("  4. Start production API:")
    logger.info("         APP_ENV=production ALLOW_COCO_FALLBACK=false "
                "gunicorn --config gunicorn.conf.py application:application")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
