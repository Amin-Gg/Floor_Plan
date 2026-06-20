"""
Application configuration settings
Centralized configuration management for the FloorPlanTo3D API.

Environment selection
---------------------
Set the APP_ENV environment variable before starting:

    APP_ENV=production gunicorn --config gunicorn.conf.py application:application
    APP_ENV=development python application.py

Valid values: development (default), production, testing
Note: FLASK_ENV was deprecated in Flask 2.3 — we use APP_ENV instead.

NOTE (Phase 1 — Mask R-CNN restore):
The detector is Mask R-CNN again. NUM_CLASSES below is the MODEL class count
(4 = background + wall + window + door) and MUST match the trained weights.
It is intentionally NOT imported from config/classes.py — that file's 15-class
list belongs to the (shelved) Mask2Former path and to the cold-code branches in
the analysis layer; it must not drive the Mask R-CNN head.
"""

import os
from typing import Dict, Any


class Config:
    """Base configuration — shared by all environments."""

    # ── Model (Mask R-CNN) ────────────────────────────────────────────────────
    MODEL_NAME         = "mask_rcnn_hq"
    WEIGHTS_FILE_NAME  = "maskrcnn_15_epochs.h5"
    WEIGHTS_FOLDER     = "./weights"          # resolved relative to project root

    # Model class count — MUST equal what the .h5 was trained with.
    # 4 = background + wall + window + door. Do NOT import this from
    # config/classes.py (that file describes the 15-class Mask2Former scheme).
    NUM_CLASSES              = 4
    GPU_COUNT                = 1
    IMAGES_PER_GPU           = 1

    # ── Inference config — VALIDATED VALUES from the repo that created the .h5 ─
    # The original FloorPlanTo3D ran this exact weights file at the Matterport
    # library defaults below. They are the authoritative settings for this .h5.
    #
    # TUNING (do this against YOUR plans once you can run inference):
    #   DETECTION_MIN_CONFIDENCE
    #     0.7  = validated baseline (high precision; may miss faint walls)
    #     ↓0.5 / 0.3 = raise recall if real walls/doors are being MISSED
    #     0.15 = what your Simsys base used (high recall, but more false
    #            positives — only go this low if recall is genuinely a problem)
    #   IMAGE_MAX_DIM
    #     1024 = validated (matches the model's training scale; best fidelity)
    #     1600 = more line detail, but objects appear larger vs trained anchors
    #            (what Simsys used). Must stay divisible by 64.
    DETECTION_MIN_CONFIDENCE = 0.7
    IMAGE_MAX_DIM            = 1024     # divisible by 64
    IMAGE_MIN_DIM            = 800
    # Cap on detections per image. Library default is 100; raised to 256 so
    # dense plans (many wall segments) don't get silently truncated. Raising
    # this only ALLOWS more output — it never changes which objects are found.
    DETECTION_MAX_INSTANCES  = 256
    # Per-class NMS IoU threshold (library/original default — set explicitly).
    DETECTION_NMS_THRESHOLD  = 0.3

    # ── Image processing ──────────────────────────────────────────────────────
    MAX_IMAGE_SIZE        = 2048    # pixels — prevent OOM
    MIN_IMAGE_SIZE        = 100
    ALLOW_IMAGE_RESIZE    = True
    RESIZE_QUALITY        = "LANCZOS"
    MAX_UPLOAD_MB         = 20      # reject uploads larger than this

    # ── Memory ────────────────────────────────────────────────────────────────
    MAX_MEMORY_USAGE_MB   = 1024    # 1 GB soft limit — logged, not enforced
    ENABLE_MEMORY_MONITORING = True

    # ── API server ────────────────────────────────────────────────────────────
    HOST  = "0.0.0.0"
    PORT  = 8080
    DEBUG = False

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL  = "INFO"
    LOG_FORMAT = "%(asctime)s [%(request_id)s] %(name)s %(levelname)s %(message)s"
    LOG_FORMAT_FALLBACK = "%(asctime)s %(name)s %(levelname)s %(message)s"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Restrict to specific origins in production — do not leave as "*".
    # Override via the APP_CORS_ORIGINS environment variable:
    #   APP_CORS_ORIGINS="https://myapp.ir,https://admin.myapp.ir"
    CORS_ORIGINS: str = os.getenv("APP_CORS_ORIGINS", "*")

    # ── Caching ───────────────────────────────────────────────────────────────
    ENABLE_CACHING = True
    CACHE_TIMEOUT  = 300            # seconds

    @classmethod
    def get_model_config(cls) -> Dict[str, Any]:
        return {
            "NAME":                     cls.MODEL_NAME,
            "NUM_CLASSES":              cls.NUM_CLASSES,
            "GPU_COUNT":                cls.GPU_COUNT,
            "IMAGES_PER_GPU":           cls.IMAGES_PER_GPU,
            "DETECTION_MIN_CONFIDENCE": cls.DETECTION_MIN_CONFIDENCE,
            "IMAGE_MAX_DIM":            cls.IMAGE_MAX_DIM,
            "IMAGE_MIN_DIM":            cls.IMAGE_MIN_DIM,
            "DETECTION_MAX_INSTANCES":  cls.DETECTION_MAX_INSTANCES,
            "DETECTION_NMS_THRESHOLD":  cls.DETECTION_NMS_THRESHOLD,
        }

    @classmethod
    def get_api_config(cls) -> Dict[str, Any]:
        return {"HOST": cls.HOST, "PORT": cls.PORT, "DEBUG": cls.DEBUG}


class DevelopmentConfig(Config):
    """Local development — verbose logging, debug mode on."""
    DEBUG        = True
    LOG_LEVEL    = "DEBUG"
    CORS_ORIGINS = "*"              # permissive during development


class ProductionConfig(Config):
    """
    Production — strict security defaults.
    APP_CORS_ORIGINS MUST be set before starting the server.
    """
    DEBUG          = False
    LOG_LEVEL      = "WARNING"
    ENABLE_CACHING = True
    CACHE_TIMEOUT  = 600

    @classmethod
    def _get_cors(cls) -> str:
        origins = os.getenv("APP_CORS_ORIGINS", "")
        if not origins:
            raise RuntimeError(
                "APP_CORS_ORIGINS must be set in production. "
                "Example: export APP_CORS_ORIGINS='https://yourdomain.ir'\n"
                "To temporarily bypass (not recommended): "
                "export APP_CORS_ORIGINS='*'"
            )
        return origins


class TestingConfig(Config):
    """Unit and integration tests — no caching, debug logging."""
    DEBUG           = True
    LOG_LEVEL       = "DEBUG"
    ENABLE_CACHING  = False
    CORS_ORIGINS    = "*"


# ── Config map ────────────────────────────────────────────────────────────────
_CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "testing":     TestingConfig,
}


def get_config(environment: str = None) -> Config:
    """
    Return the Config class for the requested environment.

    Reads APP_ENV (not the deprecated FLASK_ENV) when environment is None.
    Falls back to DevelopmentConfig if the variable is unset or unknown.
    """
    if environment is None:
        environment = os.getenv("APP_ENV") or os.getenv("FLASK_ENV", "development")
    cfg = _CONFIG_MAP.get(environment.lower(), DevelopmentConfig)
    return cfg