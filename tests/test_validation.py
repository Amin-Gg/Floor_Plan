"""
tests/test_validation.py
========================
Regression tests for the model-standards validator (validation/).

Pre-export tests are pure Python (no ifcopenshell needed). The post-export
end-to-end test is skipped automatically if ifcopenshell is not installed.

Run:  pytest tests/test_validation.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from validation import (                                   # noqa: E402
    validate_bim_data, validate_ifc_file, merge_reports, Severity,
)


# ── fixtures ──────────────────────────────────────────────────────────────────
def _clean_bim():
    """A small but fully valid 4-wall room with one door and one window."""
    return {
        "coordinate_system": {"units": "millimeters"},
        "walls": [
            {"id": 1, "start_point": [0, 0, 0],    "end_point": [5000, 0, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": 2, "start_point": [5000, 0, 0], "end_point": [5000, 4000, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": 3, "start_point": [5000, 4000, 0], "end_point": [0, 4000, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": 4, "start_point": [0, 4000, 0], "end_point": [0, 0, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
        ],
        "doors": [
            {"id": "Door_1", "host_wall_id": 1, "insertion_point": [2500, 0, 0],
             "width": 900, "height": 2100, "orientation": {"hinge_side": "left"}},
        ],
        "windows": [
            {"id": "Window_1", "host_wall_id": 2, "insertion_point": [5000, 2000, 0],
             "width": 1200, "height": 1200, "sill_height": 900, "window_type": "fixed"},
        ],
        "rooms": [
            {"id": "Room_1", "name": "Bedroom", "category": "Bedroom",
             "needs_review": False, "name_source": "ocr", "area_m2": 20.0,
             "perimeter_m": 18.0, "centroid_mm": [2500, 2000],
             "polygon": [[100, 100], [4900, 100], [4900, 3900], [100, 3900], [100, 100]]},
        ],
        "stairs": [], "slabs": [],
    }


def _codes(report):
    return {i.code for i in report.issues}


# ── pre-export: clean model ───────────────────────────────────────────────────
def test_clean_bim_passes():
    r = validate_bim_data(_clean_bim())
    assert not r.blocked
    assert r.n_critical == 0
    assert r.status == "pass"


# ── pre-export: each critical is detected ─────────────────────────────────────
def test_zero_length_wall_is_critical():
    bim = _clean_bim()
    bim["walls"][0]["end_point"] = [0, 0, 0]      # collapse wall 1
    r = validate_bim_data(bim)
    assert r.blocked
    assert "GEOM.WALL.ZERO_LENGTH" in _codes(r)


def test_zero_thickness_wall_is_critical():
    bim = _clean_bim()
    bim["walls"][0]["thickness"] = 0
    r = validate_bim_data(bim)
    assert r.blocked
    assert "COMPLETE.WALL.NO_THICKNESS" in _codes(r)


def test_missing_host_wall_is_critical():
    bim = _clean_bim()
    bim["doors"][0]["host_wall_id"] = 999
    r = validate_bim_data(bim)
    assert r.blocked
    assert "COMPLETE.DOOR.HOST_MISSING" in _codes(r)


def test_door_off_host_wall_is_critical():
    bim = _clean_bim()
    bim["doors"][0]["insertion_point"] = [2500, 3000, 0]   # far from wall 1 (y=0)
    r = validate_bim_data(bim)
    assert r.blocked
    assert "GEOM.DOOR.OFF_WALL" in _codes(r)


def test_degenerate_room_is_critical():
    bim = _clean_bim()
    bim["rooms"][0]["polygon"] = [[0, 0], [1, 0]]
    r = validate_bim_data(bim)
    assert r.blocked
    assert "GEOM.ROOM.DEGENERATE" in _codes(r)


def test_no_walls_is_critical():
    bim = _clean_bim()
    bim["walls"] = []
    r = validate_bim_data(bim)
    assert r.blocked
    assert "COMPLETE.WALL.NONE" in _codes(r)


# ── pre-export: warnings do not block ─────────────────────────────────────────
def test_untyped_room_warns_but_does_not_block():
    bim = _clean_bim()
    bim["rooms"][0].update({"category": "Unknown", "needs_review": True,
                            "name_source": "none"})
    r = validate_bim_data(bim)
    assert not r.blocked                       # warn-on-minor
    assert "CODE.ROOM.UNTYPED" in _codes(r)
    assert any(i.severity is Severity.WARNING for i in r.issues)


def test_implausible_scale_warns():
    bim = _clean_bim()
    bim["walls"][0]["thickness"] = 5           # 5mm wall — implausible
    r = validate_bim_data(bim)
    assert "GEOM.WALL.THICKNESS_RANGE" in _codes(r)


# ── report plumbing ───────────────────────────────────────────────────────────
def test_report_to_dict_and_merge():
    r = validate_bim_data(_clean_bim())
    d = r.to_dict()
    assert d["stage"] == "pre_export"
    assert set(d["counts"]) == {"critical", "warning", "info"}
    env = merge_reports("export", r, r)
    assert env["blocked"] is False
    assert "stages" in env and len(env["stages"]) == 2


# ── post-export: full end-to-end (needs ifcopenshell + numpy) ─────────────────
def test_post_export_clean_model_has_no_criticals(tmp_path):
    pytest.importorskip("ifcopenshell")
    pytest.importorskip("numpy")
    from export.ifc_exporter import bim_json_to_ifc

    out = str(tmp_path / "clean.ifc")
    bim_json_to_ifc(_clean_bim(), {}, out)
    post = validate_ifc_file(out)
    assert post.n_critical == 0, [i.to_dict() for i in post.issues]
    # With Pset_*Common now written by the exporter, a clean model is warning-free.
    assert "COMPLETE.PSET.MISSING" not in _codes(post)


def test_post_export_detects_bad_file(tmp_path):
    pytest.importorskip("ifcopenshell")
    bad = tmp_path / "not_really.ifc"
    bad.write_text("this is not an IFC file")
    post = validate_ifc_file(str(bad))
    assert post.blocked
    assert "IFC4.PARSE.FAILED" in _codes(post)
