"""
Legacy confidence-diagnostics route.

The historical URL contains ``accuracy`` but the endpoint has no ground truth.
The legacy URL remains for compatibility while the semantics stay explicit.
"""

import logging

from flask_openapi3 import APIBlueprint
from flask import request, jsonify

from services.model_runtime import is_runtime_ready
from services.image_validation import validate_and_resize_image, check_memory_usage
from services.accuracy_service import performAccuracyAnalysis
from image_processing.image_loader import myImageLoader
from utils.file_utils import getNextTestNumber, saveAccuracyAnalysis
from utils.error_handlers import ModelNotReadyError, ImageValidationError
from utils.validators import require_image_upload
from utils.inference_executor import get_executor

logger = logging.getLogger(__name__)

bp = APIBlueprint('accuracy', __name__)


@bp.route('/analyze_reliability', methods=['POST'])
@bp.route('/analyze_accuracy', methods=['POST'])
def analyze_accuracy():
    """Analyze prediction confidence; this is not ground-truth accuracy."""

    if not is_runtime_ready():
        raise ModelNotReadyError()

    # Validate image upload — raises ImageValidationError (→ 400) if bad
    imagefile = require_image_upload("image")

    imagefile, resize_info = validate_and_resize_image(imagefile)

    if resize_info["reason"] in [
        "image_too_small",
        "resize_would_make_too_small",
        "image_too_large_resize_disabled",
    ]:
        raise ImageValidationError(
            f"Image validation failed: {resize_info['reason']}",
            details={
                "original_size": resize_info["original_size"],
                "min_size":      100,
                "max_size":      2048,
                "resize_allowed": True,
            },
        )

    memory_before = check_memory_usage()
    logger.debug("Memory before processing: %.1f MB", memory_before)

    image, w, h = myImageLoader(imagefile)
    logger.info("Analyzing accuracy for image: %dx%d", h, w)

    if resize_info["resized"]:
        logger.info(
            "Image was resized: %s -> %s",
            resize_info["original_size"], resize_info["new_size"]
        )

    r = get_executor().detect_primary(image)

    accuracy_report = performAccuracyAnalysis(r, w, h)

    test_num = getNextTestNumber()
    filename  = saveAccuracyAnalysis(accuracy_report, test_num)

    memory_after = check_memory_usage()
    logger.debug("Memory after processing: %.1f MB", memory_after)

    return jsonify({
        **accuracy_report,
        "endpoint_migration": {
            "preferred_path": "/analyze_reliability",
            "legacy_path": "/analyze_accuracy",
            "legacy_path_deprecated": True,
        },
        "analysis_file": filename,
        "image_processing": {
            "original_size":  resize_info["original_size"],
            "processed_size": resize_info.get("new_size", resize_info["original_size"]),
            "resized":        resize_info["resized"],
            "resize_factor":  resize_info["resize_factor"],
            "resize_reason":  resize_info["reason"],
        },
        "memory_usage": {
            "before_processing_mb": memory_before,
            "after_processing_mb":  memory_after,
            "memory_increase_mb":   memory_after - memory_before,
        },
    })
