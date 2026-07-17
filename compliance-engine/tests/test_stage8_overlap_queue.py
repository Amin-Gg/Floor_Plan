"""
Stage 8 — QC-PLACE-006 convention-agnostic overlap + review-queue contract.

Invariants locked here:
  * QC-PLACE-006 fires ONLY when two openings on one wall overlap under
    every insertion convention (corner-left, centre, corner-right share one
    α; the pair gap is linear in α, so both endpoints overlapping is the
    guarantee). Convention-dependent overlaps are deliberately NOT flagged —
    zero false positives by construction.
  * The math: gap(α) = (t2−t1) − w1 + α·(w2−w1) at endpoints α ∈ {0, −1}.
  * Review-queue items carry finding_id / category / code / unsupported /
    review_kind so the municipal UI can sort judgment vs outside-scope items
    and correlate queue entries with BCF topics (uuid5 of finding_id).
"""
from __future__ import annotations

from tests.helpers import run_quality_checks
from services.review_queue import ReviewQueue


def _wall(wid="W1", length=6000.0):
    return {"id": wid, "start_point": [0.0, 0.0, 0.0],
            "end_point": [length, 0.0, 0.0], "thickness": 200.0}


def _bim(doors=(), windows=()):
    return {"rooms": [], "walls": [_wall()], "doors": list(doors),
            "windows": list(windows),
            "_review_summary": {"threshold": 0.5, "flagged": [],
                                "scale_flagged": False,
                                "scale_confidence": None}}


def _door(did, x, width):
    return {"id": did, "host_wall_id": "W1", "width": width,
            "insertion_point": [x, 0.0, 0.0]}


def _codes(stage):
    return [f["code"] for f in stage["findings"]]


# ── QC-PLACE-006 ─────────────────────────────────────────────────────────────

def test_guaranteed_overlap_fires():
    """Same insertion x, both 900 wide: under α=0 both intervals [1000,1900];
    under α=−1 both [100,1000] — overlap 900 mm under every convention."""
    stage = run_quality_checks(_bim(doors=[_door("D1", 1000.0, 900.0),
                                           _door("D2", 1000.0, 900.0)]))
    assert "QC-PLACE-006" in _codes(stage)
    f = [x for x in stage["findings"] if x["code"] == "QC-PLACE-006"][0]
    assert f["element_id"] == "W1"
    assert "D1" in f["actual"] and "D2" in f["actual"]


def test_convention_dependent_overlap_is_not_flagged():
    """t2−t1 = 900, w1 = w2 = 900: gap(α) = 0 for all α — the intervals
    exactly abut under every convention, and overlap only if widths grew.
    With equal widths and separation == width, never a guaranteed overlap."""
    stage = run_quality_checks(_bim(doors=[_door("D1", 1000.0, 900.0),
                                           _door("D2", 1900.0, 900.0)]))
    assert "QC-PLACE-006" not in _codes(stage)


def test_unequal_widths_use_worst_convention():
    """t2−t1 = 500, w1 = 900, w2 = 300: gap(0) = −400 (overlap 400),
    gap(−1) = −400 + 600 = 200 (NO overlap under corner-right) — must not
    fire despite a 400 mm overlap under the corner-left convention."""
    stage = run_quality_checks(_bim(doors=[_door("D1", 1000.0, 900.0),
                                           _door("D2", 1500.0, 300.0)]))
    assert "QC-PLACE-006" not in _codes(stage)


def test_clearly_separated_openings_are_clean():
    stage = run_quality_checks(_bim(doors=[_door("D1", 500.0, 900.0),
                                           _door("D2", 4000.0, 900.0)]))
    assert "QC-PLACE-006" not in _codes(stage)


def test_overlap_tolerance_overridable():
    bim = _bim(doors=[_door("D1", 1000.0, 900.0),
                      _door("D2", 1030.0, 900.0)])   # guaranteed ~870 mm
    bim["_qc_tolerances"] = {"place_overlap_tol_mm": 1000.0}
    assert "QC-PLACE-006" not in _codes(run_quality_checks(bim))


def test_mixed_door_window_pairs_are_checked():
    bim = _bim(doors=[_door("D1", 1000.0, 900.0)],
               windows=[{"id": "N1", "host_wall_id": "W1", "width": 900.0,
                         "insertion_point": [1000.0, 0.0, 0.0]}])
    assert "QC-PLACE-006" in _codes(run_quality_checks(bim))


# ── review-queue contract ────────────────────────────────────────────────────

def test_queue_items_carry_contract_fields(tmp_path):
    q = ReviewQueue(str(tmp_path / "queue.json"))
    result = {"findings": [
        {"finding_id": "aa" * 6, "category": "compliance", "code": None,
         "article_id": "4-5-1", "verdict": "NEEDS_REVIEW",
         "message": "judge me", "unsupported": False},
        {"finding_id": "bb" * 6, "category": "compliance", "code": None,
         "article_id": "4-5-2", "verdict": "NEEDS_REVIEW",
         "message": "Unsupported comparator", "unsupported": True},
    ]}
    items = {i["article_id"]: i for i in q.enqueue_result(result, plan_id="p")}
    a, b = items["4-5-1"], items["4-5-2"]
    assert a["finding_id"] == "aa" * 6 and a["review_kind"] == "judgment"
    assert b["unsupported"] is True and b["review_kind"] == "outside_scope"
    assert a["category"] == "compliance"


def test_queue_items_without_contract_fields_default_sanely(tmp_path):
    """Legacy result dicts (pre-Stage-2 serialization) still enqueue."""
    q = ReviewQueue(str(tmp_path / "queue.json"))
    result = {"findings": [{"article_id": "X", "verdict": "NEEDS_REVIEW",
                            "message": "old finding"}]}
    item = q.enqueue_result(result, plan_id="p")[0]
    assert item["finding_id"] is None
    assert item["review_kind"] == "judgment"
    assert item["unsupported"] is False
