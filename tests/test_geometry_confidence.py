"""
tests/test_geometry_confidence.py
=================================
Issues 5 / 6 / 7 — geometry-reliability confidence + review flagging.

Covers the acceptance scenarios from the review:
  * Issue 5 — suspicious rooms (tiny / sliver / cropped / untyped) are flagged.
  * Issue 6 — host-wall binding: missing, ambiguous, and far (wrong) hosts.
  * Issue 7 — exterior classification confidence (strong boundary vs weak).
  * Propagation — the builder carries the flags onto bim_data elements, and a
    window on a low-confidence-exterior wall inherits review.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Issue 5 — room quality ───────────────────────────────────────────────────
def test_room_quality_good_room_is_confident():
    from analysis.room_analysis import _assess_room_quality
    conf, needs, reasons = _assess_room_quality(
        "ocr", 12.0, 3000, 4000, [[1000, 1000]] * 5, 10000, 10000)
    assert conf == 1.0 and needs is False and reasons == []


@pytest.mark.parametrize("ns,area,bw,bh,poly,desc", [
    ("ocr", 0.8, 900, 900, [[1000, 1000]] * 5, "tiny area"),
    ("ocr", 5.0, 500, 12000, [[1000, 1000]] * 5, "thin sliver"),
    ("none", 12.0, 3000, 4000, [[1000, 1000]] * 5, "no OCR type"),
    ("ocr", 12.0, 3000, 4000,
     [[0, 1000], [3000, 1000], [3000, 5000], [0, 5000], [0, 1000]], "border"),
])
def test_room_quality_suspicious_is_flagged(ns, area, bw, bh, poly, desc):
    from analysis.room_analysis import _assess_room_quality
    conf, needs, reasons = _assess_room_quality(ns, area, bw, bh, poly, 10000, 10000)
    assert needs is True, desc
    assert conf < 0.65, desc
    assert reasons, desc


# ── Issue 6 — host-wall binding ──────────────────────────────────────────────
def test_host_wall_close_single_is_confident():
    from analysis.room_analysis import assess_host_wall
    walls = [{"wall_id": "A", "centerline": [[0, 0], [2000, 0]]},
             {"wall_id": "B", "centerline": [[0, 2000], [2000, 2000]]}]
    hb = assess_host_wall([1000, 100], walls)
    assert hb["host_wall_id"] == "A" and hb["needs_review"] is False
    assert hb["host_wall_confidence"] > 0.7


def test_host_wall_ambiguous_is_flagged():
    from analysis.room_analysis import assess_host_wall
    amb = [{"wall_id": "A", "centerline": [[0, 1000], [2000, 1000]]},
           {"wall_id": "B", "centerline": [[1000, 0], [1000, 2000]]}]
    hb = assess_host_wall([1000, 1000], amb)
    assert hb["needs_review"] is True
    assert set(hb["candidate_host_walls"]) == {"A", "B"}


def test_host_wall_too_far_yields_no_host():
    from analysis.room_analysis import assess_host_wall
    hb = assess_host_wall([1000, 1500], [{"wall_id": "A",
                                          "centerline": [[0, 0], [2000, 0]]}])
    assert hb["host_wall_id"] is None and hb["needs_review"] is True


# ── Issue 7 — exterior classification ────────────────────────────────────────
def test_exterior_strong_vs_weak_confidence():
    from analysis.wall_analysis import identify_exterior_walls
    wp = [{"wall_id": "W1", "bbox": {"x1": 0, "y1": 5000, "x2": 100, "y2": 9000},
           "connections": {"start_junction": 1, "end_junction": 2}, "length": 4000},
          {"wall_id": "W2", "bbox": {"x1": 5000, "y1": 5000, "x2": 5100, "y2": 5100},
           "connections": {}, "length": 100}]
    ext, _ = identify_exterior_walls(wp, 1000, 1000, 10.0)
    by_id = {w["wall_id"]: w for w in ext}
    assert by_id["W1"]["exterior_confidence"] >= 0.7
    assert by_id["W1"]["exterior_needs_review"] is False
    assert by_id["W2"]["exterior_confidence"] <= 0.5      # "unconnected" only
    assert by_id["W2"]["exterior_needs_review"] is True


# ── Propagation through the builder ──────────────────────────────────────────
def test_builder_propagates_flags_and_window_inherits_exterior():
    from services.bim_builder import BimDataBuilder
    out = BimDataBuilder({}).build(
        wall_parameters=[{"wall_id": "W2", "centerline": [[0, 0], [4000, 0]],
                          "thickness": {"average": 200}}],
        detailed_doors=[{"door_id": 1, "host_wall_id": "W2",
                         "host_wall_confidence": 0.3, "needs_review": True,
                         "review_reason": "ambiguous host",
                         "location": {"center": {"x": 2000, "y": 0}},
                         "dimensions": {"width": 900},
                         "orientation": {"hinge_side": "left"}}],
        detailed_windows=[{"window_id": 1, "host_wall_id": "W2",
                           "host_wall_confidence": 0.9, "needs_review": False,
                           "review_reason": "",
                           "location": {"center": {"x": 1000, "y": 0}},
                           "dimensions": {"width": 1200}, "window_type": "fixed"}],
        room_polygons=[], bim_stairs=[], bim_slabs=[],
        exterior_walls=[{"wall_id": "W2", "exterior_confidence": 0.4,
                         "exterior_reasons": ["unconnected"],
                         "exterior_needs_review": True}],
        scale={"mm_per_pixel": 25.0, "source": "user"})
    door, win = out["doors"][0], out["windows"][0]
    assert door["needs_review"] is True and door["confidence"] == 0.3
    # window's own binding was fine (0.9), but the weak-exterior host drags it down
    assert win["needs_review"] is True and win["confidence"] == 0.4
