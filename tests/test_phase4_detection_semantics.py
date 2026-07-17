from __future__ import annotations

from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from analysis.door_analysis import (
    analyzeDoorOrientation,
    assess_door_accessibility,
)
from analysis.window_analysis import analyze_window_geometry, assess_window_glazing
from export.ifc_exporter import _hinge_to_operation
from services.bim_builder import BimDataBuilder
from services.image_validation import validate_and_resize_image
from services.preprocessing import decide_office_enhancement
from services.room_taxonomy import VOCABULARY_VERSION, normalize_room_categories
from services.yolo_elements import build_yolo_elements
from validation.bim_checks import validate_bim_data


def _wall_parameters():
    return [
        {
            "wall_id": "W1",
            "centerline": [[0, 0], [3000, 0], [3000, 2500]],
            "thickness": {"average": 200},
        }
    ]


def _door(host="W1"):
    return {
        "door_id": 1,
        "host_wall_id": host,
        "host_wall_confidence": 0.9,
        "location": {"center": {"x": 1500, "y": 0}},
        "dimensions": {"width": 900},
        "orientation": {
            "estimated_swing": "unknown",
            "hinge_side": "unknown",
            "analysis_method": "bbox_axis_only",
            "confidence": 0.2,
            "needs_review": True,
            "review_reason": "mask unavailable",
        },
        "needs_review": True,
        "review_reason": "mask unavailable",
    }


def _window(host="W1"):
    return {
        "window_id": 1,
        "host_wall_id": host,
        "host_wall_confidence": 0.8,
        "location": {"center": {"x": 3000, "y": 1200}},
        "dimensions": {"width": 1200},
        "window_type": "vertical",
    }


def test_missing_door_mask_never_invents_swing_from_image_position():
    left = analyzeDoorOrientation(None, [10, 10, 80, 30], 1000, 1000)
    right = analyzeDoorOrientation(None, [10, 900, 80, 920], 1000, 1000)
    assert left["estimated_swing"] == right["estimated_swing"] == "unknown"
    assert left["analysis_method"] == "bbox_axis_only"
    assert left["needs_review"] is True


def test_hough_leaf_and_arc_detect_visible_swing_without_claiming_ifc_hinge():
    mask = np.zeros((120, 160), np.uint8)
    cv2.line(mask, (30, 60), (120, 60), 1, 2)
    cv2.ellipse(mask, (30, 60), (90, 45), 0, 0, 90, 1, 2)
    result = analyzeDoorOrientation(mask, [10, 10, 110, 150])
    assert result["estimated_swing"] == "opens_downward"
    assert result["analysis_method"] == "hough_leaf_arc"
    assert result["observable_from_plan"] is True
    assert result["hinge_side"] == "unknown"
    assert result["needs_review"] is True


def test_unknown_hinge_exports_as_notdefined_not_fake_left_swing():
    assert _hinge_to_operation("unknown") == "NOTDEFINED"
    assert _hinge_to_operation(None) == "NOTDEFINED"


def test_nonobservable_metadata_is_explicit():
    assert assess_door_accessibility(900)["status"] == "not_observable_from_plan"
    assert assess_window_glazing()["status"] == "not_observable_from_plan"


def test_window_geometry_uses_mask_when_available():
    mask = np.zeros((100, 150), np.uint8)
    rect = ((75, 50), (80, 12), 25)
    cv2.fillPoly(mask, [cv2.boxPoints(rect).astype(np.int32)], 1)
    result = analyze_window_geometry(mask, [20, 20, 80, 130])
    assert result["geometry_source"] == "mask_min_area_rect"
    assert result["major_axis_px"] == pytest.approx(80, abs=3)
    assert result["minor_axis_px"] == pytest.approx(12, abs=3)


def test_exif_orientation_is_applied_before_size_validation():
    image = Image.new("RGB", (120, 240), "white")
    exif = Image.Exif()
    exif[274] = 6
    buf = BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    buf.seek(0)
    reopened = Image.open(buf)
    normalized, info = validate_and_resize_image(reopened, max_size=500)
    assert normalized.size == (240, 120)
    assert info["exif_transposed"] is True


