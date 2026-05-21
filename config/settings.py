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
"""

import os
from typing import Dict, Any


class Config:
    """Base configuration — shared by all environments."""

    # Class count — imported from the single source of truth.
    # Do NOT set this to 8 (that was a legacy Mask R-CNN value that included background).
    # Mask2Former handles background implicitly; we have 7 architectural classes.
    from config.classes import NUM_CLASSES as _NC
    NUM_CLASSES = _NC   # 7
    GPU_COUNT             = 1
    IMAGES_PER_GPU        = 1
    DETECTION_MIN_CONFIDENCE = 0.15
    # Mask2Former Swin-Large VRAM budget:
    # At 1600px: feature pyramid alone uses ~4-6 GB VRAM — causes CUDA OOM on
    # most 8-12 GB cards when other processes are running.
    # At 1024px: ~1.5-2 GB — safe on any modern GPU with ≥ 8 GB VRAM.
    # The processor handles resizing internally; output quality loss is minimal
    # because architectural lines are preserved at 1024px.
    IMAGE_MAX_DIM         = 1024

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
            "NUM_CLASSES":            cls.NUM_CLASSES,
            "GPU_COUNT":              cls.GPU_COUNT,
            "IMAGES_PER_GPU":         cls.IMAGES_PER_GPU,
            "DETECTION_MIN_CONFIDENCE": cls.DETECTION_MIN_CONFIDENCE,
            "IMAGE_MAX_DIM":          cls.IMAGE_MAX_DIM,
        }

    @classmethod
    def get_api_config(cls) -> Dict[str, Any]:
        return {"HOST": cls.HOST, "PORT": cls.PORT, "DEBUG": cls.DEBUG}


class DevelopmentConfig(Config):
    """Local development — verbose logging, debug mode on."""
    DEBUG      = True
    LOG_LEVEL  = "DEBUG"
    CORS_ORIGINS = "*"              # permissive during development


class ProductionConfig(Config):
    """
    Production — strict security defaults.
    APP_CORS_ORIGINS MUST be set before starting the server.
    """
    DEBUG      = False
    LOG_LEVEL  = "WARNING"
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
        # APP_ENV is our variable; FLASK_ENV kept as a legacy fallback only
        environment = os.getenv("APP_ENV") or os.getenv("FLASK_ENV", "development")
    cfg = _CONFIG_MAP.get(environment.lower(), DevelopmentConfig)
    return cfg
