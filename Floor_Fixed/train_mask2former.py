"""
train_mask2former.py
====================
Fine-tunes facebook/mask2former-swin-large-coco-instance on your architectural
floor plan dataset using the HuggingFace Trainer API.

Dataset format expected
-----------------------
Your dataset must be in COCO Instance Segmentation format:

    dataset/
        train/
            images/           ← .png / .jpg floor plan images
            annotations.json  ← COCO-format JSON (instances)
        val/
            images/
            annotations.json

COCO JSON structure (minimum required fields):
    {
      "images":      [{"id": 1, "file_name": "plan_001.png", "width": 1200, "height": 800}],
      "annotations": [{"id": 1, "image_id": 1, "category_id": 1,
                        "segmentation": [[x1,y1,x2,y2,...]], "bbox": [x,y,w,h], "area": 12000}],
      "categories":  [{"id": 1, "name": "wall"},
                      {"id": 2, "name": "window"},
                      {"id": 3, "name": "door"},
                      {"id": 4, "name": "stairs"},
                      {"id": 5, "name": "parking"},
                      {"id": 6, "name": "balcony"},
                      {"id": 7, "name": "terrace"}]
    }

Labeling tool recommendation
-----------------------------
Use CVAT (https://cvat.ai) or Labelme to annotate your floor plans.
Both can export directly to COCO instance segmentation format.

Usage
-----
    # Basic — uses ./dataset and saves to ./weights/mask2former-floorplan-finetuned
    python train_mask2former.py

    # Full options
    python train_mask2former.py \\
        --dataset_dir ./dataset \\
        --output_dir  ./weights/mask2former-floorplan-finetuned \\
        --epochs      30 \\
        --batch_size  2 \\
        --fp16              # enable on GPU with >= 16 GB VRAM

After training
--------------
In models/mask_rcnn_model.py, make two changes:

    1. Set model_id to your checkpoint directory:
           model_id = "./weights/mask2former-floorplan-finetuned"

    2. In _map_to_project_classes, replace `return None` with:
           valid_classes = {1, 2, 3, 4, 5, 6, 7}
           return label_id if label_id in valid_classes else None
"""

import os
import json
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
    Trainer,
    TrainingArguments,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Project class definitions  (must match your COCO annotation category IDs)
