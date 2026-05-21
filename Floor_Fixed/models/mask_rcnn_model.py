"""
Mask2Former (Swin-Large) inference engine
=========================================
Wraps HuggingFace Mask2FormerForUniversalSegmentation so its output matches
the legacy MaskRCNN dictionary format expected by all downstream analysis modules.

Key design points
-----------------
1. inference_mode()  — replaces no_grad(); faster and stricter.
2. eval() enforced   — called before every forward pass, not just at init.
3. No result cache   — users upload different images each time; a cache adds
                       complexity with negligible hit rate. detect() calls
                       _run_inference() directly.
4. Local weights     — loads from ./weights/ after fine-tuning; falls back to
                       the HuggingFace hub on first run.
5. Confidence 0.45   — post-training threshold. Tune with evaluate.py
                       --find_best_threshold on your validation set.

After fine-tuning — two changes required in this file
------------------------------------------------------
    1. Set model_id (line ~68):
           model_id = "./weights/mask2former-floorplan-finetuned"

    2. In _map_to_project_classes, the body is already active:
           valid_classes = {1, 2, 3, 4, 5, 6, 7}
           return label_id if label_id in valid_classes else None
"""

import os
import logging
from typing import Optional

import numpy as np
import torch
from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
)

from config.settings import get_config
from config.classes import TRAIN_ID_TO_PROJECT_ID, TRAIN_ID_TO_NAME

app_config = get_config()
logger = logging.getLogger(__name__)

_model: Optional["Mask2FormerSwinLEngine"] = None
_cfg:   Optional["DummyConfig"]            = None

# ── Model path resolution ─────────────────────────────────────────────────────
# Set FLOORPLAN_MODEL_PATH to your fine-tuned checkpoint directory.
# In production, ALLOW_COCO_FALLBACK must be "false" — the server will refuse
# to start without a real floor plan checkpoint.
_ENV_MODEL_PATH    = os.getenv("FLOORPLAN_MODEL_PATH", "")
_ALLOW_COCO_FALLBACK = os.getenv("ALLOW_COCO_FALLBACK", "true").lower() == "true"
_COCO_BASE_MODEL   = "facebook/mask2former-swin-large-coco-instance"


def _resolve_model_id() -> str:
    """
    Return the model path to load.
    Fails fast in production (APP_ENV=production) if no fine-tuned checkpoint
    is configured and ALLOW_COCO_FALLBACK is false.
    """
    if _ENV_MODEL_PATH and os.path.isdir(_ENV_MODEL_PATH):
        logger.info("Loading fine-tuned floor plan model from: %s", _ENV_MODEL_PATH)
        return _ENV_MODEL_PATH

    if _ENV_MODEL_PATH and not os.path.isdir(_ENV_MODEL_PATH):
        raise FileNotFoundError(
            f"FLOORPLAN_MODEL_PATH is set to '{_ENV_MODEL_PATH}' "
            "but that directory does not exist. "
            "Run train_mask2former.py first, then set the path."
        )

    # No env var set — fall back to local weights directory if it exists
    local_weights = "./weights/mask2former-floorplan-finetuned"
    if os.path.isdir(local_weights):
        logger.info("Loading fine-tuned model from default path: %s", local_weights)
        return local_weights

    # No fine-tuned model anywhere
    if not _ALLOW_COCO_FALLBACK:
        raise RuntimeError(
            "No fine-tuned floor plan model found and ALLOW_COCO_FALLBACK=false. "
            "Set FLOORPLAN_MODEL_PATH to your trained checkpoint directory, "
            "or set ALLOW_COCO_FALLBACK=true for development/testing only."
        )

    logger.warning(
        "No fine-tuned checkpoint found. Falling back to generic COCO model. "
        "Detections will be unreliable for floor plan elements. "
        "Set FLOORPLAN_MODEL_PATH after training to fix this."
    )
    return _COCO_BASE_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# Inference engine
# ─────────────────────────────────────────────────────────────────────────────

