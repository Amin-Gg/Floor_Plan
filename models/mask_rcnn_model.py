"""Lazy Mask R-CNN model lifecycle.

The HTTP/OpenAPI surface must remain importable when TensorFlow or the external
weights are unavailable. Heavy Matterport/TensorFlow imports therefore happen
only inside :func:`initialize_model`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from config.constants import ROOT_DIR
from config.settings import get_config as get_app_config

app_config = get_app_config()
logger = logging.getLogger(__name__)

_model: Any = None
_cfg: Any = None
_weights_path: str | None = None
_last_error: str | None = None


def _resolve_weights_path() -> str:
    folder = app_config.WEIGHTS_FOLDER
    if not os.path.isabs(folder):
        folder = os.path.join(ROOT_DIR, folder.lstrip("./").lstrip(".\\"))
    return os.path.join(folder, app_config.WEIGHTS_FILE_NAME)


def _prediction_config_class():
    """Create the inference Config only after TensorFlow-backed mrcnn is needed."""
    from mrcnn.config import Config

    class PredictionConfig(Config):
        NAME = app_config.MODEL_NAME
        NUM_CLASSES = app_config.NUM_CLASSES
        GPU_COUNT = app_config.GPU_COUNT
        IMAGES_PER_GPU = app_config.IMAGES_PER_GPU
        DETECTION_MIN_CONFIDENCE = app_config.DETECTION_MIN_CONFIDENCE
        IMAGE_MAX_DIM = app_config.IMAGE_MAX_DIM
        IMAGE_MIN_DIM = app_config.IMAGE_MIN_DIM
        DETECTION_MAX_INSTANCES = app_config.DETECTION_MAX_INSTANCES
        DETECTION_NMS_THRESHOLD = app_config.DETECTION_NMS_THRESHOLD

    return PredictionConfig


def initialize_model():
    """Load the model once; importing this module itself never imports TensorFlow."""
    global _cfg, _model, _weights_path, _last_error
    if _model is not None and _cfg is not None:
        return _model, _cfg
    try:
        weights_path = _resolve_weights_path()
        if not os.path.exists(weights_path):
            raise FileNotFoundError(
                f"Weights file not found: {weights_path}. Place "
                "maskrcnn_15_epochs.h5 in weights/."
            )

        from mrcnn.model import MaskRCNN

        PredictionConfig = _prediction_config_class()
        model_dir = os.path.join(ROOT_DIR, "logs")
        os.makedirs(model_dir, exist_ok=True)
        _cfg = PredictionConfig()
        _model = MaskRCNN(mode="inference", model_dir=model_dir, config=_cfg)
        _model.load_weights(weights_path, by_name=True)
        _weights_path = weights_path
        _last_error = None
        logger.info("Mask R-CNN weights loaded: %s", weights_path)
        return _model, _cfg
    except Exception as exc:
        _model = None
        _cfg = None
        _weights_path = None
        _last_error = f"{type(exc).__name__}: {exc}"
        logger.error("Error initializing Mask R-CNN model: %s", exc, exc_info=True)
        raise


def get_model():
    return _model


def get_model_config():
    return _cfg


def get_weights_path():
    return _weights_path


def get_last_error() -> str | None:
    return _last_error


def get_config():
    """Backwards-compatible alias for the loaded model config."""
    return _cfg


def is_model_initialized() -> bool:
    return _model is not None and _cfg is not None