def test_office_enhancement_is_disabled_by_default_and_auditable():
    image = Image.new("RGB", (200, 200), "white")
    disabled = decide_office_enhancement(image, mode="disabled")
    auto = decide_office_enhancement(image, mode="auto", threshold=0.5)
    assert disabled.office_enhancement_applied is False
    assert disabled.reason == "disabled_pending_reference_dataset_validation"
    assert auto.office_enhancement_applied is True
    assert auto.edge_density == 0.0


def test_bim_builder_preserves_polyline_segments_and_externality():
    builder = BimDataBuilder({})
    bim = builder.build(
        _wall_parameters(),
        [_door()],
        [_window()],
        [], [], [],
        [{
            "wall_id": "W1",
            "exterior_confidence": 0.73,
            "exterior_reasons": ["boundary evidence"],
            "exterior_needs_review": True,
        }],
        scale={"mm_per_pixel": 5, "source": "manual"},
    )
    wall = bim["walls"][0]
    assert wall["segment_ids"] == ["W1__seg_1", "W1__seg_2"]
    assert len(wall["segments"]) == 2
    assert bim["doors"][0]["is_exterior"] is True
    assert bim["doors"][0]["externality_confidence"] == pytest.approx(0.73)
    assert bim["windows"][0]["is_exterior"] is True
    assert bim["windows"][0]["glazing"]["status"] == "not_observable_from_plan"


def test_preexport_opening_validation_uses_nearest_polyline_segment():
    bim = {
        "coordinate_system": {"units": "millimeters"},
        "walls": [{
            "id": "W1",
            "centerline": [[0, 0, 0], [3000, 0, 0], [3000, 2500, 0]],
            "start_point": [0, 0, 0],
            "end_point": [3000, 2500, 0],
            "thickness": 200,
            "height": 2800,
        }],
        "doors": [{
            "id": "D1", "host_wall_id": "W1",
            "insertion_point": [3000, 1200, 0],
            "width": 900, "height": 2100,
        }],
        "windows": [], "rooms": [],
    }
    report = validate_bim_data(bim)
    codes = {issue.code for issue in report.issues}
    assert "GEOM.DOOR.OFF_WALL" not in codes
    assert "GEOM.DOOR.PAST_END" not in codes


def test_room_taxonomy_uses_shared_contract_without_global_alias_mutation():
    root = Path(__file__).resolve().parents[1]
    canonical = root / "contracts" / "controlled_values_v1.yaml"
    engine_fallback = root / "compliance-engine" / "standards" / "controlled_values.yaml"
    assert canonical.read_bytes() == engine_fallback.read_bytes()

    first = {"rooms": [{"category": "custom lab", "name": "custom lab"}]}
    normalize_room_categories(first, extra_aliases={"custom lab": "room_storage"})
    assert first["rooms"][0]["category"] == "room_storage"

    second = {"rooms": [{"category": "custom lab", "name": "custom lab"}]}
    summary = normalize_room_categories(second)
    assert summary["vocabulary_version"] == VOCABULARY_VERSION
    assert second["rooms"][0]["category_source"] == "unmapped"


def test_yolo_elements_are_marked_approximate_and_mapped_by_bucket():
    detections = [
        {"element_type": "Stairs", "bucket": "stairs", "bbox": [0, 0, 20, 40], "confidence": 0.8},
        {"element_type": "Column", "bucket": "columns", "bbox": [5, 5, 15, 15], "confidence": 0.7},
        {"element_type": "Railing", "bucket": "railings", "bbox": [10, 10, 12, 50], "confidence": 0.6},
    ]
    result = build_yolo_elements(detections, 10)
    assert len(result["stairs"]) == len(result["columns"]) == len(result["railings"]) == 1
    assert result["stairs"][0]["mask_based"] is False
    assert result["stairs"][0]["source"] == "yolo"
    assert result["columns"][0]["width_mm"] == pytest.approx(100)
