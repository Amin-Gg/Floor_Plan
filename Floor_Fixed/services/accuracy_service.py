"""
Accuracy analysis services
"""

import numpy
import logging
from utils.geometry import calculateOverlap
from image_processing.image_loader import getClassName
from config.classes import PROJECT_ID_TO_NAME

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Human-readable plural names for the object_analysis report block
# ─────────────────────────────────────────────────────────────────────────────
# By convention, the accuracy report uses plural forms ("walls" not "wall").
# For names that just need an "s" appended this is automatic; the overrides
# below handle the few irregular cases. If you add a new class to
# config/classes.py whose pluralization is non-standard, add it here.
_PLURAL_OVERRIDES = {
    "balcony":       "balconies",
    "terrace":       "terraces",
    "parking":       "parking",        # already plural in usage
    "stairs":        "stairs",         # already plural in usage
    "room_bedroom":  "bedrooms",
    "room_living":   "living_rooms",
    "room_kitchen":  "kitchens",
    "room_bathroom": "bathrooms",
    "room_entry":    "entries",
    "room_storage":  "storage_rooms",
}


def _plural_name(class_name: str) -> str:
    """Return the plural form used in the object_analysis report block."""
    if class_name in _PLURAL_OVERRIDES:
        return _PLURAL_OVERRIDES[class_name]
    return class_name + "s"


def performAccuracyAnalysis(model_results, image_width, image_height):
    """Perform comprehensive accuracy analysis of model predictions"""

    bboxes    = model_results['rois']
    class_ids = model_results['class_ids']
    scores    = model_results['scores']
    masks     = model_results['masks']

    # Build object_analysis dynamically from the project's class registry.
    # New classes added to config/classes.py are picked up automatically.
    empty_stat = {"count": 0, "avg_confidence": 0.0, "avg_size": 0.0}
    object_analysis = {
        _plural_name(name): dict(empty_stat)
        for name in PROJECT_ID_TO_NAME.values()
    }

    analysis = {
        "overall_metrics": {
            "total_detections": len(bboxes),
            "image_coverage": 0.0,
            "average_confidence": float(numpy.mean(scores)) if len(scores) > 0 else 0.0,
            "confidence_distribution": {}
        },
        "detection_quality": {
            "high_confidence":   [],   # > 0.8
            "medium_confidence": [],   # 0.5 – 0.8
            "low_confidence":    []    # < 0.5
        },
        "object_analysis": object_analysis,
        "spatial_analysis": {
            "bbox_overlaps":   0,
            "size_anomalies":  [],
            "position_analysis": {}
        },
        "reliability_score": 0.0,
        "recommendations": []
    }

    total_area_covered = 0
    # class_data has one bucket per project class, plus bucket 0 for any
    # unexpected class IDs that slip through (e.g. detections from a model
    # version using a different class space than the current code).
    class_data = {pid: [] for pid in PROJECT_ID_TO_NAME}
    class_data[0] = []   # safety bucket for unknown class IDs

    for i in range(len(bboxes)):
        bbox       = bboxes[i]
        class_id   = int(class_ids[i])
        confidence = float(scores[i])

        y1, x1, y2, x2 = bbox
        bbox_area = (x2 - x1) * (y2 - y1)
        total_area_covered += bbox_area

        # Use key 0 as a safe bucket for any unexpected class IDs
        bucket = class_id if class_id in class_data else 0
        class_data[bucket].append({
            "confidence": confidence,
            "area":       bbox_area,
            "bbox":       [float(x1), float(y1), float(x2), float(y2)]
        })

        detection_info = {
            "id":         i,
            "type":       getClassName(class_id),
            "confidence": confidence,
            "area":       float(bbox_area),
            "bbox":       [float(x1), float(y1), float(x2), float(y2)]
        }

        if confidence > 0.8:
            analysis["detection_quality"]["high_confidence"].append(detection_info)
        elif confidence > 0.5:
            analysis["detection_quality"]["medium_confidence"].append(detection_info)
        else:
            analysis["detection_quality"]["low_confidence"].append(detection_info)

    image_area = image_width * image_height
    analysis["overall_metrics"]["image_coverage"] = float(
        total_area_covered / image_area * 100) if image_area > 0 else 0.0

    # Fill object_analysis from class_data, driven by the live class registry.
    for class_id, class_name in PROJECT_ID_TO_NAME.items():
        plural = _plural_name(class_name)
        if class_data[class_id]:
            data = class_data[class_id]
            analysis["object_analysis"][plural] = {
                "count":            len(data),
                "avg_confidence":   float(numpy.mean([d["confidence"] for d in data])),
                "avg_size":         float(numpy.mean([d["area"] for d in data])),
                "confidence_range": {
                    "min": float(min(d["confidence"] for d in data)),
                    "max": float(max(d["confidence"] for d in data))
                }
            }

    # Overlap analysis
    overlaps = 0
    for i in range(len(bboxes)):
        for j in range(i + 1, len(bboxes)):
            if calculateOverlap(bboxes[i], bboxes[j]) > 0.3:
                overlaps += 1
    analysis["spatial_analysis"]["bbox_overlaps"] = overlaps

    # Size anomaly check (walls only — they are the most numerous and measurable)
    if class_data[1]:
        wall_areas        = [d["area"] for d in class_data[1]]
        median_wall_area  = numpy.median(wall_areas)
        for i, area in enumerate(wall_areas):
            if area > median_wall_area * 5 or area < median_wall_area * 0.2:
                analysis["spatial_analysis"]["size_anomalies"].append({
                    "type":         "wall",
                    "detection_id": i,
                    "area":         float(area),
                    "median_area":  float(median_wall_area),
                    "reason":       "unusually_large" if area > median_wall_area * 5 else "unusually_small"
                })

    # Reliability score (0-100)
    reliability_factors = []
    avg_confidence = analysis["overall_metrics"]["average_confidence"]
    reliability_factors.append(min(40, avg_confidence * 40))

    high_conf_ratio = len(analysis["detection_quality"]["high_confidence"]) / max(1, len(bboxes))
    reliability_factors.append(high_conf_ratio * 30)

    overlap_penalty = max(0, 20 - overlaps * 5)
    reliability_factors.append(overlap_penalty)

    detection_count_score = 10 if 1 <= len(bboxes) <= 50 else max(0, 10 - abs(len(bboxes) - 25))
    reliability_factors.append(detection_count_score)

    analysis["reliability_score"] = sum(reliability_factors)

    # Recommendations
    recommendations = []
    if avg_confidence < 0.6:
        recommendations.append("Low average confidence. Consider higher quality images or more training.")
    if len(analysis["detection_quality"]["low_confidence"]) > len(bboxes) * 0.3:
        recommendations.append("Many low-confidence detections found. Review these detections manually.")
    if overlaps > 0:
        recommendations.append(f"Found {overlaps} overlapping detections. Check for duplicate objects.")
    if analysis["spatial_analysis"]["size_anomalies"]:
        recommendations.append(
            f"Found {len(analysis['spatial_analysis']['size_anomalies'])} size anomalies. Review unusual object sizes.")
    if analysis["reliability_score"] > 80:
        recommendations.append("High reliability score! Results appear very accurate.")
    elif analysis["reliability_score"] > 60:
        recommendations.append("Good reliability score. Results are generally trustworthy.")
    else:
        recommendations.append("Low reliability score. Carefully review all detections.")

    analysis["recommendations"] = recommendations
    return analysis
