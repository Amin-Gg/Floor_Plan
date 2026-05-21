"""
Mask2Former (Swin-Large) engine configuration and initialization
Replaces legacy MaskRCNN
"""

import os
import logging
import torch
import numpy as np
from transformers import Mask2FormerForUniversalSegmentation, Mask2FormerImageProcessor
from config.settings import get_config

# Get application configuration
app_config = get_config()
logger = logging.getLogger(__name__)

# Global variables for model
_model = None
_cfg = None

class Mask2FormerSwinLEngine:
    """
    Wrapper class to make Mask2Former output compatible with legacy MaskRCNN format.
    Ready for fine-tuning on architectural floor plan datasets.
    """
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Loading Mask2Former (Swin-Large) on {self.device}...")

        # Load the base Swin-Large model (Pre-trained on COCO for now)
        # After fine-tuning, replace model_id with your saved checkpoint path:
        #   model_id = "./weights/mask2former-floorplan-finetuned"
        model_id = "facebook/mask2former-swin-large-coco-instance"

        self.processor = Mask2FormerImageProcessor.from_pretrained(model_id)
        self.model = Mask2FormerForUniversalSegmentation.from_pretrained(model_id)
        self.model.to(self.device)
        self.model.eval()

        self.min_confidence = app_config.DETECTION_MIN_CONFIDENCE

    def detect(self, images, verbose=0):
        """
        Runs inference and adapts output to match the legacy MaskRCNN dictionary format:
        {'rois': np.array, 'class_ids': np.array, 'scores': np.array, 'masks': np.array(H,W,N)}
        """
        image = images[0] if isinstance(images, list) else images

        # Preprocess — processor handles normalization and resizing internally
        inputs = self.processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Inference
        with torch.no_grad():
            outputs = self.model(**inputs)

        # Post-process — target_sizes restores masks to original image dimensions
        result = self.processor.post_process_instance_segmentation(
            outputs,
            target_sizes=[image.shape[:2]]
        )[0]

        final_rois = []
        final_masks = []
        final_class_ids = []
        final_scores = []

        segments_info = result["segments_info"]
        # IMPORTANT: segmentation_map pixel values are instance IDs (info["id"]),
        # NOT class label IDs (info["label_id"]). These are different values.
        segmentation_map = result["segmentation"].cpu().numpy()

        for info in segments_info:
            score = info["score"]
            if score < self.min_confidence:
                continue

            # segment_id: the unique instance ID — used as pixel value in segmentation_map
            # label_id:   the class category ID — used to map to our project classes
            segment_id = info["id"]
            label_id   = info["label_id"]

            mapped_class_id = self._map_to_project_classes(label_id)
            if mapped_class_id is None:
                continue

            # Extract binary mask using the INSTANCE id, not the class label id
            binary_mask = (segmentation_map == segment_id).astype(np.uint8)

            y_indices, x_indices = np.where(binary_mask)
            if len(y_indices) == 0:
                continue

            y1, x1 = int(np.min(y_indices)), int(np.min(x_indices))
            y2, x2 = int(np.max(y_indices)), int(np.max(x_indices))

            final_rois.append([y1, x1, y2, x2])
            final_class_ids.append(mapped_class_id)
            final_scores.append(float(score))
            final_masks.append(binary_mask.astype(bool))

        # Stack masks to shape (H, W, N) as expected by all downstream analysis modules
        if final_masks:
            final_masks_stack = np.stack(final_masks, axis=-1)
        else:
            final_masks_stack = np.empty((image.shape[0], image.shape[1], 0), dtype=bool)

        return [{
            "rois":      np.array(final_rois, dtype=np.int32) if final_rois else np.empty((0, 4), dtype=np.int32),
            "class_ids": np.array(final_class_ids, dtype=np.int32),
            "scores":    np.array(final_scores, dtype=np.float32),
            "masks":     final_masks_stack
        }]

    def _map_to_project_classes(self, label_id):
        """
        Maps model output label_id to the project class system.

        Project class IDs:
            1=Wall  2=Window  3=Door  4=Stairs  5=Parking  6=Balcony  7=Terrace

        PRE-TRAINING (current state):
            Returns None for everything — the COCO model does not understand
            architectural elements. All detections are suppressed until fine-tuning.

        POST-TRAINING:
            After running train_mask2former.py, the fine-tuned model's label_ids
            will directly match project IDs 1-7.
            Replace the body of this method with the two lines below:

                valid_classes = {1, 2, 3, 4, 5, 6, 7}
                return label_id if label_id in valid_classes else None
        """
        return None  # Block all detections until fine-tuning is complete


class DummyConfig:
    """
    Mock configuration object.
    Provides the same interface as the old MaskRCNN PredictionConfig so that
    routes and health checks that read config properties continue to work.
    """
    NAME = "mask2former-swin-large-floorplan"
    IMAGE_RESIZE_MODE = "none"
    IMAGE_MAX_DIM = app_config.IMAGE_MAX_DIM
    NUM_CLASSES = app_config.NUM_CLASSES
    DETECTION_MIN_CONFIDENCE = app_config.DETECTION_MIN_CONFIDENCE


def initialize_model():
    """Initialize the Mask2Former model once at startup"""
    global _cfg, _model
    try:
        logger.info('==============Initializing Mask2Former (Swin-L)=========')
        _cfg = DummyConfig()
        _model = Mask2FormerSwinLEngine()
        logger.info('=================Model loaded successfully==============')
        return _model, _cfg
    except Exception as e:
        logger.error(f"Error initializing model: {str(e)}")
        raise e


def get_model():
    global _model
    return _model


def get_config():
    global _cfg
    return _cfg


def is_model_initialized():
    global _model, _cfg
    return _model is not None and _cfg is not None
