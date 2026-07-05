"""
tests/test_provenance_gate.py
=============================
IFC Interface Spec §8.3 — the confidence / review pre-pass (§B2).

An element flagged uncertain — `NeedsReview=true` or `Confidence < threshold` —
must be marked so any verdict depending on it resolves to NEEDS_REVIEW, with the
reason surfaced. These tests exercise `apply_review_prepass` directly (unit) and
end-to-end from an exported IFC (integration).

The pre-pass is the honest-degradation fix: it annotates the dict the Step-2
agents read; it does not modify any agent (the agents live in the Step-2 repo).
"""

import pytest

from _engine_modules import apply_review_prepass  # production engine pre-pass


def _bim(elements):
    """Wrap a list of room dicts into a minimal bim_data shell."""
    return {"walls": [], "doors": [], "windows": [],
            "rooms": elements, "stairs": [], "slabs": []}


def test_needs_review_flag_propagates():
    bim = _bim([{"id": "r1", "_provenance": {
        "needs_review": True, "review_reason": "room name from OCR missing",
        "confidence": 1.0, "source": "ocr"}}])
    apply_review_prepass(bim, threshold=0.5)
    r = bim["rooms"][0]
    assert r["review"]["needs_review"] is True
    assert r["needs_review"] is True
    assert "OCR" in r["review"]["reason"]
    assert bim["_review_summary"]["flagged_count"] == 1
    assert bim["_review_summary"]["flagged"][0]["id"] == "r1"


def test_low_confidence_flagged():
    bim = _bim([{"id": "r2", "_provenance": {
        "needs_review": False, "review_reason": "",
        "confidence": 0.30, "source": "maskrcnn"}}])
    apply_review_prepass(bim, threshold=0.5)
    r = bim["rooms"][0]
    assert r["review"]["needs_review"] is True
    assert "0.30" in r["review"]["reason"] or "threshold" in r["review"]["reason"]


def test_high_confidence_not_flagged():
    bim = _bim([{"id": "r3", "_provenance": {
        "needs_review": False, "review_reason": "",
        "confidence": 1.0, "source": "maskrcnn"}}])
    apply_review_prepass(bim, threshold=0.5)
    r = bim["rooms"][0]
    assert r["review"]["needs_review"] is False
    assert r["review"]["reason"] == ""
    assert bim["_review_summary"]["flagged_count"] == 0


def test_threshold_is_configurable():
    def mk():
        return _bim([{"id": "r4", "_provenance": {
            "needs_review": False, "review_reason": "",
            "confidence": 0.6, "source": "maskrcnn"}}])
    lenient = apply_review_prepass(mk(), threshold=0.5)   # 0.6 >= 0.5 → ok
    strict  = apply_review_prepass(mk(), threshold=0.7)   # 0.6 < 0.7 → flag
    assert lenient["rooms"][0]["review"]["needs_review"] is False
    assert strict["rooms"][0]["review"]["needs_review"] is True


def test_missing_provenance_defaults_to_confident():
    """An element with no provenance should not be spuriously flagged."""
    bim = _bim([{"id": "r5"}])
    apply_review_prepass(bim, threshold=0.5)
    assert bim["rooms"][0]["review"]["needs_review"] is False


def test_end_to_end_untyped_room_flagged(tmp_path):
    """Export a plan with an untyped (OCR-failed) room, reload, pre-pass:
    that room must come out NEEDS_REVIEW."""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from export.ifc_exporter import bim_json_to_ifc
    from _engine_modules import ifc_to_bim_data  # production engine loader

    bim = {
        "walls": [
            {"id": "w1", "start_point": [0, 0, 0], "end_point": [4000, 0, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": "w2", "start_point": [4000, 0, 0], "end_point": [4000, 3000, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": "w3", "start_point": [4000, 3000, 0], "end_point": [0, 3000, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
            {"id": "w4", "start_point": [0, 3000, 0], "end_point": [0, 0, 0],
             "thickness": 200, "height": 2800, "is_exterior": True},
        ],
        "doors": [], "windows": [],
        "rooms": [{"id": "r1", "name": "Room", "category": "Unknown",
                   "polygon": [[0, 0], [4000, 0], [4000, 3000], [0, 3000], [0, 0]],
                   "area_m2": 12.0, "centroid_mm": [2000, 1500],
                   "name_source": "none", "needs_review": True}],
        "stairs": [], "slabs": [],
    }
    out = tmp_path / "untyped.ifc"
    bim_json_to_ifc(bim, {}, str(out))
    recon = ifc_to_bim_data(str(out))
    apply_review_prepass(recon, threshold=0.5)

    room = recon["rooms"][0]
    assert room["review"]["needs_review"] is True
    assert recon["_review_summary"]["flagged_count"] >= 1
