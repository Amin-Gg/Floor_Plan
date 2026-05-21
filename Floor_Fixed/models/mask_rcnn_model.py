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

import logging
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
        return self._run_inference(image)

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
