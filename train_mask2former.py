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
2. Gradient accumulation — effective batch of 16 without 16x GPU memory.
3. Differential LR      — backbone at lr/10, fresh classification head at lr.
                          Protects pretrained weights while learning new classes.
4. Best-checkpoint policy — saves by eval_loss during training (Mask2Former
                          loss cannot be replaced with mAP inline because the
                          HuggingFace Trainer API does not pass image sizes to
                          compute_metrics). Run evaluate.py after training to
                          compute real mAP@50 and pick the operating threshold.
5. Auto worker count    — dataloader workers sized to available CPU cores.
6. Resume support       — --resume_from_checkpoint to continue a crashed run.
7. Per-class IoU monitor — after every eval step, computes per-class semantic
                          IoU on a sample of val images and writes one row to
                          <output_dir>/per_class_iou.csv. Pure monitoring —
                          does not affect checkpoint selection. Disable with
                          --no_iou_logging or --iou_eval_images 0.

Note: CLASS_WEIGHTS (rare-class upweighting for terrace/balcony) is defined in
config/classes.py but is NOT yet applied to the loss. Implementing it requires
subclassing Trainer and overriding compute_loss(); this is planned for a later
iteration.

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

from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

from config.classes import (
    NAME_TO_TRAIN_ID  as LABEL2ID,
    TRAIN_ID_TO_NAME  as ID2LABEL,
    NUM_CLASSES,
    # CLASS_WEIGHTS is intentionally NOT imported — see the "Note" in the
    # module docstring above. It is defined in config/classes.py for future use.
    PROJECT_ID_TO_TRAIN_ID,
)

