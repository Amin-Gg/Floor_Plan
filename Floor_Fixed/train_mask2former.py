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
            images/          ← .png / .jpg floor plan images
            annotations.json ← COCO-format JSON (instances)
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
Use CVAT (https://cvat.ai) or Labelme to annotate.
Both can export COCO instance segmentation format.

Usage
-----
    # Basic
    python train_mask2former.py

    # With custom paths and GPU
    python train_mask2former.py \
        --dataset_dir ./dataset \
        --output_dir  ./weights/mask2former-floorplan-finetuned \
        --epochs      30 \
        --batch_size  2

After training
--------------
In models/mask_rcnn_model.py, make two changes:

    1.  model_id = "./weights/mask2former-floorplan-finetuned"  # use your checkpoint

    2.  Replace _map_to_project_classes with:
            valid_classes = {1, 2, 3, 4, 5, 6, 7}
            return label_id if label_id in valid_classes else None
"""

import os
import json
import argparse
import logging
from pathlib import Path

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
# Project class definitions (must match your COCO annotations)
# ─────────────────────────────────────────────────────────────────────────────
LABEL2ID = {
    "wall":    1,
    "window":  2,
    "door":    3,
    "stairs":  4,
    "parking": 5,
    "balcony": 6,
    "terrace": 7,
}
ID2LABEL = {v: k for k, v in LABEL2ID.items()}

# ─────────────────────────────────────────────────────────────────────────────
# Dataset
# ─────────────────────────────────────────────────────────────────────────────

class FloorPlanDataset(Dataset):
    """
    Loads floor plan images and their COCO instance segmentation annotations.
    Returns processor-ready inputs for Mask2Former.
    """

    def __init__(self, image_dir: str, annotation_file: str, processor):
        self.image_dir  = Path(image_dir)
        self.processor  = processor

        with open(annotation_file, "r", encoding="utf-8") as f:
            coco = json.load(f)

        # Build quick lookup tables
        self.images = {img["id"]: img for img in coco["images"]}

        # Group annotations by image_id
        self.ann_by_image: dict[int, list] = {}
        for ann in coco["annotations"]:
            iid = ann["image_id"]
            self.ann_by_image.setdefault(iid, []).append(ann)

        # Only keep images that have at least one annotation
        self.image_ids = [
            iid for iid in self.images
            if iid in self.ann_by_image
        ]

        logger.info(f"Dataset: {len(self.image_ids)} images with annotations "
                    f"from {annotation_file}")

    def __len__(self):
        return len(self.image_ids)

    def __getitem__(self, idx):
        image_id   = self.image_ids[idx]
        image_info = self.images[image_id]
        annotations = self.ann_by_image[image_id]

        # Load image
        img_path = self.image_dir / image_info["file_name"]
        image    = Image.open(img_path).convert("RGB")
        W, H     = image.size

        # Build instance masks and category labels
        instance_masks  = []
        category_labels = []

        for ann in annotations:
            cat_id = ann["category_id"]
            if cat_id not in ID2LABEL:
                # Skip annotations with categories not in our 7-class system
                continue

            # Build binary mask from COCO polygon segmentation
            mask = self._polygon_to_mask(ann["segmentation"], H, W)
            if mask.sum() == 0:
                continue

            instance_masks.append(mask)
            category_labels.append(cat_id)

        if not instance_masks:
            # Edge case: image has annotations but all were filtered out
            # Return a dummy single-pixel mask so the processor doesn't crash
            instance_masks  = [np.zeros((H, W), dtype=np.uint8)]
            category_labels = [1]

        # Stack: shape (N, H, W)
        masks_array = np.stack(instance_masks, axis=0).astype(np.uint8)

        # HuggingFace processor expects:
        #   images            = PIL.Image or np.array
        #   segmentation_maps = list of per-image instance maps (H, W) with pixel = instance_id
        #   instance_id_to_semantic_id = {instance_id (1-based): category_id}
        instance_map = np.zeros((H, W), dtype=np.int32)
        instance_id_to_semantic_id = {}

        for i, (mask, cat_id) in enumerate(zip(instance_masks, category_labels), start=1):
            instance_map[mask > 0] = i
            instance_id_to_semantic_id[i] = cat_id

        encoding = self.processor(
            images=[image],
            segmentation_maps=[instance_map],
            instance_id_to_semantic_id=[instance_id_to_semantic_id],
            return_tensors="pt",
        )

        # Remove batch dimension added by processor (Trainer handles batching)
        return {k: v.squeeze(0) for k, v in encoding.items()}

    @staticmethod
    def _polygon_to_mask(segmentation, H: int, W: int) -> np.ndarray:
        """Convert COCO polygon segmentation to a binary mask."""
        import cv2
        mask = np.zeros((H, W), dtype=np.uint8)

        if isinstance(segmentation, list):
            for poly in segmentation:
                if len(poly) < 6:   # Need at least 3 points
                    continue
                pts = np.array(poly, dtype=np.float32).reshape(-1, 2)
                pts = pts.astype(np.int32)
                cv2.fillPoly(mask, [pts], color=1)

        elif isinstance(segmentation, dict):
            # RLE format (less common for floor plans, but handle gracefully)
            try:
                from pycocotools import mask as coco_mask
                rle = coco_mask.frPyObjects(segmentation, H, W)
                mask = coco_mask.decode(rle).astype(np.uint8)
            except ImportError:
                logger.warning("pycocotools not installed — RLE masks skipped")

        return mask


# ─────────────────────────────────────────────────────────────────────────────
# Collator
# ─────────────────────────────────────────────────────────────────────────────

def collate_fn(batch):
    """
    Stack samples from FloorPlanDataset into a single batch dictionary.
    Pads masks to the largest size in the batch.
    """
    pixel_values        = torch.stack([b["pixel_values"] for b in batch])
    pixel_mask          = torch.stack([b["pixel_mask"]   for b in batch])

    # mask_labels and class_labels are ragged (different N per image)
    # — keep them as lists; Mask2Former's forward() handles lists natively
    mask_labels  = [b["mask_labels"]  for b in batch]
    class_labels = [b["class_labels"] for b in batch]

    return {
        "pixel_values":  pixel_values,
        "pixel_mask":    pixel_mask,
        "mask_labels":   mask_labels,
        "class_labels":  class_labels,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main training entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Fine-tune Mask2Former on floor plan data")
    parser.add_argument("--dataset_dir",  default="./dataset",
                        help="Root folder containing train/ and val/ sub-directories")
    parser.add_argument("--output_dir",   default="./weights/mask2former-floorplan-finetuned",
                        help="Where to save checkpoints and the final model")
    parser.add_argument("--base_model",   default="facebook/mask2former-swin-large-coco-instance",
                        help="HuggingFace model ID to start from")
    parser.add_argument("--epochs",       type=int,   default=30)
    parser.add_argument("--batch_size",   type=int,   default=2,
                        help="Per-device batch size (use 1 if GPU memory < 16 GB)")
    parser.add_argument("--lr",           type=float, default=5e-5,
                        help="Learning rate")
    parser.add_argument("--warmup_ratio", type=float, default=0.05)
    parser.add_argument("--save_steps",   type=int,   default=200)
    parser.add_argument("--eval_steps",   type=int,   default=200)
    parser.add_argument("--fp16",         action="store_true",
                        help="Enable mixed-precision training (recommended for GPU)")
    return parser.parse_args()


def main():
    args = parse_args()

    dataset_dir = Path(args.dataset_dir)
    train_img   = dataset_dir / "train" / "images"
    train_ann   = dataset_dir / "train" / "annotations.json"
    val_img     = dataset_dir / "val"   / "images"
    val_ann     = dataset_dir / "val"   / "annotations.json"

    # Validate dataset paths exist
    for p in [train_img, train_ann, val_img, val_ann]:
        if not p.exists():
            raise FileNotFoundError(
                f"Expected path not found: {p}\n"
                "Please ensure your dataset is structured as:\n"
                "  dataset/train/images/  + annotations.json\n"
                "  dataset/val/images/    + annotations.json"
            )

    logger.info(f"Loading processor and model from: {args.base_model}")

    # ── Processor ────────────────────────────────────────────────────────────
    processor = Mask2FormerImageProcessor.from_pretrained(
        args.base_model,
        ignore_mismatched_sizes=True,
    )

    # ── Model ─────────────────────────────────────────────────────────────────
    # num_labels = 7 (our architectural classes, no background)
    # ignore_mismatched_sizes=True replaces the COCO classification head (80 classes)
    # with a fresh head sized for our 7 classes
    num_labels = len(LABEL2ID)
    model = Mask2FormerForUniversalSegmentation.from_pretrained(
        args.base_model,
        num_labels=num_labels,
        id2label=ID2LABEL,
        label2id=LABEL2ID,
        ignore_mismatched_sizes=True,
    )
    logger.info(f"Model ready — classification head replaced for {num_labels} classes")

    # ── Datasets ──────────────────────────────────────────────────────────────
    train_dataset = FloorPlanDataset(train_img, train_ann, processor)
    val_dataset   = FloorPlanDataset(val_img,   val_ann,   processor)

    # ── Training arguments ────────────────────────────────────────────────────
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=1,           # eval is memory-intensive
        learning_rate=args.lr,
        warmup_ratio=args.warmup_ratio,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        evaluation_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,                     # keep only 3 best checkpoints
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        fp16=args.fp16,
        dataloader_num_workers=2,
        logging_dir=os.path.join(args.output_dir, "logs"),
        logging_steps=50,
        report_to="none",                       # set to "wandb" or "tensorboard" if desired
        remove_unused_columns=False,            # required — our dataset returns custom keys
    )

    # ── Trainer ───────────────────────────────────────────────────────────────
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collate_fn,
        tokenizer=processor,                    # Trainer uses this for saving
    )

    # ── Train ─────────────────────────────────────────────────────────────────
    logger.info("Starting training...")
    trainer.train()

    # ── Save final model and processor ────────────────────────────────────────
    logger.info(f"Training complete. Saving final model to: {args.output_dir}")
    trainer.save_model(args.output_dir)
    processor.save_pretrained(args.output_dir)

    logger.info("=" * 60)
    logger.info("NEXT STEPS:")
    logger.info(f"1. In models/mask_rcnn_model.py, change model_id to:")
    logger.info(f"       model_id = \"{args.output_dir}\"")
    logger.info("2. In _map_to_project_classes, replace `return None` with:")
    logger.info("       valid_classes = {1, 2, 3, 4, 5, 6, 7}")
    logger.info("       return label_id if label_id in valid_classes else None")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
