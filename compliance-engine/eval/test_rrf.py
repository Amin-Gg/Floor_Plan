"""
eval/test_rrf.py
================
Unit tests for the pure Reciprocal Rank Fusion function rrf_fuse().

No DB, no model — rrf_fuse is pure math. Importing eval.retrieval_eval
first registers the RAG/ modules under the services.* namespace, so the
import below works in both repository layouts.

Run:
    python -m pytest eval/test_rrf.py -v
"""

from __future__ import annotations

import pytest

import eval.retrieval_eval  # noqa: F401  (bootstraps services.* namespace)
from rag.rag_retriever import rrf_fuse


# ---------------------------------------------------------------------------
# Required cases from the Step 3 spec
# ---------------------------------------------------------------------------

def test_id_first_in_both_lists_beats_id_first_in_one():
    # A is rank 1 in both lists; B is rank 1 in only the (hypothetical)
    # ordering of one list. Concretely:
    #   list1 = [A, B], list2 = [A, C]
    #   A = 1/61 + 1/61, B = 1/62, C = 1/62
    fused = rrf_fuse([["A", "B"], ["A", "C"]], rrf_k=60)
    assert fused["A"] > fused["B"]
    assert fused["A"] > fused["C"]
    # and a single rank-1 appearance still beats a single rank-2 appearance
    fused2 = rrf_fuse([["B"], ["A", "C"]], rrf_k=60)
    assert fused2["B"] == fused2["A"]          # both rank-1 once
    assert fused2["B"] > fused2["C"]           # rank-1 beats rank-2


def test_exact_arithmetic_rank1_to_8_decimal_places():
    fused = rrf_fuse([["A"]], rrf_k=60)
    # 1 / (60 + 1) = 0.016393442622950820...
    assert round(fused["A"], 8) == round(1.0 / 61.0, 8)
    assert fused["A"] == pytest.approx(0.01639344, abs=1e-8)


def test_empty_inputs_handled_gracefully():
    assert rrf_fuse([]) == {}
    assert rrf_fuse([[], []]) == {}
    fused = rrf_fuse([[], ["A"]], rrf_k=60)
    assert fused == {"A": pytest.approx(1.0 / 61.0)}


# ---------------------------------------------------------------------------
# Additional hand-checked cases
# ---------------------------------------------------------------------------

def test_missing_from_one_list_contributes_zero():
    # A: rank 1 in list1 only -> 1/61
    # B: rank 2 in list1 AND rank 1 in list2 -> 1/62 + 1/61 > 1/61
    fused = rrf_fuse([["A", "B"], ["B"]], rrf_k=60)
    assert fused["A"] == pytest.approx(1.0 / 61.0)
    assert fused["B"] == pytest.approx(1.0 / 62.0 + 1.0 / 61.0)
    assert fused["B"] > fused["A"]


def test_full_hand_computed_fusion():
    # list1 = [x, y, z]; list2 = [y, w]
    # x = 1/61            = 0.016393442...
    # y = 1/62 + 1/61     = 0.032522561...
    # z = 1/63            = 0.015873015...
    # w = 1/62            = 0.016129032...
    fused = rrf_fuse([["x", "y", "z"], ["y", "w"]], rrf_k=60)
    assert fused["x"] == pytest.approx(1 / 61)
    assert fused["y"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["z"] == pytest.approx(1 / 63)
    assert fused["w"] == pytest.approx(1 / 62)
    order = sorted(fused, key=fused.get, reverse=True)
    assert order == ["y", "x", "w", "z"]


def test_duplicate_id_within_one_list_counts_once():
    # A appears at ranks 1 and 3 of the same list -> only rank 1 counts.
    fused = rrf_fuse([["A", "B", "A"]], rrf_k=60)
    assert fused["A"] == pytest.approx(1.0 / 61.0)
    assert fused["B"] == pytest.approx(1.0 / 62.0)


def test_custom_rrf_k():
    fused = rrf_fuse([["A"]], rrf_k=0)
    assert fused["A"] == pytest.approx(1.0)  # 1/(0+1)


def test_negative_rrf_k_rejected():
    with pytest.raises(ValueError):
        rrf_fuse([["A"]], rrf_k=-1)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
