#!/usr/bin/env python3
"""
test_room_binding.py — offline room-extraction / OCR-binding / host-wall tests.

Exercises the REAL analysis/room_analysis.py on a synthetic two-room plan. Needs
only numpy + opencv (no TensorFlow, no .h5, no graphics server).

Issue 14 — this used to run as a script and called sys.exit() at import, which
crashed `pytest` collection. It is now collected cleanly: missing deps SKIP
(not exit), each test is self-contained, and there is no module-level execution.
Run with `pytest tests/test_room_binding.py`, or `python tests/test_room_binding.py`.
"""

import os
import sys

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")   # SKIP (don't crash) if opencv is unavailable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from analysis.room_analysis import extract_room_polygons, find_host_wall_id
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"analysis.room_analysis not importable: {exc}",
                allow_module_level=True)

SCALE_MM_PER_PX = 15.0   # chambers ≈ 34 m² each — safely above MIN_ROOM_AREA_M2
SPACE_NAMES = [
    {"insertion_point": [800 * 0.25, 600 * 0.5], "name": "Bedroom",
     "local_name": "اتاق خواب", "category": "Accommodation"},
    {"insertion_point": [800 * 0.75, 600 * 0.5], "name": "Kitchen",
     "local_name": "آشپزخانه", "category": "Service"},
]


def _build_two_room_plan(H=600, W=800, t=8):
    """Outer wall box + one interior dividing wall → two chambers."""
    wall = np.zeros((H, W), np.uint8)
    cv2.rectangle(wall, (60, 60), (W - 60, H - 60), 255, t)   # outer walls
    cv2.line(wall, (W // 2, 60), (W // 2, H - 60), 255, t)     # divider
    return wall.astype(bool)


@pytest.fixture(scope="module")
def rooms_with_ocr():
    return extract_room_polygons(_build_two_room_plan(), SCALE_MM_PER_PX, SPACE_NAMES)


def test_extract_finds_two_rooms_with_valid_geometry(rooms_with_ocr):
    assert len(rooms_with_ocr) == 2, f"expected 2 rooms, got {len(rooms_with_ocr)}"
    for r in rooms_with_ocr:
        for k in ("id", "name", "polygon", "area_m2", "perimeter_m",
                  "centroid_mm", "vertex_count"):
            assert k in r, f"room dict missing key '{k}'"
        assert r["area_m2"] > 0
        assert len(r["polygon"]) >= 4


def test_rooms_carry_confidence_review_fields(rooms_with_ocr):
    # Issue 5 — every room must expose confidence / needs_review / review_reasons.
    for r in rooms_with_ocr:
        assert isinstance(r.get("confidence"), (int, float))
        assert "needs_review" in r
        assert isinstance(r.get("review_reasons"), list)


def test_ocr_names_bind_to_correct_chambers(rooms_with_ocr):
    names = sorted(r["name"] for r in rooms_with_ocr)
    assert names == ["Bedroom", "Kitchen"], f"OCR names not bound: {names}"


def test_no_ocr_run_still_extracts_rooms():
    rooms = extract_room_polygons(_build_two_room_plan(), SCALE_MM_PER_PX, None)
    assert len(rooms) == 2
    assert all(isinstance(r.get("name"), str) for r in rooms)


def test_host_wall_nearest_and_graceful_none():
    wall_parameters = [
        {"wall_id": "Wall_Top",    "centerline": [[0, 0],    [5000, 0]]},
        {"wall_id": "Wall_Bottom", "centerline": [[0, 5000], [5000, 5000]]},
    ]
    assert find_host_wall_id([2500, 120], wall_parameters) == "Wall_Top"
    assert find_host_wall_id([2500, 2500], []) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
