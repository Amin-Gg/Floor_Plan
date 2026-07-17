"""Observable window geometry helpers."""
from __future__ import annotations

import cv2
import numpy as np


def analyze_window_geometry(window_mask, bbox) -> dict:
    """Use the segmentation mask when available; fall back explicitly to bbox."""
    y1, x1, y2, x2 = [float(v) for v in bbox]
    bbox_w = abs(x2 - x1)
    bbox_h = abs(y2 - y1)
    result = {
        "major_axis_px": max(bbox_w, bbox_h),
        "minor_axis_px": min(bbox_w, bbox_h),
        "orientation": "horizontal" if bbox_w >= bbox_h else "vertical",
        "rotation_angle_deg": 0.0 if bbox_w >= bbox_h else 90.0,
        "geometry_source": "bbox",
    }
    if window_mask is None:
        return result
    binary = (np.asarray(window_mask) > 0).astype(np.uint8)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return result
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 4:
        return result
    (_, _), (rw, rh), angle = cv2.minAreaRect(contour)
    major = max(float(rw), float(rh))
    minor = min(float(rw), float(rh))
    if major <= 0 or minor <= 0:
        return result
    result.update({
        "major_axis_px": major,
        "minor_axis_px": minor,
        "orientation": "horizontal" if bbox_w >= bbox_h else "vertical",
        "rotation_angle_deg": float(angle),
        "geometry_source": "mask_min_area_rect",
    })
    return result


def categorize_window_size(width_mm, height_mm):
    width = float(width_mm)
    area_m2 = max(width, 0.0) * max(float(height_mm), 0.0) / 1_000_000.0
    if area_m2 < 0.5:
        return "small"
    if area_m2 < 2.0:
        return "typical"
    if area_m2 < 4.0:
        return "large"
    return "oversized"


def assess_window_glazing(width=None, height=None):
    del width, height
    return {
        "status": "not_observable_from_plan",
        "reason": "Pane count and glazing build-up are not encoded by a 2D plan symbol",
    }


def generate_window_notes(width_mm, height_mm, window_type):
    del height_mm
    return [
        f"Plan-symbol major axis: {float(width_mm):.1f} mm",
        f"Image-plane orientation: {window_type}",
        "Glazing specification requires schedule, detail, or manual input",
    ]
