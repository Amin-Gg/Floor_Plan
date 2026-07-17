"""Explicit detector orchestration for the production pipeline.

Heavy TensorFlow/PyTorch modules are imported lazily so schema tooling, tests,
and preflight checks can import this module on a Python host without GPU ML
runtimes installed.
"""
from __future__ import annotations

from typing import Any

PRIMARY_CLASSES = {
    0: "background",
    1: "wall",
    2: "window",
    3: "door",
}


def is_model_initialized() -> bool:
    from services.model_runtime import is_runtime_ready
    return is_runtime_ready()


def get_model():
    from models.mask_rcnn_model import get_model as getter
    return getter()


def get_executor():
    from utils.inference_executor import get_executor as getter
    return getter()


def is_yolo_initialized() -> bool:
    from models.yolo_detector import is_yolo_initialized as check
    return check()



def get_yolo_status() -> dict[str, Any]:
    from models.yolo_detector import get_yolo_status as getter
    return getter()


def detect_supplementary(image):
    from models.yolo_detector import detect_supplementary as detect
    return detect(image)


def run_detectors(image) -> dict[str, Any]:
    """Run required Mask R-CNN and optional supplementary YOLO."""
    if not is_model_initialized():
        raise RuntimeError("Mask R-CNN is not initialized")

    executor = get_executor()
    if hasattr(executor, "detect_bundle"):
        bundle = executor.detect_bundle(image)
    else:  # compatibility for lightweight test doubles and legacy adapters
        primary_legacy = executor.run(get_model().detect, [image], verbose=0)[0]
        yolo_active_legacy = is_yolo_initialized()
        bundle = {
            "primary": primary_legacy,
            "supplementary": detect_supplementary(image) if yolo_active_legacy else [],
            "yolo_status": get_yolo_status(),
            "_yolo_active": yolo_active_legacy,
        }
    primary = bundle["primary"]
    class_ids = [int(x) for x in primary.get("class_ids", [])]
    unsupported = sorted({cid for cid in class_ids if cid not in PRIMARY_CLASSES})
    if unsupported:
        raise ValueError(
            "Mask R-CNN emitted class ids outside its declared 4-class contract: "
            f"{unsupported}"
        )

    supplementary = bundle.get("supplementary", [])
    yolo_status = bundle.get("yolo_status") or get_yolo_status()
    yolo_active = bool(bundle.get("_yolo_active", yolo_status.get("initialized")))
    supplementary_status = (
        "degraded" if yolo_status.get("last_error") and yolo_active
        else "ok" if yolo_active else "disabled_or_unavailable"
    )

    return {
        "primary": primary,
        "supplementary": supplementary,
        "detector_status": {
            "primary": "mask_rcnn_4class",
            "primary_classes": ["wall", "window", "door"],
            "supplementary": "yolo_v8" if yolo_active else "not_active",
            "supplementary_status": supplementary_status,
            "supplementary_detail": yolo_status,
        },
    }
