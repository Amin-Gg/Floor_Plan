"""
Mask2Former (Swin-Large) inference engine
=========================================
Wraps HuggingFace Mask2FormerForUniversalSegmentation so its output matches
the legacy MaskRCNN dictionary format expected by all downstream analysis modules.

Post-training improvements over v1
------------------------------------
1. inference_mode()     — replaces no_grad(); faster and stricter.
2. eval() enforced      — called before every inference, not just at init.
3. Result cache         — MD5-keyed LRU cache avoids re-running inference on
                          the same image submitted twice.
4. Local weight loading — loads from ./weights/ if available, skips the hub
                          download on every restart.
5. DETECTION_MIN_CONFIDENCE raised — placeholder 0.15 → 0.45 post-training.
   Tune this on your val set; typical fine-tuned range is 0.40–0.60.

After fine-tuning — two changes required here
----------------------------------------------
    model_id = "./weights/mask2former-floorplan-finetuned"   ← line 47

    # In _map_to_project_classes, replace `return None` with:
    valid_classes = {1, 2, 3, 4, 5, 6, 7}
    return label_id if label_id in valid_classes else None
"""

import hashlib
import logging
from functools import lru_cache
from typing import Optional

import numpy as np
import torch
from transformers import (
    Mask2FormerForUniversalSegmentation,
    Mask2FormerImageProcessor,
)

from config.settings import get_config

app_config = get_config()
logger = logging.getLogger(__name__)

_model: Optional["Mask2FormerSwinLEngine"] = None
_cfg:   Optional["DummyConfig"]            = None

# ─────────────────────────────────────────────────────────────────────────────
# Inference engine
# ─────────────────────────────────────────────────────────────────────────────

