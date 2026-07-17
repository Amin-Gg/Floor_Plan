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

from flask_openapi3 import APIBlueprint, Tag
from flask import jsonify, g

from models.mask_rcnn_model import get_model_config
from services.model_runtime import is_runtime_ready, runtime_status

logger = logging.getLogger(__name__)

bp = APIBlueprint("health", __name__)
TAG = Tag(name="System", description="Service health and runtime status")


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
    """Probe OCR package metadata without importing PaddleOCR itself."""
    import importlib.util
    available = importlib.util.find_spec("paddleocr") is not None
    return {
        "available": available,
        "engine": "PaddleOCR",
        "lazy_loaded": True,
        **({} if available else {"error": "paddleocr package is not installed"}),
    }


def _model_path() -> str:
    """Return the actual weights file the model loaded, or a clear status marker."""
    try:
        from models.mask_rcnn_model import get_weights_path
        loaded = get_weights_path()
        if loaded:
            return loaded
    except Exception:
        pass
    # Loader hasn't run yet (or failed). Fall back to any explicit override.
    env_path = os.getenv("FLOORPLAN_MODEL_PATH", "").strip()
    if env_path:
        return env_path
    return "model not initialized"


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@bp.get("/livez", tags=[TAG], summary="Process liveness probe")
def liveness_check():
    """Minimal unauthenticated probe: the HTTP process can answer requests."""
    return jsonify({"status": "alive"}), 200


@bp.get("/readyz", tags=[TAG], summary="Inference readiness probe")
def readiness_check():
    """Minimal unauthenticated probe used by container orchestrators."""
    ready = is_runtime_ready()
    status = runtime_status()
    payload = {
        "status": "ready" if ready else "not_ready",
        "inference_mode": status.get("mode"),
        "hard_timeout": bool(status.get("hard_timeout")),
    }
    return jsonify(payload), 200 if ready else 503


@bp.get("/health", tags=[TAG], summary="Stage-1 model readiness")
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
    model_loaded = is_runtime_ready()
    inference = runtime_status()

    # ── Common diagnostic block (returned on both success and failure) ──────
    base = {
        "status":         "healthy" if model_loaded else "unavailable",
        "model_loaded":   model_loaded,
        "model_path":     _model_path(),
        "model_error":    inference.get("last_error"),
        "inference":      inference,
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