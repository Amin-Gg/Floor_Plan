"""
Health check routes
"""

import logging

from flask_openapi3 import APIBlueprint
from flask import jsonify, g

from models.mask_rcnn_model import get_config, is_model_initialized

logger = logging.getLogger(__name__)

bp = APIBlueprint("health", __name__)


@bp.route("/health", methods=["GET"])
def health_check():
    """
    Health check endpoint.

    Returns 200 when the model is loaded and ready,
    503 when it is still initializing or failed to load.

    The response is intentionally NOT wrapped in the standard error envelope
    because monitoring tools (load balancers, uptime checkers) expect a simple
    status object here, not a generic API error structure.
    """
    request_id = getattr(g, "request_id", "-")

    if not is_model_initialized():
        logger.warning("[%s] Health check: model not initialized", request_id)
        return jsonify({
            "status":      "unavailable",
            "model_loaded": False,
            "message":     "AI model is not yet initialized. "
                           "Check server logs for details.",
        }), 503

    cfg = get_config()
    return jsonify({
        "status":       "healthy",
        "model_loaded": True,
        "model_config": {
            "name":                    cfg.NAME,
            "num_classes":             cfg.NUM_CLASSES,
            "detection_min_confidence": cfg.DETECTION_MIN_CONFIDENCE,
            "image_max_dim":           cfg.IMAGE_MAX_DIM,
        },
    }), 200