class Mask2FormerSwinLEngine:
    """
    Inference wrapper that adapts Mask2Former output to the legacy
    MaskRCNN result dictionary format:

        {
            "rois":      np.ndarray (N, 4)    — [y1, x1, y2, x2] in pixels
            "class_ids": np.ndarray (N,)      — project class IDs 1-7
            "scores":    np.ndarray (N,)      — confidence 0.0-1.0
            "masks":     np.ndarray (H, W, N) — boolean instance masks
        }
    """

    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("Loading Mask2Former (Swin-Large) on %s", self.device)

        model_id = _resolve_model_id()
        self.processor = Mask2FormerImageProcessor.from_pretrained(model_id)
        self.model     = Mask2FormerForUniversalSegmentation.from_pretrained(model_id)
        self.model.to(self.device)

        # Post-training threshold: 0.45
        # Pre-training placeholder was 0.15 — raise this after fine-tuning.
        # Run: python evaluate.py --find_best_threshold  to find the optimal value.
        self.min_confidence = app_config.DETECTION_MIN_CONFIDENCE

        logger.info(
            "Model loaded. Device: %s  Confidence threshold: %.2f",
            self.device, self.min_confidence
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(self, images, verbose: int = 0) -> list:
        """
        Run instance segmentation on a single image.

        Parameters
        ----------
        images : list[np.ndarray] or np.ndarray
            A list containing one RGB image (H, W, 3) uint8, or the array directly.
        verbose : int
            Unused. Kept for API compatibility with legacy MaskRCNN call sites.

        Returns
        -------
        list[dict]
            Always a one-element list containing the result dictionary, matching
            the original MaskRCNN API so all downstream code requires no changes.
        """
        image = images[0] if isinstance(images, list) else images
        return self._run_inference(image)

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_inference(self, image: np.ndarray) -> list:
        """
        Execute one Mask2Former forward pass and convert the output to the
        legacy result dictionary format.

        Steps
        -----
        1. Enforce eval mode — guards against accidental model.train() calls
           between requests which would activate dropout and produce wrong results.
        2. Preprocess the image with the HuggingFace processor.
        3. Run the forward pass under inference_mode() for maximum speed.
        4. Post-process to restore masks to the original image resolution.
        5. Filter by confidence threshold.
        6. Map model label_ids to project class IDs (1-7).
        7. Extract binary masks using segment instance IDs (not class label IDs).
        8. Compute bounding boxes from mask pixel coordinates.
        9. Stack masks to (H, W, N) shape expected by all analysis modules.
        """
        # Step 1: enforce eval mode
        self.model.eval()

        # Step 2: preprocess
        inputs = self.processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Step 3: forward pass
        # inference_mode() is stricter than no_grad() — it disables autograd
        # version tracking entirely, giving a small additional speed improvement.
        with torch.inference_mode():
            outputs = self.model(**inputs)

        # Step 4: post-process — restores masks to original image dimensions
        result = self.processor.post_process_instance_segmentation(
            outputs,
            target_sizes=[image.shape[:2]]
        )[0]

        final_rois      = []
        final_masks     = []
        final_class_ids = []
        final_scores    = []

        # IMPORTANT: segmentation_map pixel values are INSTANCE IDs (info["id"]),
        # NOT class label IDs (info["label_id"]). These are different values.
        # Using label_id to index the map produces empty or completely wrong masks.
        segmentation_map = result["segmentation"].cpu().numpy()

        for info in result["segments_info"]:
            score = float(info["score"])

            # Step 5: confidence filter
            if score < self.min_confidence:
                continue

            # Step 6: map to project classes
            segment_id      = info["id"]        # instance ID — pixel value in map
            label_id        = info["label_id"]  # class category ID
            mapped_class_id = self._map_to_project_classes(label_id)
            if mapped_class_id is None:
                continue

            # Step 7: extract binary mask for this specific instance
            binary_mask = (segmentation_map == segment_id).astype(np.uint8)

            # Step 8: bounding box from mask pixel coordinates
            y_idx, x_idx = np.where(binary_mask)
            if len(y_idx) == 0:
                continue

            final_rois.append([
                int(np.min(y_idx)), int(np.min(x_idx)),
                int(np.max(y_idx)), int(np.max(x_idx)),
            ])
            final_class_ids.append(mapped_class_id)
            final_scores.append(score)
            final_masks.append(binary_mask.astype(bool))

        H, W = image.shape[:2]

        # Step 9: stack masks to (H, W, N) — shape expected by all downstream modules
        if final_masks:
            masks_stack = np.stack(final_masks, axis=-1)
        else:
            masks_stack = np.empty((H, W, 0), dtype=bool)

        return [{
            "rois":      np.array(final_rois,      dtype=np.int32)   if final_rois  else np.empty((0, 4), dtype=np.int32),
            "class_ids": np.array(final_class_ids, dtype=np.int32),
            "scores":    np.array(final_scores,    dtype=np.float32),
            "masks":     masks_stack,
        }]

    def _map_to_project_classes(self, label_id: int) -> Optional[int]:
        """
        Convert the model's output training ID (0-6) to a project class ID (1-7).

        Training IDs are 0-indexed (0=wall … 6=terrace).
        Project IDs are 1-indexed (1=wall … 7=terrace).
        The mapping is defined in config/classes.py — do not modify it here.

        Returns None for any ID outside the known range (background, noise).
        """
        return TRAIN_ID_TO_PROJECT_ID.get(label_id)


# ─────────────────────────────────────────────────────────────────────────────
# Config shim
# ─────────────────────────────────────────────────────────────────────────────

class DummyConfig:
    """
    Provides the same interface as the old MaskRCNN PredictionConfig so that
    health_routes and other consumers of get_config() continue to work unchanged.
    """
    NAME                     = "mask2former-swin-large-floorplan"
    IMAGE_RESIZE_MODE        = "none"
    IMAGE_MAX_DIM            = app_config.IMAGE_MAX_DIM
    NUM_CLASSES              = app_config.NUM_CLASSES
    DETECTION_MIN_CONFIDENCE = app_config.DETECTION_MIN_CONFIDENCE


# ─────────────────────────────────────────────────────────────────────────────
# Module-level lifecycle functions
# ─────────────────────────────────────────────────────────────────────────────

def initialize_model():
    """Load the model and config into module-level globals. Call once at startup."""
    global _cfg, _model
    logger.info("Initializing Mask2Former (Swin-L)...")
    _cfg   = DummyConfig()
    _model = Mask2FormerSwinLEngine()
    logger.info("Model initialized successfully.")
    return _model, _cfg


def get_model() -> Optional[Mask2FormerSwinLEngine]:
    """Return the initialized model, or None if not yet initialized."""
    return _model


def get_config() -> Optional[DummyConfig]:
    """Return the model configuration shim, or None if not yet initialized."""
    return _cfg


def is_model_initialized() -> bool:
    """Return True if both model and config are loaded and ready."""
    return _model is not None and _cfg is not None
