"""
Unit tests for RAG/retrieval_evaluator.evaluate_retrieval.

Pure tests — the evaluator makes no model calls, so there is nothing to mock.
Run from the project root:  python -m pytest eval/test_retrieval_evaluator.py -v
"""

import math

import pytest

from rag.retrieval_evaluator import evaluate_retrieval, DEFAULT_THRESHOLDS


def make_hits(scores):
    """Build minimally-realistic hit-dicts for the given list of scores."""
    hits = []
    for i, s in enumerate(scores):
        hits.append(
            {
                "mabhas_part": "3",
                "article_id": f"3-1-1-{i}",
                "heading_fa": "عنوان آزمایشی",
                "text_fa": "متن آزمایشی",
                "text_en": "test text",
                "rule_type": "numeric",
                "entities": {},
                "score": s,
            }
        )
    return hits


# --- The four required cases ---------------------------------------------

def test_high_confidence_clear_separation():
    # Strong top-1 AND a clear gap over rank 3 -> HIGH.
    res = evaluate_retrieval("q", make_hits([0.82, 0.61, 0.55, 0.50, 0.44]))
    assert res["confidence"] == "HIGH"
    assert res["signals"]["top1_score"] == pytest.approx(0.82)
    assert res["signals"]["top1_to_top3_gap"] == pytest.approx(0.27)
    assert "clear margin" in res["explanation"]


def test_medium_top1_strong_but_close():
    # Strong top-1 but ranks 2/3 sit just behind it -> MEDIUM.
    res = evaluate_retrieval("q", make_hits([0.72, 0.69, 0.67, 0.40, 0.35]))
    assert res["confidence"] == "MEDIUM"
    assert res["signals"]["top1_to_top3_gap"] == pytest.approx(0.05)


def test_low_weak_top1():
    # Top-1 below low_top1 -> LOW regardless of gap.
    res = evaluate_retrieval("q", make_hits([0.25, 0.20, 0.15, 0.10, 0.05]))
    assert res["confidence"] == "LOW"
    assert "Weak top hit" in res["explanation"]


def test_low_tied_high_scores():
    # High scores but tightly clustered (gap < low_gap) -> LOW.
    # This is the "clustered-high" case from the spec, reconciled to LOW.
    res = evaluate_retrieval("q", make_hits([0.70, 0.69, 0.685, 0.68, 0.67]))
    assert res["confidence"] == "LOW"
    assert res["signals"]["top1_to_top3_gap"] == pytest.approx(0.015)
    assert "nearly tied" in res["explanation"]


def test_empty_hits_low_no_hits():
    res = evaluate_retrieval("q", [])
    assert res["confidence"] == "LOW"
    assert res["explanation"] == "no hits"
    assert res["signals"]["hit_count"] == 0
    assert res["signals"]["top1_score"] == 0.0


# --- Signal-computation and edge cases -----------------------------------

def test_signals_computed_correctly():
    res = evaluate_retrieval("q", make_hits([0.9, 0.8, 0.7, 0.6, 0.5]))
    s = res["signals"]
    assert s["top1_score"] == pytest.approx(0.9)
    assert s["top1_to_top3_gap"] == pytest.approx(0.2)   # 0.9 - 0.7
    assert s["top1_to_top5_gap"] == pytest.approx(0.4)   # 0.9 - 0.5
    assert s["score_dispersion"] == pytest.approx(math.sqrt(0.02))  # ~0.1414
    assert s["hit_count"] == 5


def test_fewer_than_three_hits_uses_last():
    # n=2: both gaps fall back to the last available hit (index 1).
    res = evaluate_retrieval("q", make_hits([0.8, 0.5]))
    assert res["signals"]["top1_to_top3_gap"] == pytest.approx(0.3)
    assert res["signals"]["top1_to_top5_gap"] == pytest.approx(0.3)
    assert res["signals"]["hit_count"] == 2
    assert res["confidence"] == "HIGH"  # top1=0.8, gap=0.3


def test_single_hit_cannot_be_high():
    # One hit -> no separation to measure -> gap is 0 -> LOW by design.
    res = evaluate_retrieval("q", make_hits([0.95]))
    assert res["confidence"] == "LOW"
    assert res["signals"]["top1_to_top3_gap"] == 0.0
    assert res["signals"]["score_dispersion"] == 0.0


def test_custom_thresholds_override():
    hits = make_hits([0.55, 0.40, 0.38])
    # With defaults this is MEDIUM (top1 < 0.6).
    assert evaluate_retrieval("q", hits)["confidence"] == "MEDIUM"
    # Lowering the HIGH bar flips it to HIGH.
    res = evaluate_retrieval("q", hits, thresholds={"high_top1": 0.5, "high_gap": 0.1})
    assert res["confidence"] == "HIGH"


def test_default_thresholds_unmutated():
    # Passing overrides must not mutate the module-level defaults.
    evaluate_retrieval("q", make_hits([0.9, 0.1]), thresholds={"high_top1": 0.1})
    assert DEFAULT_THRESHOLDS["high_top1"] == 0.6


def test_missing_score_field_defaults_to_zero():
    hits = [{"article_id": "x"}, {"article_id": "y", "score": 0.5}]
    res = evaluate_retrieval("q", hits)
    assert res["signals"]["top1_score"] == 0.0
    assert res["confidence"] == "LOW"
