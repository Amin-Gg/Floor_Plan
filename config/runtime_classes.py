"""Authoritative class registry for the active inference pipeline.

The production Mask R-CNN model emits three foreground classes plus background:
0=background, 1=wall, 2=window, 3=door. Optional YOLO detections use the
separate named registry in :mod:`config.yolo_classes` and are never mixed into
this numeric ID space.
"""
from __future__ import annotations

from types import MappingProxyType

BACKGROUND_CLASS_ID = 0
PRIMARY_CLASS_ID_TO_NAME = MappingProxyType({
    1: "wall",
    2: "window",
    3: "door",
})
PRIMARY_CLASS_NAME_TO_ID = MappingProxyType(
    {name: class_id for class_id, name in PRIMARY_CLASS_ID_TO_NAME.items()}
)
PRIMARY_NUM_CLASSES = 1 + len(PRIMARY_CLASS_ID_TO_NAME)


def primary_class_name(class_id: int) -> str:
    """Return the active primary detector class name or ``unknown``."""
    return PRIMARY_CLASS_ID_TO_NAME.get(int(class_id), "unknown")
