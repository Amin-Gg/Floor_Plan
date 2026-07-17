"""Geometry primitives used by the deterministic evaluator."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw


def bbox_iou(a: list[float], b: list[float]) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _mask_from_spec(spec: dict[str, Any] | None, *, width: int, height: int, base: Path) -> np.ndarray | None:
    if not spec:
        return None
    kind = spec.get("type")
    if kind == "polygon":
        points = spec.get("points")
        if not isinstance(points, list) or len(points) < 3:
            return None
        image = Image.new("1", (width, height), 0)
        ImageDraw.Draw(image).polygon([(float(p[0]), float(p[1])) for p in points], fill=1)
        return np.asarray(image, dtype=bool)
    if kind == "png":
        value = spec.get("path")
        if not isinstance(value, str) or not value:
            return None
        path = (base / value).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        image = Image.open(path).convert("1")
        if image.size != (width, height):
            return None
        return np.asarray(image, dtype=bool)
    return None


def instance_iou(
    ground_truth: dict[str, Any],
    prediction: dict[str, Any],
    *,
    width: int,
    height: int,
    gt_base: Path,
    pred_base: Path,
) -> tuple[float, str]:
    gt_mask = _mask_from_spec(ground_truth.get("mask"), width=width, height=height, base=gt_base)
    pred_mask = _mask_from_spec(prediction.get("mask"), width=width, height=height, base=pred_base)
    if gt_mask is not None and pred_mask is not None:
        intersection = np.logical_and(gt_mask, pred_mask).sum(dtype=np.float64)
        union = np.logical_or(gt_mask, pred_mask).sum(dtype=np.float64)
        return (float(intersection / union) if union else 0.0, "mask")
    return bbox_iou(ground_truth["bbox_xyxy"], prediction["bbox_xyxy"]), "bbox"


def bbox_center(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def center_distance(a: list[float], b: list[float]) -> float:
    ax, ay = bbox_center(a)
    bx, by = bbox_center(b)
    return math.hypot(ax - bx, ay - by)


def relative_size_error(reference: list[float], predicted: list[float]) -> tuple[float, float]:
    ref_w, ref_h = reference[2] - reference[0], reference[3] - reference[1]
    pred_w, pred_h = predicted[2] - predicted[0], predicted[3] - predicted[1]
    width_error = abs(pred_w - ref_w) / ref_w if ref_w else 0.0
    height_error = abs(pred_h - ref_h) / ref_h if ref_h else 0.0
    return width_error, height_error


def circular_angle_error(reference: float, predicted: float) -> float:
    return abs((predicted - reference + 180.0) % 360.0 - 180.0)


def point_to_segment_distance(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def polyline_hausdorff(reference: list[list[float]], predicted: list[list[float]]) -> float | None:
    if len(reference) < 2 or len(predicted) < 2:
        return None
    ref = [(float(p[0]), float(p[1])) for p in reference]
    pred = [(float(p[0]), float(p[1])) for p in predicted]

    def directed(points: list[tuple[float, float]], target: list[tuple[float, float]]) -> float:
        values = []
        for point in points:
            values.append(min(point_to_segment_distance(point, target[i], target[i + 1]) for i in range(len(target) - 1)))
        return max(values, default=0.0)

    return max(directed(ref, pred), directed(pred, ref))
