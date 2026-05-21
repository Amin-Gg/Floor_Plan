"""
Health check routes
===================

Returns a structured status snapshot of the API used by:
  - Load balancers and uptime monitors (status field is always present)
  - The frontend, to know whether to show OCR-related features
  - The operations team, to debug deployments

Response shape is intentionally flat so it can be consumed by simple monitoring
tools (Datadog, UptimeRobot, k8s liveness probes) without parsing nested JSON.

Status semantics
----------------
"healthy"     200 — model is loaded and ready to serve requests
"unavailable" 503 — model is not initialized; do NOT route traffic here

The status field is ALWAYS one of these two values. The other fields are
diagnostic — clients should not branch on their content.
"""

import logging
import os
import platform
import sys

from flask_openapi3 import APIBlueprint
from flask import jsonify, g

from models.mask_rcnn_model import get_model_config, is_model_initialized

logger = logging.getLogger(__name__)

bp = APIBlueprint("health", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic helpers
# ─────────────────────────────────────────────────────────────────────────────
# Each helper returns a self-contained dict and NEVER raises. Health-check
# code must be bulletproof — if a probe fails, the whole endpoint must still
# return a usable response so monitors can distinguish "service degraded" from
# "service down".

def _torch_info() -> dict:
    """Probe torch for version and CUDA availability without raising."""
    try:
        import torch  # imported here so this module loads even if torch is missing
        info = {
            "available":      True,
            "version":        torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
        }
        if info["cuda_available"]:
            try:
                info["cuda_device_count"] = torch.cuda.device_count()
                info["cuda_device_name"]  = torch.cuda.get_device_name(0)
            except Exception as exc:
                # CUDA reports available but device probing failed — report partial info
                info["cuda_device_error"] = str(exc)
        return info
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _model_device() -> str:
    """Return the device string of the loaded model, or 'unknown' on probe failure."""
    try:
        from models.mask_rcnn_model import get_model
        engine = get_model()
        if engine is None or not hasattr(engine, "device"):
            return "unknown"
        return str(engine.device)
    except Exception:
        return "unknown"


def _ocr_status() -> dict:
    """
    Report OCR library availability.

    PaddleOCR is lazy-initialized on first request, so we only check whether
    the library is importable here — not whether the singleton is loaded.
    A future health probe could trigger initialization, but that defeats the
    purpose of a fast health check.
    """
    try:
        import paddleocr  # noqa: F401 — import test
        return {"available": True, "engine": "PaddleOCR", "lazy_loaded": True}
    except ImportError as exc:
        return {"available": False, "engine": "PaddleOCR", "error": str(exc)}
    except Exception as exc:
        # Some PaddleOCR installs raise non-ImportError at import time
        # (e.g., missing libgomp). Report it cleanly.
        return {"available": False, "engine": "PaddleOCR", "error": str(exc)}


def _model_path() -> str:
    """Return the model path string the API will use, or a fallback marker."""
    env_path = os.getenv("FLOORPLAN_MODEL_PATH", "").strip()
    if env_path:
        return env_path
    # Fall back to the same default the model loader uses
    return "coco_fallback (set FLOORPLAN_MODEL_PATH to use fine-tuned weights)"


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@bp.route("/health", methods=["GET"])
def health_check():
    """
    Structured health check.

    Returns 200 when the model is loaded and ready,
    503 when it is still initializing or failed to load.

    The response is intentionally NOT wrapped in the standard error envelope
    because monitoring tools (load balancers, uptime checkers) expect a simple
    status object here, not a generic API error structure.
    """
    request_id = getattr(g, "request_id", "-")
    model_loaded = is_model_initialized()

    # ── Common diagnostic block (returned on both success and failure) ──────
    base = {
        "status":         "healthy" if model_loaded else "unavailable",
        "model_loaded":   model_loaded,
        "model_path":     _model_path(),
        "environment":    os.getenv("APP_ENV", "development"),
        "python_version": sys.version.split()[0],
        "platform":       platform.platform(),
        "torch":          _torch_info(),
        "ocr":            _ocr_status(),
    }

    if not model_loaded:
        logger.warning("[%s] Health check: model not initialized", request_id)
        base["message"] = (
            "AI model is not yet initialized. Check server logs for details."
        )
        return jsonify(base), 503

    # ── Healthy response: add model config and device info ──────────────────
    cfg = get_model_config()
    if cfg is not None:
        base["model_config"] = {
            "name":                     cfg.NAME,
            "num_classes":              cfg.NUM_CLASSES,
            "detection_min_confidence": cfg.DETECTION_MIN_CONFIDENCE,
            "image_max_dim":            cfg.IMAGE_MAX_DIM,
        }
    base["model_device"] = _model_device()

    return jsonify(base), 200
