"""Conservative door geometry analysis.

Only properties visible in the segmentation mask are emitted. In particular,
the fallback path never invents a swing direction from the door's position in
the image. Unknown values remain unknown and are marked for review.
"""
from __future__ import annotations

import cv2
import numpy as np

from utils.conversions import pixels_to_mm


def _bbox_axis(door_bbox) -> str:
    y1, x1, y2, x2 = [float(v) for v in door_bbox]
    return "horizontal" if abs(x2 - x1) >= abs(y2 - y1) else "vertical"


def _unknown(axis: str, method: str, reason: str, confidence: float = 0.2) -> dict:
    return {
        "door_type": axis,
        "estimated_swing": "unknown",
        "hinge_side": "unknown",
        "confidence": float(confidence),
        "analysis_method": method,
        "needs_review": True,
        "review_reason": reason,
        "observable_from_plan": False,
    }


def analyzeDoorOrientation(door_mask, door_bbox, image_width=None, image_height=None):
    """Estimate a visible image-plane swing from the mask's leaf and arc.

    A probabilistic Hough transform locates the longest straight leaf. Pixels
    sufficiently far from that line are treated as the swing arc. If either
    structure is not separable, the function returns ``unknown`` rather than
    inventing direction from image position.
    """
    del image_width, image_height
    fallback_axis = _bbox_axis(door_bbox)
    if door_mask is None:
        return _unknown(fallback_axis, "bbox_axis_only", "door mask is unavailable")

    mask = np.asarray(door_mask)
    if mask.ndim != 2:
        return _unknown(fallback_axis, "bbox_axis_only", "door mask must be two-dimensional")
    binary = (mask > 0).astype(np.uint8) * 255
    if int(np.count_nonzero(binary)) < 12:
        return _unknown(fallback_axis, "bbox_axis_only", "door mask has too few foreground pixels")
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    min_len = max(8, int(max(binary.shape) * 0.18))
    lines = cv2.HoughLinesP(
        binary, 1, np.pi / 180, threshold=max(6, min_len // 3),
        minLineLength=min_len, maxLineGap=5,
    )
    if lines is None:
        return _unknown(fallback_axis, "bbox_axis_only", "straight door leaf was not detected")

    candidates = [tuple(int(v) for v in line[0]) for line in lines]
    x1, y1, x2, y2 = max(
        candidates, key=lambda line: (line[2] - line[0]) ** 2 + (line[3] - line[1]) ** 2
    )
    dx, dy = float(x2 - x1), float(y2 - y1)
    leaf_length = float(np.hypot(dx, dy))
    if leaf_length < min_len:
        return _unknown(fallback_axis, "bbox_axis_only", "detected door leaf is too short")
    axis = "horizontal" if abs(dx) >= abs(dy) else "vertical"

    ys, xs = np.nonzero(binary)
    signed_distance = ((xs - x1) * dy - (ys - y1) * dx) / leaf_length
    arc_selector = np.abs(signed_distance) > 2.5
    arc_xs = xs[arc_selector]
    arc_ys = ys[arc_selector]
    if len(arc_xs) < 8:
        return _unknown(axis, "hough_leaf_only", "swing arc is not separable from the door leaf")

    arc_cx = float(np.mean(arc_xs))
    arc_cy = float(np.mean(arc_ys))
    line_cx = (x1 + x2) / 2.0
    line_cy = (y1 + y2) / 2.0
    if axis == "horizontal":
        offset = arc_cy - line_cy
        minimum_offset = max(2.0, binary.shape[0] * 0.02)
        if abs(offset) < minimum_offset:
            return _unknown(axis, "hough_leaf_arc", "swing quadrant is ambiguous", 0.35)
        swing = "opens_downward" if offset > 0 else "opens_upward"
    else:
        offset = arc_cx - line_cx
        minimum_offset = max(2.0, binary.shape[1] * 0.02)
        if abs(offset) < minimum_offset:
            return _unknown(axis, "hough_leaf_arc", "swing quadrant is ambiguous", 0.35)
        swing = "opens_rightward" if offset > 0 else "opens_leftward"

    separation = abs(offset) / max(leaf_length, 1.0)
    confidence = round(min(0.9, 0.58 + separation), 2)
    return {
        "door_type": axis,
        "estimated_swing": swing,
        # Mapping the image-plane leaf endpoint to IFC left/right requires a
        # stable host-wall direction, so this remains explicitly unknown.
        "hinge_side": "unknown",
        "confidence": confidence,
        "analysis_method": "hough_leaf_arc",
        "needs_review": True,
        "review_reason": "hinge operation requires host-wall-relative verification",
        "observable_from_plan": True,
    }


def enhancedDoorAnalysis(door_objects, masks, door_indices, image_width, image_height):
    enhanced_doors = []
    for i, door in enumerate(door_objects):
        mask_index = door_indices[i] if i < len(door_indices) else None
        door_mask = None
        if masks is not None and mask_index is not None and mask_index < masks.shape[2]:
            door_mask = masks[:, :, mask_index]
        bbox = door["bbox"]
        bbox_array = [bbox["y1"], bbox["x1"], bbox["y2"], bbox["x2"]]
        orientation = analyzeDoorOrientation(
            door_mask, bbox_array, image_width, image_height
        )
        enhanced = dict(door)
        enhanced["orientation"] = orientation
        enhanced["architectural_notes"] = generateArchitecturalNotes(orientation, bbox)
        enhanced_doors.append(enhanced)
    return enhanced_doors


def generateArchitecturalNotes(orientation, bbox):
    del bbox
    notes = []
    swing = orientation.get("estimated_swing", "unknown")
    if swing == "unknown":
        notes.append("Door swing is not reliably observable; manual verification required")
    else:
        notes.append(f"Image-plane swing symbol detected: {swing}")
    if orientation.get("needs_review"):
        notes.append(orientation.get("review_reason") or "Manual verification recommended")
    return notes


def categorize_door_size(width_mm, height_mm):
    """Broad geometric category using calibrated millimetres, never pixels."""
    width = float(width_mm)
    height = float(height_mm)
    if width < 700:
        return "narrow"
    if width <= 1100 and height >= 1800:
        return "typical_single_leaf"
    if width <= 1800:
        return "wide_or_double_leaf"
    return "oversized"


def assess_door_accessibility(width_mm):
    """Accessibility cannot be concluded from a plan symbol alone."""
    return {
        "status": "not_observable_from_plan",
        "measured_width_mm": float(width_mm),
        "reason": (
            "Clear opening, hardware, thresholds, approach space and project-specific "
            "accessibility obligations require separate evidence"
        ),
    }


def generate_door_layout_insights(doors, image_width, image_height):
    del image_width, image_height
    if not doors:
        return ["No doors detected for layout analysis"]
    unknown = sum(
        1 for d in doors if d.get("orientation", {}).get("estimated_swing") == "unknown"
    )
    return [f"{unknown} door swing(s) require manual verification"] if unknown else []


def convert_door_center_to_mm(door_data, scale_factor_mm_per_pixel):
    if not door_data or "location" not in door_data or "center" not in door_data["location"]:
        return door_data
    door_mm = dict(door_data)
    center_pixels = door_data["location"]["center"]
    if "x" in center_pixels and "y" in center_pixels:
        door_mm.setdefault("location", dict(door_data["location"]))
        door_mm["location"]["center_mm"] = {
            "x": pixels_to_mm(center_pixels["x"], scale_factor_mm_per_pixel),
            "y": pixels_to_mm(center_pixels["y"], scale_factor_mm_per_pixel),
        }
    return door_mm
