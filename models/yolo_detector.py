"""
models/yolo_detector.py
=======================
Supplementary YOLOv8 detector (sanatladkat `best.pt`).

Runs in parallel with the Mask R-CNN engine and contributes ONLY the element
types Mask R-CNN doesn't produce: columns, railings, staircases. Returns plain
box detections (no masks) in the Mask R-CNN bbox convention [y1, x1, y2, x2].

Public interface (mirrors models/mask_rcnn_model.py for consistency):
    initialize_yolo()        -> load best.pt once at startup (returns the model or None)
    get_yolo()               -> the loaded YOLO model, or None
    is_yolo_initialized()    -> bool
    detect_supplementary(image_bgr_or_rgb) -> list[dict]

Each detection dict:
    {
      "element_type": "Column" | "Railing" | "Stairs" | ...,
      "bucket":       "columns" | "railings" | "stairs" | ...,
      "geometry":     "footprint" | "line",
      "bbox":         [y1, x1, y2, x2],   # pixels, Mask R-CNN convention
      "confidence":   float,
      "yolo_class":   "<raw YOLO class name>",
      "source":       "yolo",
    }

Design notes:
- ultralytics (and therefore torch) is imported LAZILY inside initialize_yolo()
  so the module can be imported even where YOLO isn't installed, and so a
  missing/disabled YOLO never blocks the Mask R-CNN pipeline.
- If YOLO_ENABLED is false, or weights are missing, or ultralytics isn't
  installed, initialization logs a warning and the detector becomes a no-op
  (detect_supplementary returns []). The main pipeline keeps working.
"""

import logging
import os

from config.constants import ROOT_DIR
from config.yolo_classes import (
    YOLO_ENABLED,
    YOLO_IMG_SIZE,
    YOLO_MIN_CONFIDENCE,
    YOLO_WEIGHTS_FILE_NAME,
    YOLO_WEIGHTS_FOLDER,
    resolve_yolo_class,
)

logger = logging.getLogger(__name__)

_yolo_model = None
_yolo_ready = False
_yolo_last_error = None


def _resolve_weights_path() -> str:
    folder = YOLO_WEIGHTS_FOLDER
    if not os.path.isabs(folder):
        folder = os.path.join(ROOT_DIR, folder.lstrip("./").lstrip(".\\"))
    return os.path.join(folder, YOLO_WEIGHTS_FILE_NAME)


def initialize_yolo():
    """
    Load the YOLOv8 weights once. Soft-fails (logs + no-op) if YOLO is disabled,
    ultralytics is missing, or the weights aren't present — the Mask R-CNN
    pipeline must never be blocked by the supplementary detector.
    """
    global _yolo_model, _yolo_ready, _yolo_last_error
    _yolo_last_error = None

    if not YOLO_ENABLED:
        _yolo_last_error = "disabled"
        logger.info("YOLO supplementary detector disabled (YOLO_ENABLED=false).")
        return None

    weights_path = _resolve_weights_path()
    if not os.path.exists(weights_path):
        _yolo_last_error = f"weights_missing:{weights_path}"
        logger.warning(
            "YOLO weights not found at %s — supplementary detection (columns/"
            "railings/stairs) will be skipped. Place the sanatladkat best.pt "
            "there (renamed to %s) to enable it.",
            weights_path, YOLO_WEIGHTS_FILE_NAME,
        )
        return None

    try:
        from ultralytics import YOLO  # lazy import (pulls torch)
    except ImportError:
        _yolo_last_error = "ultralytics_not_installed"
        logger.warning(
            "ultralytics not installed — supplementary YOLO detection skipped. "
            "Add `ultralytics` (and torch) to requirements to enable it."
        )
        return None

    try:
        _yolo_model = YOLO(weights_path)
        _yolo_ready = True
        names = getattr(_yolo_model, "names", {})
        logger.info("YOLO supplementary detector loaded: %s (classes: %s)",
                    weights_path, list(names.values()) if isinstance(names, dict) else names)
        return _yolo_model
    except Exception as exc:
        logger.error("Failed to load YOLO weights (%s): %s", weights_path, exc, exc_info=True)
        _yolo_model = None
        _yolo_ready = False
        _yolo_last_error = f"initialization_failed:{exc}"
        return None


def get_yolo():
    return _yolo_model


def is_yolo_initialized() -> bool:
    return _yolo_ready and _yolo_model is not None



def get_yolo_status() -> dict:
    """Return an auditable supplementary-detector runtime status."""
    return {
        "enabled": bool(YOLO_ENABLED),
        "initialized": is_yolo_initialized(),
        "last_error": _yolo_last_error,
        "weights_path": _resolve_weights_path(),
    }


def detect_supplementary(image):
    """
    Run YOLO and return filtered box detections for the supplementary classes.

    `image` may be an HxWx3 numpy array (RGB or BGR) or a file path — ultralytics
    accepts both. Returns [] if YOLO isn't ready (never raises for that reason).
    """
    global _yolo_last_error
    if not is_yolo_initialized():
        return []

    _yolo_last_error = None
    try:
        results = _yolo_model.predict(
            source=image,
            conf=YOLO_MIN_CONFIDENCE,
            imgsz=YOLO_IMG_SIZE,
            verbose=False,
        )
    except Exception as exc:
        _yolo_last_error = f"inference_failed:{exc}"
        logger.error("YOLO inference failed: %s", exc, exc_info=True)
        return []

    detections = []
    names = getattr(_yolo_model, "names", {}) or {}
    for res in results:
        boxes = getattr(res, "boxes", None)
        if boxes is None:
            continue
        # iterate per-box; ultralytics tensors → python floats/ints
        for b in boxes:
            cls_id = int(b.cls[0]) if hasattr(b, "cls") else int(b.cls)
            raw_name = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(cls_id)
            spec = resolve_yolo_class(raw_name)
            if spec is None:
                continue   # ignored class (wall/window/door/dimension/etc.)
            conf = float(b.conf[0]) if hasattr(b, "conf") else float(b.conf)
            # ultralytics xyxy = [x1, y1, x2, y2]; convert to Mask R-CNN [y1,x1,y2,x2]
            xyxy = b.xyxy[0].tolist() if hasattr(b.xyxy[0], "tolist") else list(b.xyxy[0])
            x1, y1, x2, y2 = [float(v) for v in xyxy]
            detections.append({
                "element_type": spec["element_type"],
                "bucket":       spec["bucket"],
                "geometry":     spec["geometry"],
                "bbox":         [y1, x1, y2, x2],
                "confidence":   conf,
                "yolo_class":   raw_name,
                "source":       "yolo",
            })

    logger.info("YOLO supplementary: %d detection(s) kept (%s)",
                len(detections),
                ", ".join(sorted({d["element_type"] for d in detections})) or "none")
    return detections