# Remove local redefinitions that were here before — they are now in config/classes.py

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Debug mode: PyTorch anomaly detection
# ─────────────────────────────────────────────────────────────────────────────
# When DEBUG_EAGER=true is set in the environment, PyTorch's autograd anomaly
# detection is enabled BEFORE the model is constructed. This makes NaN and
# Inf gradient errors traceable to the exact operation that produced them,
# at the cost of ~2-3x slower training.
#
# Use this when:
#   - eval_loss suddenly becomes NaN partway through training
#   - The loss explodes to inf in the first few steps
#   - You see "Function 'XXXBackward' returned nan values in its 0th output"
#
# DO NOT enable in production training runs — the slowdown is significant.
# Setting it via env var (not a CLI flag) is intentional: turning it on
# requires deliberate effort, so it can't accidentally remain enabled.
if os.getenv("DEBUG_EAGER", "false").lower() in ("true", "1", "yes"):
    torch.autograd.set_detect_anomaly(True)
    logger.warning(
        "DEBUG_EAGER=true — PyTorch anomaly detection enabled. "
        "Training will be ~2-3x slower. Unset DEBUG_EAGER for production runs."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Startup config consistency check
# ─────────────────────────────────────────────────────────────────────────────
# Validate that config/classes.py is internally consistent BEFORE allocating
# any GPU memory or loading the dataset. This catches the entire family of
# silent class-ID bugs (e.g. "terrace" never trains because TRAIN_ID_TO_NAME
# uses keys 0-6 but PROJECT_ID_TO_TRAIN_ID misses an entry) at import time
# rather than hours into a training run.
#
# If any assertion fails, training aborts immediately with a clear message
# pointing to config/classes.py — the only file the developer needs to fix.

def _assert_config_consistent() -> None:
    """Validate config/classes.py is internally consistent. Raises AssertionError on mismatch."""

    # 1. NUM_CLASSES must match the size of the training ID map.
    assert len(LABEL2ID) == NUM_CLASSES, (
        f"config/classes.py mismatch: NAME_TO_TRAIN_ID has {len(LABEL2ID)} entries "
        f"but NUM_CLASSES={NUM_CLASSES}. They MUST match. Fix config/classes.py."
    )
    assert len(ID2LABEL) == NUM_CLASSES, (
        f"config/classes.py mismatch: TRAIN_ID_TO_NAME has {len(ID2LABEL)} entries "
        f"but NUM_CLASSES={NUM_CLASSES}. They MUST match. Fix config/classes.py."
    )

    # 2. Training IDs MUST be a clean contiguous range 0..NUM_CLASSES-1.
    # Mask2Former (and any PyTorch classification head) requires this — a gap
    # at index N means the head has a dead neuron and rare classes go unlearned.
    expected_train_ids = set(range(NUM_CLASSES))
    actual_train_ids   = set(ID2LABEL.keys())
    assert actual_train_ids == expected_train_ids, (
        f"config/classes.py mismatch: TRAIN_ID_TO_NAME keys are not a clean "
        f"0..{NUM_CLASSES - 1} range. Expected {sorted(expected_train_ids)}, "
        f"got {sorted(actual_train_ids)}. Fix config/classes.py."
    )

    # 3. NAME_TO_TRAIN_ID must be the exact inverse of TRAIN_ID_TO_NAME.
    # If these disagree, training labels and inference labels will diverge silently.
    assert {v: k for k, v in ID2LABEL.items()} == LABEL2ID, (
        "config/classes.py mismatch: NAME_TO_TRAIN_ID is not the inverse of "
        "TRAIN_ID_TO_NAME. Fix config/classes.py — one of the two dicts is wrong."
    )

    # 4. PROJECT_ID_TO_TRAIN_ID values MUST cover all training IDs exactly once.
    # If a project ID is missing, that class never reaches the model. If a
    # project ID maps outside the training range, the model crashes at loss time.
    project_to_train_values = set(PROJECT_ID_TO_TRAIN_ID.values())
    assert project_to_train_values == expected_train_ids, (
        f"config/classes.py mismatch: PROJECT_ID_TO_TRAIN_ID values do not cover "
        f"the training range. Expected values {sorted(expected_train_ids)}, "
        f"got {sorted(project_to_train_values)}. Fix config/classes.py — a "
        f"project ID is either missing or mapped to an invalid training ID."
    )

    # 5. PROJECT_ID_TO_TRAIN_ID must be injective (no two project IDs map to
    # the same train ID). Duplicates here cause two annotation classes to
    # collapse into one model class without warning.
    assert len(PROJECT_ID_TO_TRAIN_ID) == len(project_to_train_values), (
        f"config/classes.py mismatch: PROJECT_ID_TO_TRAIN_ID has duplicate values. "
        f"Two project IDs map to the same training ID — fix config/classes.py."
    )

    logger.info(
        "Config consistency check PASSED: %d classes, training IDs 0..%d",
        NUM_CLASSES, NUM_CLASSES - 1
    )
    logger.info("  Classes: %s", ", ".join(f"{tid}={name}" for tid, name in sorted(ID2LABEL.items())))


_assert_config_consistent()


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
        Apply augmentation to the PIL image and the integer instance_map.

        We use a single packed instance_map (one int32 array) rather than N
        separate binary masks to keep memory O(1) per image. Holding N separate
        full-resolution masks would cost N x H x W bytes — for a dense floor
        plan with 200+ instances at 1000x800 px that is ~160 MB per image,
        which kills the DataLoader worker process on Colab.

        The packed map is augmented geometrically alongside the image (same
        rotation / flip / scale), preserving which pixels belong to which
        instance ID. Per-instance binary masks are extracted one at a time
        after augmentation in __getitem__, so memory stays bounded.

        Parameters
        ----------
        image        : PIL.Image.Image  (RGB)
        instance_map : np.ndarray (H, W) int32 — instance IDs per pixel (0 = background)

        Returns
        -------
        (augmented_image, augmented_instance_map)
        """
        # -- 90 degree rotation ----------------------------------------------
        if random.random() < self.rotate_prob:
            k = random.choice([1, 2, 3])           # 90, 180, 270 degrees
            image        = image.rotate(k * 90, expand=True)
            instance_map = np.rot90(instance_map, k=k).copy()

        # -- Horizontal flip --------------------------------------------------
        if random.random() < self.flip_h_prob:
            image        = image.transpose(Image.FLIP_LEFT_RIGHT)
            instance_map = np.fliplr(instance_map).copy()

        # -- Vertical flip ----------------------------------------------------
        if random.random() < self.flip_v_prob:
            image        = image.transpose(Image.FLIP_TOP_BOTTOM)
            instance_map = np.flipud(instance_map).copy()

        # -- Brightness / contrast jitter (image only) ------------------------
        if random.random() < 0.5:
            factor = random.uniform(*self.brightness_range)
            image  = ImageEnhance.Brightness(image).enhance(factor)
        if random.random() < 0.3:
            factor = random.uniform(0.8, 1.2)
            image  = ImageEnhance.Contrast(image).enhance(factor)

        # -- Scale jitter -----------------------------------------------------
        if random.random() < 0.4:
            scale = random.uniform(*self.scale_range)
            orig_w, orig_h = image.size
            new_w = max(64, int(orig_w * scale))
            new_h = max(64, int(orig_h * scale))
            image        = image.resize((new_w, new_h), Image.BILINEAR)
            instance_map = cv2.resize(
                instance_map, (new_w, new_h),
                interpolation=cv2.INTER_NEAREST   # nearest preserves integer IDs
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
            # project class ID (1..NUM_CLASSES). Filter here so PyTorch DataLoader
            # never receives an invalid sample — DataLoader does not skip
            # exceptions. Membership is checked against PROJECT_ID_TO_TRAIN_ID so
            # the valid set tracks config/classes.py automatically.
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

        # Build the per-instance mask list and matching class labels.
        instance_masks:  List[np.ndarray] = []
        category_labels: List[int]        = []

        # Validate against PROJECT_ID_TO_TRAIN_ID (keys: project IDs 1..NUM_CLASSES).
        # Store training IDs (0-indexed) directly in category_labels so the
        # project-id -> train-id conversion is done once, here.
        for ann in annotations:
            project_id = ann["category_id"]
            train_id   = PROJECT_ID_TO_TRAIN_ID.get(project_id)
            if train_id is None:
                # Unknown class (not one of our configured classes) — skip it
                continue
            mask = _polygon_to_mask(ann["segmentation"], H, W)
            if mask.sum() == 0:
                continue
            instance_masks.append(mask)
            category_labels.append(train_id)   # training ID (0-indexed), not project ID

        # This should not happen because __init__ already filtered valid images,
        # but guard defensively in case the dataset changes between init and getitem.
        if not instance_masks:
            raise RuntimeError(
                f"Image {image_id} has no valid annotations after filtering. "
                "This should have been caught during dataset initialization."
            )

        # ── Pack per-polygon masks into one instance_map for augmentation ────
        # Using a single packed int32 array (one instance ID per pixel) keeps
        # memory O(1) per image regardless of instance count.  Holding N
        # separate full-resolution masks would cost N x H x W bytes — ~160 MB
        # per image for a dense 200-instance plan at 1000x800 px — which kills
        # the DataLoader worker on Colab (and wastes RAM on the supercomputer).
        #
        # Trade-off: pixels where two annotations overlap are assigned to the
        # later annotation (last-writer-wins). In CubiCasa, most overlap is at
        # thin wall-boundary pixels and has negligible effect on training quality
        # versus the alternative of crashing with OOM.
        instance_map = np.zeros((H, W), dtype=np.int32)
        instance_id_to_semantic_id: Dict[int, int] = {}
        for i, (mask, train_id) in enumerate(zip(instance_masks, category_labels), start=1):
            instance_map[mask > 0] = i
            instance_id_to_semantic_id[i] = train_id

        # ── Augmentation (image + packed map get identical spatial transforms) -
        if self.augmenter is not None:
            image, instance_map = self.augmenter(image, instance_map)

        # ── Image preprocessing via the processor (image ONLY) ────────────────
        # We do NOT pass segmentation_maps to the processor: it converts them
        # to a uint8 PIL image internally, which overflows when instance IDs
        # exceed 255 (a dense floor plan easily has 300+ instances).
        # Instead we resize each binary mask ourselves to the processor's
        # output size — binary masks are 0/1 and never overflow uint8.
        image_encoding = self.processor(
            images=[image],
            return_tensors="pt",
        )
        pixel_values = image_encoding["pixel_values"].squeeze(0)   # (C, Hp, Wp)
        pixel_mask   = image_encoding["pixel_mask"].squeeze(0)     # (Hp, Wp)
        target_h, target_w = pixel_values.shape[-2:]

        # ── Extract per-instance binary masks one at a time ───────────────────
        # We iterate over unique IDs in the augmented instance_map and extract
        # one binary mask at a time, resize it, then discard it — so at most
        # two full-resolution arrays (instance_map + one binary mask) are live
        # simultaneously. This keeps peak memory low regardless of N.
        mask_label_list: List[np.ndarray] = []
        class_label_list: List[int]       = []
        for inst_id in np.unique(instance_map):
            if inst_id == 0:
                continue   # background
            binary = (instance_map == inst_id).astype(np.uint8)
            binary_resized = cv2.resize(
                binary, (target_w, target_h), interpolation=cv2.INTER_NEAREST
            )
            if binary_resized.sum() == 0:
                continue   # instance shrank to nothing after resize
            mask_label_list.append(binary_resized)
            class_label_list.append(instance_id_to_semantic_id[int(inst_id)])

        if not mask_label_list:
            raise RuntimeError(
                f"Image {image_id} has no instances left after resize to "
                f"({target_h},{target_w}). Original instance count: "
                f"{len(np.unique(instance_map)) - 1}."
            )

        mask_labels  = torch.from_numpy(np.stack(mask_label_list)).float()  # (N, Hp, Wp)
        class_labels = torch.tensor(class_label_list, dtype=torch.int64)    # (N,)

        return {
            "pixel_values": pixel_values,
            "pixel_mask":   pixel_mask,
            "mask_labels":  mask_labels,
            "class_labels": class_labels,
        }


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
# Per-class IoU monitoring callback
# ─────────────────────────────────────────────────────────────────────────────
# Sidecar that computes per-class IoU on a subset of the val set after every
# Trainer evaluation step. This is MONITORING ONLY — it does not influence
# checkpoint selection (Trainer still uses eval_loss for that).
#
# Why this exists:
# eval_loss can decrease while rare classes (terrace, balcony) silently get
# zero IoU. Without this sidecar, the team has no way to see that until they
# run evaluate.py at the end of training — when it's too late to course-correct.
#
# Cost:
# Defaults to 50 val images per eval step. With eval_steps=100 and a 50-image
# pass adding ~10-15 seconds, this adds <2% to total training time on GPU.
# Set --iou_eval_images 0 (or --no_iou_logging) to disable entirely.
#
# Output:
# - Per-class IoU printed to the logger after every evaluation
# - Appended row in <output_dir>/per_class_iou.csv (one row per eval step)
#
# Safety:
# All work is wrapped in try/except. If anything fails, a warning is logged
# and training continues — monitoring code must NEVER crash training.

class PerClassIoUCallback(TrainerCallback):
    """Compute per-class semantic IoU on a val subset after each Trainer eval."""

    def __init__(self,
                 processor: Mask2FormerImageProcessor,
                 val_dataset: Dataset,
                 output_dir: str,
                 num_eval_images: int,
                 num_classes: int,
                 id2label: Dict[int, str]):
        self.processor       = processor
        self.val_dataset     = val_dataset
        self.output_dir      = Path(output_dir)
        self.num_classes     = num_classes
        self.id2label        = id2label
        # Cap sample size to dataset size; 0 disables the callback's work
        self.num_eval_images = min(max(0, num_eval_images), len(val_dataset))
        self.csv_path        = self.output_dir / "per_class_iou.csv"
        # Deterministic image sampling so the IoU trace is comparable across steps
        self._rng            = random.Random(42)

        logger.info(
            "PerClassIoUCallback active: %d val images per eval, CSV → %s",
            self.num_eval_images, self.csv_path
        )

    def on_evaluate(self, args, state, control, model=None, **kwargs):
        if model is None or self.num_eval_images == 0:
            return
        try:
            self._compute_and_log(model, state)
        except Exception as exc:
            # Never crash training because of monitoring code.
            logger.warning(
                "Per-class IoU monitoring failed at step %d (training continues): %s",
                state.global_step, exc, exc_info=True
            )

    # ── Implementation ────────────────────────────────────────────────────────

    def _compute_and_log(self, model, state) -> None:
        indices = self._rng.sample(
            range(len(self.val_dataset)), self.num_eval_images
        )

        # Accumulate pixel intersect/union per class across all sampled images
        intersect = np.zeros(self.num_classes, dtype=np.int64)
        union     = np.zeros(self.num_classes, dtype=np.int64)

        device       = next(model.parameters()).device
        was_training = model.training
        model.eval()

        try:
            for idx in indices:
                sample = self.val_dataset[idx]
                pixel_values = sample["pixel_values"].unsqueeze(0).to(device)

                gt_semantic = self._build_gt_semantic(sample)
                if gt_semantic is None:
                    continue   # skip image with no usable GT
                H, W = gt_semantic.shape

                with torch.inference_mode():
                    outputs = model(pixel_values=pixel_values)

                result = self.processor.post_process_instance_segmentation(
                    outputs, target_sizes=[(H, W)]
                )[0]
                pred_semantic = self._build_pred_semantic(result, H, W)

                for c in range(self.num_classes):
                    gt_c   = (gt_semantic   == c)
                    pred_c = (pred_semantic == c)
                    intersect[c] += int(np.logical_and(gt_c, pred_c).sum())
                    union[c]     += int(np.logical_or (gt_c, pred_c).sum())
        finally:
            if was_training:
                model.train()

        # Per-class IoU = intersect / union; NaN if class absent from sample
        iou_per_class = np.full(self.num_classes, np.nan, dtype=np.float64)
        for c in range(self.num_classes):
            if union[c] > 0:
                iou_per_class[c] = intersect[c] / union[c]

        mean_iou = float(np.nanmean(iou_per_class)) if np.any(~np.isnan(iou_per_class)) else float("nan")

        # Console output: one block per eval step
        logger.info(
            "──── Per-class IoU @ step %d (epoch %.2f, %d val images) ────",
            state.global_step, state.epoch or 0.0, self.num_eval_images
        )
        for c in range(self.num_classes):
            name    = self.id2label.get(c, str(c))
            iou_str = "no GT" if np.isnan(iou_per_class[c]) else f"{iou_per_class[c]:.4f}"
            logger.info("  %-12s  %s", name, iou_str)
        logger.info("  %-12s  %.4f", "MEAN", mean_iou)

        self._append_csv(state, iou_per_class, mean_iou)

    def _build_gt_semantic(self, sample: dict) -> Optional[np.ndarray]:
        """
        Collapse per-instance GT masks into a single semantic map.
        Returns an HxW int64 array with values in [0, num_classes-1] or -1 for background.
        Returns None if the sample has no instances (shouldn't happen — dataset filters these).
        """
        mask_labels  = sample.get("mask_labels")
        class_labels = sample.get("class_labels")
        if mask_labels is None or class_labels is None or len(mask_labels) == 0:
            return None

        H, W = mask_labels.shape[-2:]
        semantic = np.full((H, W), -1, dtype=np.int64)
        for i in range(len(class_labels)):
            cls = int(class_labels[i])
            if cls < 0 or cls >= self.num_classes:
                continue   # defensive: skip out-of-range labels
            m = mask_labels[i].cpu().numpy().astype(bool)
            semantic[m] = cls
        return semantic

    def _build_pred_semantic(self, result: dict, H: int, W: int) -> np.ndarray:
        """
        Collapse instance predictions to a single semantic map at (H, W).
        Background pixels stay -1.
        """
        seg_map       = result["segmentation"].cpu().numpy()
        segments_info = result.get("segments_info", [])

        semantic = np.full((H, W), -1, dtype=np.int64)
        for info in segments_info:
            seg_id = info["id"]
            cls    = int(info["label_id"])
            if 0 <= cls < self.num_classes:
                semantic[seg_map == seg_id] = cls
        return semantic

    def _append_csv(self, state, iou_per_class: np.ndarray, mean_iou: float) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        write_header = not self.csv_path.exists()

        with open(self.csv_path, "a", encoding="utf-8") as f:
            if write_header:
                cols = ["step", "epoch"]
                cols += [f"iou_{self.id2label.get(c, str(c))}" for c in range(self.num_classes)]
                cols += ["mean_iou"]
                f.write(",".join(cols) + "\n")

            row  = [str(state.global_step), f"{state.epoch or 0.0:.4f}"]
            row += [f"{iou:.6f}" if not np.isnan(iou) else "" for iou in iou_per_class]
            row.append(f"{mean_iou:.6f}" if not np.isnan(mean_iou) else "")
            f.write(",".join(row) + "\n")


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
    parser.add_argument("--iou_eval_images", type=int, default=50,
                        help="Number of val images used by the per-class IoU monitor "
                             "after every eval step. Default 50. Set to 0 to disable.")
    parser.add_argument("--no_iou_logging", action="store_true",
                        help="Disable per-class IoU monitoring entirely. Equivalent to "
                             "--iou_eval_images 0.")
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
                f"If running Section 9 alone in Colab, re-run Section 7 first "
                f"to regenerate the COCO dataset on local disk."
            )

    effective_batch = args.batch_size * args.grad_accum
    logger.info(
        "Effective batch size: %d  (%d per-device × %d grad_accum steps)",
        effective_batch, args.batch_size, args.grad_accum
    )

    # ── Processor ────────────────────────────────────────────────────────
    # We use the processor for IMAGE preprocessing only (resize, rescale,
    # normalize). We deliberately do NOT pass segmentation_maps to it in
    # __getitem__ — instead we build mask_labels/class_labels by hand. This
    # avoids the processor's uint8 segmentation-map path, which overflows
    # when a floor plan has more than 255 instances (a dense plan easily has
    # 300+ wall/door/window segments). See FloorPlanDataset.__getitem__.
    #
    # ignore_index and do_reduce_labels are kept at safe values for any
    # incidental use, but they have no effect on our image-only call path.
    processor = Mask2FormerImageProcessor.from_pretrained(
        args.base_model,
        ignore_index=0,
        do_reduce_labels=False,
    )

    # ── Model ─────────────────────────────────────────────────────────────────
    # NUM_CLASSES comes from config/classes.py — single source of truth.
    # Training head indices are 0-indexed (0..NUM_CLASSES-1), which is correct
    # for num_labels=NUM_CLASSES. As of writing this is 15 (7 structural +
    # 6 room types + 2 safety/storage); the code reads the live value.
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

    # ── Auto-cap eval_steps / save_steps to total training steps ─────────────
    # With a small smoke-test dataset (e.g. 50 plans, 2 epochs) the total
    # number of optimizer steps can be as low as 6. If eval_steps=100 (the
    # default), evaluation never triggers and the IoU CSV is never written.
    # We cap both to max(1, total_steps // 2) so at least one eval and one
    # checkpoint always occur regardless of dataset size.
    steps_per_epoch = max(1, len(train_dataset) //
                          (args.batch_size * args.grad_accum))
    total_steps     = steps_per_epoch * args.epochs
    effective_eval_steps = min(args.eval_steps, max(1, total_steps // 2))
    effective_save_steps = min(args.save_steps, max(1, total_steps // 2))
    if effective_eval_steps < args.eval_steps:
        logger.warning(
            "eval_steps=%d exceeds half of total_steps=%d — "
            "auto-capped to %d so evaluation triggers at least once.",
            args.eval_steps, total_steps, effective_eval_steps,
        )
    if effective_save_steps < args.save_steps:
        logger.warning(
            "save_steps=%d exceeds half of total_steps=%d — "
            "auto-capped to %d so a checkpoint is always saved.",
            args.save_steps, total_steps, effective_save_steps,
        )

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
        eval_steps=effective_eval_steps,
        save_strategy="steps",
        save_steps=effective_save_steps,
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

    # ── Monitoring callbacks ──────────────────────────────────────────────────
    # Per-class IoU is a SIDECAR — checkpoint selection still uses eval_loss
    # (set in TrainingArguments above). This just gives us visibility into
    # whether rare classes are learning. --no_iou_logging or
    # --iou_eval_images 0 disables it.
    callbacks: List[TrainerCallback] = []
    iou_images = 0 if args.no_iou_logging else args.iou_eval_images
    if iou_images > 0:
        callbacks.append(PerClassIoUCallback(
            processor       = processor,
            val_dataset     = val_dataset,
            output_dir      = args.output_dir,
            num_eval_images = iou_images,
            num_classes     = NUM_CLASSES,
            id2label        = ID2LABEL,
        ))

    # ── Trainer ───────────────────────────────────────────────────────────────
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        data_collator=collate_fn,
        tokenizer=processor,
        optimizers=(optimizer, None),   # None = Trainer builds the LR scheduler
        callbacks=callbacks,
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
