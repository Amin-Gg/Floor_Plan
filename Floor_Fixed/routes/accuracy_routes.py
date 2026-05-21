"""
Accuracy analysis routes
"""

from flask import Blueprint, request, jsonify
import logging
import traceback
from PIL import Image
from models.mask_rcnn_model import get_model, get_config, is_model_initialized
from services.image_validation import validate_and_resize_image, check_memory_usage
from services.accuracy_service import performAccuracyAnalysis
from image_processing.image_loader import myImageLoader
from utils.file_utils import getNextTestNumber, saveAccuracyAnalysis

logger = logging.getLogger(__name__)

# Create blueprint
bp = Blueprint('accuracy', __name__)


@bp.route('/analyze_accuracy', methods=['POST'])
def analyze_accuracy():
    """Analyze the accuracy and reliability of the model predictions"""

    if not is_model_initialized():
        return jsonify({"error": "Model not initialized. Please check server logs."}), 503

    try:
        imagefile = Image.open(request.files['image'].stream)

        # Validate and resize image
        imagefile, resize_info = validate_and_resize_image(imagefile)

        if resize_info["reason"] in ["image_too_small", "resize_would_make_too_small", "image_too_large_resize_disabled"]:
            return jsonify({
                "error": f"Image validation failed: {resize_info['reason']}",
                "details": {
                    "original_size": resize_info["original_size"],
                    "min_size": 100,
                    "max_size": 2048,
                    "resize_allowed": True
                }
            }), 400

        memory_before = check_memory_usage()
        logger.debug(f"Memory before processing: {memory_before:.1f}MB")

        image, w, h = myImageLoader(imagefile)
        logger.info(f"Analyzing accuracy for image: {h}x{w}")

        if resize_info["resized"]:
            logger.info(f"Image was resized: {resize_info['original_size']} -> {resize_info['new_size']}")

        # New engine: pass raw numpy image directly — no molding or batch expansion needed
        model = get_model()
        r = model.detect([image], verbose=0)[0]

        # Perform accuracy analysis
        accuracy_report = performAccuracyAnalysis(r, w, h)

        test_num = getNextTestNumber()
        filename = saveAccuracyAnalysis(accuracy_report, test_num)

        memory_after = check_memory_usage()
        logger.debug(f"Memory after processing: {memory_after:.1f}MB")

        response = accuracy_report.copy()
        response["analysis_file"] = filename
        response["image_processing"] = {
            "original_size": resize_info["original_size"],
            "processed_size": resize_info.get("new_size", resize_info["original_size"]),
            "resized": resize_info["resized"],
            "resize_factor": resize_info["resize_factor"],
            "resize_reason": resize_info["reason"]
        }
        response["memory_usage"] = {
            "before_processing_mb": memory_before,
            "after_processing_mb": memory_after,
            "memory_increase_mb": memory_after - memory_before
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error in accuracy analysis: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
