"""
tests/test_numeric_property_routing.py
======================================
Regression tests for the property-aware measurement fix in numeric_checker.

The original OBJECT_MAP keyed only on the clause `object`, so two clauses that
shared an object but differed in `property` were measured identically:

    * door "clear width" and door "clear height"  -> both measured door WIDTH
    * dwelling "area", "width", "height"           -> all measured room AREA

That produced false PASS/FAIL (a 2.1 m door FAILing a 2.05 m height rule; a room
PASSing a width rule on its area). These tests pin the corrected behaviour:
the measurement now follows `property`, and unmeasurable pairings degrade to
NEEDS_REVIEW instead of measuring the wrong quantity.
"""
import sys
sys.path.insert(0, "services")  # flat intra-package imports (matches pyproject)

import pytest
from numeric_checker import NumericChecker, Verdict


BIM = {
    "rooms": [
        {"id": "R1", "category": "room_bedroom", "area_m2": 12.0,
         "dimensions": {"width_mm": 3000, "length_mm": 4000}},
    ],
    "doors": [
        {"id": "D1", "width": 900, "height": 2100},   # 0.9 m wide, 2.1 m tall
    ],
    "windows": [],
}


def _clause(aid, obj, prop, comp, value, unit):
    return {"article_id": aid, "rule_type": "numeric", "text_en": aid,
            "entities": {"object": obj, "property": prop, "comparator": comp,
                         "value": value, "unit": unit, "condition": None}}


def _only(findings):
    assert len(findings) == 1, f"expected 1 finding, got {findings}"
    return findings[0]


def test_door_clear_height_measures_height_not_width():
    """A 2.1 m door must PASS a >= 2.05 m clear-height rule (regression: it was
    measuring the 0.9 m width and FAILing)."""
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("DOOR-H", "main door", "clear height", ">=", 2.05, "m")))
    assert f.verdict == Verdict.PASS, f.message
    assert f.measured == 2.1, f.message


def test_door_clear_width_still_measures_width():
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("DOOR-W", "main door", "clear width", ">=", 0.9, "m")))
    assert f.verdict == Verdict.PASS and f.measured == 0.9, f.message


def test_room_width_measures_min_dimension_not_area():
    """dwelling 'width' must measure the 3.0 m short side, not the 12 m^2 area
    (regression: it PASSed a >= 7.5 m width rule on the area value)."""
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("ROOM-W", "dwelling_space", "width", ">=", 7.5, "m")))
    assert f.verdict == Verdict.FAIL, f.message
    assert f.measured == 3.0, f.message


def test_room_length_measures_max_dimension():
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("ROOM-L", "dwelling_space", "length", ">=", 3.5, "m")))
    assert f.verdict == Verdict.PASS and f.measured == 4.0, f.message


def test_room_area_unchanged():
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("ROOM-A", "dwelling_space", "area", ">=", 8.0, "m2")))
    assert f.verdict == Verdict.PASS and f.measured == 12.0, f.message


def test_room_ceiling_height_default_forces_review():
    """Policy (operator sign-off 2026-07): an UNASSERTED ceiling height must
    not produce a PASS/FAIL — the default is never used for a verdict. The
    finding tells the operator exactly which parameter to supply."""
    chk = NumericChecker(BIM)  # default ceiling_height_mm = 2800, unasserted
    f = _only(chk.check_clause(
        _clause("ROOM-H", "dwelling_space", "ceiling height", ">=", 2.4, "m")))
    assert f.verdict == Verdict.NOT_EVALUATED, f.message
    assert "building_params.wall_height" in f.message
    assert "user building parameter" not in f.message


def test_ceiling_height_parameter_can_fail_when_lowered():
    chk = NumericChecker(BIM, building_params={"ceiling_height_mm": 2200})
    f = _only(chk.check_clause(
        _clause("ROOM-H", "dwelling_space", "ceiling height", ">=", 2.4, "m")))
    assert f.verdict == Verdict.FAIL and f.measured == 2.2, f.message


def test_ceiling_height_parameter_read_from_bim_data():
    bim = {**BIM, "building_params": {"ceiling_height_mm": 3000}}
    chk = NumericChecker(bim)
    f = _only(chk.check_clause(
        _clause("ROOM-H", "dwelling_space", "ceiling height", ">=", 2.9, "m")))
    assert f.verdict == Verdict.PASS and f.measured == 3.0, f.message


def test_window_width_measures_window_geometry():
    """A window-width rule in metres is now owned by the numeric checker and
    measured against the actual window (regression: windows were unmapped)."""
    bim = {**BIM, "windows": [{"id": "WIN1", "width": 1500, "height": 1200,
                               "sill_height": 900}]}
    chk = NumericChecker(bim)
    f = _only(chk.check_clause(
        _clause("WIN-W", "window", "clear width", ">=", 0.5, "m")))
    assert f.verdict == Verdict.PASS and f.measured == 1.5, f.message


def test_window_sill_height_measures_sill():
    bim = {**BIM, "windows": [{"id": "WIN1", "width": 1500, "height": 1200,
                               "sill_height": 900}]}
    chk = NumericChecker(bim)
    f = _only(chk.check_clause(
        _clause("WIN-S", "window", "sill height", "<=", 1.1, "m")))
    assert f.verdict == Verdict.PASS and f.measured == 0.9, f.message


def test_unmeasurable_pairing_is_review():
    """A door 'area' rule has no door-area measurement -> NEEDS_REVIEW, never a
    silent reuse of width/height."""
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("DOOR-A", "main door", "area", ">=", 1.5, "m2")))
    assert f.verdict == Verdict.NEEDS_REVIEW, f.message


def test_unknown_property_is_review():
    chk = NumericChecker(BIM)
    f = _only(chk.check_clause(
        _clause("ROOM-X", "dwelling_space", "fire rating", ">=", 60, "min")))
    assert f.verdict == Verdict.NEEDS_REVIEW, f.message


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