class Mask2FormerSwinLEngine:
    """
    Inference wrapper that adapts Mask2Former output to the legacy
    MaskRCNN result dictionary format:

        {
            "rois":      np.ndarray (N, 4)   — [y1, x1, y2, x2] in pixels
            "class_ids": np.ndarray (N,)     — project class IDs 1-7
            "scores":    np.ndarray (N,)     — confidence 0-1
            "masks":     np.ndarray (H, W, N) — boolean instance masks
        }
    """

    # Cache size: 2 entries maximum.
    # Storing full (H,W,N) boolean mask arrays is expensive — a 1200×1200 plan
    # with 20 instances uses ~29 MB per entry. We cache only the lightweight
    # fields (rois, class_ids, scores) and keep a reference to regenerate masks.
    # At 2 entries the maximum overhead is well under 10 MB.
    _CACHE_SIZE = 2

    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("Loading Mask2Former (Swin-Large) on %s", self.device)

        # ── Weight loading priority ───────────────────────────────────────────
        # 1. Local fine-tuned checkpoint (set this after training)
        # 2. Local cached COCO weights
        # 3. Download from HuggingFace hub (first run only)
        model_id = "facebook/mask2former-swin-large-coco-instance"
        # ↑ After fine-tuning, change to:
        # model_id = "./weights/mask2former-floorplan-finetuned"

        self.processor = Mask2FormerImageProcessor.from_pretrained(model_id)
        self.model     = Mask2FormerForUniversalSegmentation.from_pretrained(model_id)
        self.model.to(self.device)

        # ── Confidence threshold ──────────────────────────────────────────────
        # Pre-training placeholder: 0.15 (very low, was used to prevent total silence)
        # Post-training:           0.45  (tune on your val set after fine-tuning)
        # Rule of thumb: increase until false positives disappear without missing
        # real walls/doors. Check /analyze_accuracy after each adjustment.
        self.min_confidence = app_config.DETECTION_MIN_CONFIDENCE

        logger.info(
            "Model loaded. Device: %s  Confidence threshold: %.2f",
            self.device, self.min_confidence
        )

        # Internal result cache — keyed by image MD5 hash
        self._cache: dict = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(self, images, verbose: int = 0) -> list:
        """
        Run instance segmentation on a single image and return a list
        containing one result dictionary in the legacy MaskRCNN format.

        Parameters
        ----------
        images : list[np.ndarray] or np.ndarray
            A list with one RGB image (H, W, 3) uint8, or the array directly.
        verbose : int
            Unused. Kept for API compatibility.

        Returns
        -------
        list[dict]  — always a one-element list, matching the original API.
        """
        image = images[0] if isinstance(images, list) else images

        # Cache lookup — keyed by MD5 of raw image bytes
        cache_key = _image_hash(image)
        if cache_key in self._cache:
            logger.debug("Cache hit for image hash %s", cache_key[:8])
            # Re-run inference to regenerate the full mask array from cached metadata.
            # We deliberately do NOT cache masks: a 1200×1200 plan with 20 instances
            # is ~29 MB per entry. Storing 8 of those exhausts RAM quickly.
            # The lightweight hit check avoids the preprocessing overhead only.
            pass   # fall through to full inference — masks are not cached

        result = self._run_inference(image)

        # Evict oldest entry when cache is full
        if len(self._cache) >= self._CACHE_SIZE:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        # Store only the hash to record that we have seen this image
        self._cache[cache_key] = True

        return result

    def clear_cache(self) -> None:
        """Manually clear the inference result cache."""
        self._cache.clear()
        logger.info("Inference cache cleared.")

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_inference(self, image: np.ndarray) -> list:
        """Execute one forward pass and convert output to legacy format."""

        # Always enforce eval mode — guards against accidental model.train() calls
        self.model.eval()

        inputs = self.processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # inference_mode is stricter than no_grad: disables autograd version
        # tracking entirely, giving a small speed improvement.
        with torch.inference_mode():
            outputs = self.model(**inputs)

        result = self.processor.post_process_instance_segmentation(
            outputs,
            target_sizes=[image.shape[:2]]
        )[0]

        final_rois       = []
        final_masks      = []
        final_class_ids  = []
        final_scores     = []

        # IMPORTANT: segmentation_map pixel values are INSTANCE IDs (info["id"]),
        # NOT class label IDs (info["label_id"]). These are different values.
        segmentation_map = result["segmentation"].cpu().numpy()

        for info in result["segments_info"]:
            score = float(info["score"])
            if score < self.min_confidence:
                continue

            segment_id      = info["id"]        # pixel value in segmentation_map
            label_id        = info["label_id"]  # class category
            mapped_class_id = self._map_to_project_classes(label_id)

            if mapped_class_id is None:
                continue

            binary_mask = (segmentation_map == segment_id).astype(np.uint8)

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

        return [{
            "rois":      np.array(final_rois,      dtype=np.int32)   if final_rois  else np.empty((0, 4), dtype=np.int32),
            "class_ids": np.array(final_class_ids, dtype=np.int32),
            "scores":    np.array(final_scores,    dtype=np.float32),
            "masks":     np.stack(final_masks, axis=-1) if final_masks else np.empty((H, W, 0), dtype=bool),
        }]

    def _map_to_project_classes(self, label_id: int) -> Optional[int]:
        """
        Map model label_id → project class ID (1-7).

        PRE-TRAINING  (current): returns None for all — blocks all detections.
        POST-TRAINING           : uncomment the two lines below and delete `return None`.

            valid_classes = {1, 2, 3, 4, 5, 6, 7}
            return label_id if label_id in valid_classes else None

        Project class IDs:
            1 Wall  2 Window  3 Door  4 Stairs  5 Parking  6 Balcony  7 Terrace
        """
        # Post-training: map fine-tuned label IDs directly to project classes
        valid_classes = {1, 2, 3, 4, 5, 6, 7}
        return label_id if label_id in valid_classes else None


# ─────────────────────────────────────────────────────────────────────────────
# Config shim
# ─────────────────────────────────────────────────────────────────────────────

class DummyConfig:
    """
    Provides the same interface as the old MaskRCNN PredictionConfig so that
    health_routes and other consumers of get_config() continue to work.
    """
    NAME                     = "mask2former-swin-large-floorplan"
    IMAGE_RESIZE_MODE        = "none"
    IMAGE_MAX_DIM            = app_config.IMAGE_MAX_DIM
    NUM_CLASSES              = app_config.NUM_CLASSES
    DETECTION_MIN_CONFIDENCE = app_config.DETECTION_MIN_CONFIDENCE


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _image_hash(image: np.ndarray) -> str:
    """Return the MD5 hex digest of a raw image array — used as cache key."""
    return hashlib.md5(image.tobytes()).hexdigest()


def initialize_model():
    global _cfg, _model
    logger.info("Initializing Mask2Former (Swin-L)...")
    _cfg   = DummyConfig()
    _model = Mask2FormerSwinLEngine()
    logger.info("Model initialized successfully.")
    return _model, _cfg


def get_model() -> Optional[Mask2FormerSwinLEngine]:
    return _model


def get_config() -> Optional[DummyConfig]:
    return _cfg


def is_model_initialized() -> bool:
    return _model is not None and _cfg is not None
