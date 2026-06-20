"""
models/mask_rcnn_model.py
=========================
Mask R-CNN inference engine (Keras / TensorFlow, Matterport implementation).

This REPLACES the Mask2Former wrapper. It loads the pre-trained floor-plan
weights (`maskrcnn_15_epochs.h5`) and exposes the SAME module interface the
rest of the project already imports, so no other file needs to change:

    initialize_model()      -> load weights into module globals (call once at startup)
    get_model()             -> the loaded MaskRCNN model (has .detect())
    get_model_config()      -> the PredictionConfig instance (used by /health)
    is_model_initialized()  -> bool

The model's native .detect([image], verbose=0) already returns the exact
result dictionary every downstream module expects:

    [{ "rois": (N,4), "class_ids": (N,), "scores": (N,), "masks": (H,W,N) }]

so the analysis / BIM / IFC layers keep working untouched.
"""

import os
import logging

from mrcnn.config import Config
from mrcnn.model import MaskRCNN

from config.settings import get_config
from config.constants import ROOT_DIR

app_config = get_config()
logger = logging.getLogger(__name__)

# ── Module-level singletons ───────────────────────────────────────────────────
_model = None
_cfg = None


# ─────────────────────────────────────────────────────────────────────────────
# Inference configuration
# ─────────────────────────────────────────────────────────────────────────────
class PredictionConfig(Config):
    """
    Mask R-CNN inference configuration.

    NUM_CLASSES MUST match the value the weights were trained with (4 =
    background + wall + window + door). Any other value makes the classifier /
    mask heads shape-mismatch the .h5 and load_weights(by_name=True) silently
    skips them, producing garbage detections.
    """
    NAME                     = app_config.MODEL_NAME
    NUM_CLASSES              = app_config.NUM_CLASSES          # 4
    GPU_COUNT                = app_config.GPU_COUNT            # 1
    IMAGES_PER_GPU           = app_config.IMAGES_PER_GPU       # 1
    DETECTION_MIN_CONFIDENCE = app_config.DETECTION_MIN_CONFIDENCE
    # Image scaling — match the resolution the .h5 was validated at.
    IMAGE_MAX_DIM            = app_config.IMAGE_MAX_DIM        # 1024 (divisible by 64)
    IMAGE_MIN_DIM            = app_config.IMAGE_MIN_DIM        # 800
    # Output cap + NMS (see config/settings.py for rationale).
    DETECTION_MAX_INSTANCES  = app_config.DETECTION_MAX_INSTANCES
    DETECTION_NMS_THRESHOLD  = app_config.DETECTION_NMS_THRESHOLD


def _resolve_weights_path() -> str:
    """
    Resolve the weights file path relative to the project root, so it works
    regardless of the current working directory (important under gunicorn).
    """
    folder = app_config.WEIGHTS_FOLDER
    if not os.path.isabs(folder):
        # strip a leading "./" then anchor to ROOT_DIR
        folder = os.path.join(ROOT_DIR, folder.lstrip("./").lstrip(".\\"))
    return os.path.join(folder, app_config.WEIGHTS_FILE_NAME)


def initialize_model():
    """Load the Mask R-CNN model + weights once at startup. Returns (model, cfg)."""
    global _cfg, _model

    try:
        weights_path = _resolve_weights_path()
        if not os.path.exists(weights_path):
            raise FileNotFoundError(
                f"Weights file not found: {weights_path}\n"
                f"Download 'maskrcnn_15_epochs.h5' (~244 MB) and place it in the "
                f"weights/ folder. See weights/README for the source link."
            )

        # Keras log directory (used by MaskRCNN for inference-mode bookkeeping).
        model_dir = os.path.join(ROOT_DIR, "logs")
        os.makedirs(model_dir, exist_ok=True)

        _cfg = PredictionConfig()
        logger.info("Mask R-CNN config: resize_mode=%s  max_dim=%s  num_classes=%s",
                    _cfg.IMAGE_RESIZE_MODE, _cfg.IMAGE_MAX_DIM, _cfg.NUM_CLASSES)
        logger.info("============== Initializing Mask R-CNN model ==============")

        _model = MaskRCNN(mode="inference", model_dir=model_dir, config=_cfg)
        logger.info("================= Model created =================")

        _model.load_weights(weights_path, by_name=True)
        logger.info("================= Weights loaded: %s =================", weights_path)

        return _model, _cfg

    except Exception as exc:
        logger.error("Error initializing Mask R-CNN model: %s", exc, exc_info=True)
        raise


def get_model():
    """Return the loaded MaskRCNN model, or None if not yet initialized."""
    return _model


def get_model_config():
    """Return the PredictionConfig instance, or None if not yet initialized."""
    return _cfg


# Backwards-compatible alias (some older call sites used get_config()).
def get_config():
    """Alias of get_model_config() for compatibility."""
    return _cfg


def is_model_initialized() -> bool:
    """Return True once both model and config are loaded and ready."""
    return _model is not None and _cfg is not None