# ─────────────────────────────────────────────────────────────────────────────
LABEL2ID: Dict[str, int] = {
    "wall":    1,
    "window":  2,
    "door":    3,
    "stairs":  4,
    "parking": 5,
    "balcony": 6,
    "terrace": 7,
}
ID2LABEL: Dict[int, str] = {v: k for k, v in LABEL2ID.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Dataset
# ─────────────────────────────────────────────────────────────────────────────

class FloorPlanDataset(Dataset):
    """
    Loads floor plan images and COCO instance segmentation annotations.
    Returns processor-ready tensors for Mask2Former.
    """

    def __init__(self, image_dir: str, annotation_file: str, processor: Mask2FormerImageProcessor):
        self.image_dir = Path(image_dir)
        self.processor = processor

        with open(annotation_file, "r", encoding="utf-8") as f:
            coco = json.load(f)

        # Build lookup tables
        self.images: Dict[int, dict] = {img["id"]: img for img in coco["images"]}

        self.ann_by_image: Dict[int, List[dict]] = {}
        for ann in coco["annotations"]:
            iid = ann["image_id"]
            self.ann_by_image.setdefault(iid, []).append(ann)

        # Only keep images that have at least one annotation
        self.image_ids: List[int] = [
            iid for iid in self.images if iid in self.ann_by_image
        ]

        logger.info(
            f"Dataset loaded: {len(self.image_ids)} images with annotations "
            f"(from {annotation_file})"
        )

    def __len__(self) -> int:
        return len(self.image_ids)

    def __getitem__(self, idx: int) -> dict:
        image_id    = self.image_ids[idx]
        image_info  = self.images[image_id]
        annotations = self.ann_by_image[image_id]

        # Load image
        img_path = self.image_dir / image_info["file_name"]
        image    = Image.open(img_path).convert("RGB")
        W, H     = image.size

        # Build per-instance binary masks and their class labels
        instance_masks:  List[np.ndarray] = []
        category_labels: List[int]        = []

        for ann in annotations:
            cat_id = ann["category_id"]
            if cat_id not in ID2LABEL:
                continue  # skip categories not in our 7-class system

            mask = self._polygon_to_mask(ann["segmentation"], H, W)
            if mask.sum() == 0:
                continue  # skip empty/degenerate polygons

            instance_masks.append(mask)
            category_labels.append(cat_id)

        # ── Build the instance segmentation map ──────────────────────────────
        # instance_map[y, x] = instance_id (1-based), 0 = background
        # instance_id_to_semantic_id maps each instance_id → category_id
        instance_map = np.zeros((H, W), dtype=np.int32)
        instance_id_to_semantic_id: Dict[int, int] = {}

        if instance_masks:
            for i, (mask, cat_id) in enumerate(zip(instance_masks, category_labels), start=1):
                # Later instances overwrite earlier ones where they overlap —
                # this is standard practice and accepted by the Mask2Former loss.
                instance_map[mask > 0] = i
                instance_id_to_semantic_id[i] = cat_id
        else:
            # Edge case: all annotations were filtered out (unknown class / empty polygon).
            # We must still return a valid sample. A single background pixel is set so
            # the processor produces non-empty class_labels (required by Mask2Former forward).
            instance_map[0, 0] = 1
            instance_id_to_semantic_id[1] = 1   # treat as "wall" (least harmful dummy)
            logger.debug(f"Image {image_id}: no valid annotations found — using dummy mask")

        # ── Run through the HuggingFace processor ────────────────────────────
        # Input:  PIL image + instance_map + id→class mapping
        # Output: pixel_values (C,H,W), pixel_mask (H,W),
        #         mask_labels (N,H,W), class_labels (N,)
        encoding = self.processor(
            images=[image],
            segmentation_maps=[instance_map],
            instance_id_to_semantic_id=[instance_id_to_semantic_id],
            return_tensors="pt",
        )

        # Remove the batch dimension (shape 1,…) that the processor adds.
        # The Trainer's collate_fn will re-add it across the batch.
        return {k: v.squeeze(0) for k, v in encoding.items()}

    @staticmethod
    def _polygon_to_mask(segmentation, H: int, W: int) -> np.ndarray:
        """Convert a COCO polygon or RLE segmentation to a binary mask."""
        mask = np.zeros((H, W), dtype=np.uint8)

        if isinstance(segmentation, list):
            # Standard COCO polygon format: list of [x1,y1,x2,y2,...] flat arrays
            for poly in segmentation:
                if len(poly) < 6:   # Minimum 3 points required for a polygon
                    continue
                pts = np.array(poly, dtype=np.float32).reshape(-1, 2).astype(np.int32)
                cv2.fillPoly(mask, [pts], color=1)

        elif isinstance(segmentation, dict):
            # RLE format — less common for floor plans but handled gracefully
            try:
                from pycocotools import mask as coco_mask
                rle  = coco_mask.frPyObjects(segmentation, H, W)
                mask = coco_mask.decode(rle).astype(np.uint8)
            except ImportError:
                logger.warning(
                    "pycocotools not installed — RLE annotation skipped. "
                    "Install with: pip install pycocotools"
                )

        return mask


# ─────────────────────────────────────────────────────────────────────────────
# Collator
# ─────────────────────────────────────────────────────────────────────────────

def collate_fn(batch: List[dict]) -> dict:
    """
    Collates a list of dataset samples into a training batch.

    pixel_values and pixel_mask are stacked into tensors.
    mask_labels and class_labels are kept as lists of tensors because
    each image can have a different number of instances (ragged).
    Mask2FormerForUniversalSegmentation.forward() natively accepts lists for these.
    """
    pixel_values = torch.stack([b["pixel_values"] for b in batch])
    pixel_mask   = torch.stack([b["pixel_mask"]   for b in batch])
    mask_labels  = [b["mask_labels"]  for b in batch]   # List[Tensor(N_i, H, W)]
    class_labels = [b["class_labels"] for b in batch]   # List[Tensor(N_i,)]

    return {
        "pixel_values":  pixel_values,
        "pixel_mask":    pixel_mask,
        "mask_labels":   mask_labels,
        "class_labels":  class_labels,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Arguments
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fine-tune Mask2Former (Swin-L) on architectural floor plan data"
    )
    parser.add_argument(
        "--dataset_dir", default="./dataset",
        help="Root folder with train/ and val/ sub-directories"
    )
    parser.add_argument(
        "--output_dir", default="./weights/mask2former-floorplan-finetuned",
        help="Directory to save checkpoints and the final model"
    )
    parser.add_argument(
        "--base_model", default="facebook/mask2former-swin-large-coco-instance",
        help="HuggingFace model ID to start from (or local path to a checkpoint)"
    )
    parser.add_argument("--epochs",       type=int,   default=30)
    parser.add_argument(
        "--batch_size",  type=int,   default=2,
        help="Per-device train batch size. Use 1 if GPU has < 16 GB VRAM."
    )
    parser.add_argument("--lr",           type=float, default=5e-5)
    parser.add_argument("--warmup_ratio", type=float, default=0.05)
    parser.add_argument("--save_steps",   type=int,   default=200)
    parser.add_argument("--eval_steps",   type=int,   default=200)
    parser.add_argument(
        "--fp16", action="store_true",
        help="Enable mixed-precision training (recommended for CUDA GPUs with >= 16 GB)"
    )
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
            raise FileNotFoundError(
                f"Required path not found: {p}\n"
                "Dataset must be structured as:\n"
                "  dataset/train/images/   + annotations.json\n"
                "  dataset/val/images/     + annotations.json"
            )

    logger.info(f"Loading processor and model from: {args.base_model}")

    # ── Processor ────────────────────────────────────────────────────────────
    processor = Mask2FormerImageProcessor.from_pretrained(args.base_model)

    # ── Model ─────────────────────────────────────────────────────────────────
    # ignore_mismatched_sizes=True replaces the COCO 80-class classification head
    # with a fresh randomly-initialised head for our 7 architectural classes.
    # All other weights (backbone, pixel decoder, transformer decoder) are
    # transferred from the COCO checkpoint — this is transfer learning.
    num_labels = len(LABEL2ID)
    model = Mask2FormerForUniversalSegmentation.from_pretrained(
        args.base_model,
        num_labels=num_labels,
        id2label=ID2LABEL,
        label2id=LABEL2ID,
        ignore_mismatched_sizes=True,
    )
    logger.info(f"Model ready — classification head re-initialised for {num_labels} classes: {list(LABEL2ID.keys())}")

    # ── Datasets ──────────────────────────────────────────────────────────────
    train_dataset = FloorPlanDataset(train_img, train_ann, processor)
    val_dataset   = FloorPlanDataset(val_img,   val_ann,   processor)

    # ── Training arguments ────────────────────────────────────────────────────
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=1,           # evaluation is more memory intensive
        learning_rate=args.lr,
        warmup_ratio=args.warmup_ratio,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        eval_strategy="steps",                  # eval_strategy (not evaluation_strategy) for transformers >= 4.41
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,                     # keep the 3 most recent checkpoints
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        fp16=args.fp16,
        dataloader_num_workers=2,
        logging_dir=os.path.join(args.output_dir, "logs"),
        logging_steps=50,
        report_to="none",                       # change to "tensorboard" or "wandb" if desired
        remove_unused_columns=False,            # required: dataset returns non-standard keys
    )

    # ── Trainer ───────────────────────────────────────────────────────────────
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collate_fn,
        processing_class=processor,             # processing_class= (not tokenizer=) for transformers >= 4.46
    )

    # ── Train ─────────────────────────────────────────────────────────────────
    logger.info("Starting training...")
    trainer.train()

    # ── Save ──────────────────────────────────────────────────────────────────
    logger.info(f"Training complete. Saving final model to: {args.output_dir}")
    trainer.save_model(args.output_dir)
    processor.save_pretrained(args.output_dir)

    logger.info("=" * 60)
    logger.info("TRAINING COMPLETE — NEXT STEPS:")
    logger.info(f"  1. In models/mask_rcnn_model.py, set:")
    logger.info(f"         model_id = \"{args.output_dir}\"")
    logger.info("  2. In _map_to_project_classes, replace `return None` with:")
    logger.info("         valid_classes = {1, 2, 3, 4, 5, 6, 7}")
    logger.info("         return label_id if label_id in valid_classes else None")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
