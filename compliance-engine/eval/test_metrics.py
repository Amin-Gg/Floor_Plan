"""
eval/test_metrics.py
====================
Hand-checked unit tests for eval/metrics.py.

Every expected value below was computed by hand first, then encoded as a
test — never the other way around. No DB, no model, no I/O.

Run:
    python -m pytest eval/test_metrics.py -v
"""

from __future__ import annotations

import math

import pytest

from eval.metrics import all_metrics, hit_at_k, mrr, ndcg_at_k, recall_at_k


# ---------------------------------------------------------------------------
# Required cases from the Step 1 spec
# ---------------------------------------------------------------------------

def test_mrr_gold_at_rank_2_is_half():
    # gold "4-5-2-1" first appears at rank 2 (1-based) -> 1/2
    retrieved = ["9-9-9-9", "4-5-2-1", "3-3-3-3"]
    assert mrr(retrieved, {"4-5-2-1"}) == 0.5


def test_recall_at_5_one_of_two_gold_found():
    # 2 gold ids, only one inside top-5 -> 1/2
    retrieved = ["a", "b", "gold1", "c", "d", "gold2"]  # gold2 at rank 6
    assert recall_at_k(retrieved, {"gold1", "gold2"}, k=5) == 0.5


def test_hit_gold_at_rank_1():
    retrieved = ["gold", "x", "y"]
    assert hit_at_k(retrieved, {"gold"}, k=1) == 1.0
    # k = 0 -> empty window -> miss by definition
    assert hit_at_k(retrieved, {"gold"}, k=0) == 0.0


def test_empty_retrieved_list_all_metrics_zero():
    gold = {"4-5-2-1"}
    assert hit_at_k([], gold, 5) == 0.0
    assert recall_at_k([], gold, 5) == 0.0
    assert mrr([], gold) == 0.0
    assert ndcg_at_k([], gold, 5) == 0.0


# ---------------------------------------------------------------------------
# Additional hand-checked cases
# ---------------------------------------------------------------------------

def test_hit_at_k_gold_just_outside_window():
    retrieved = ["x", "y", "z", "gold"]
    assert hit_at_k(retrieved, {"gold"}, k=3) == 0.0
    assert hit_at_k(retrieved, {"gold"}, k=4) == 1.0


def test_recall_at_k_both_gold_found():
    retrieved = ["gold1", "x", "gold2"]
    assert recall_at_k(retrieved, {"gold1", "gold2"}, k=3) == 1.0


def test_recall_duplicates_not_double_counted():
    # same gold id retrieved twice must count once: 1/2, not 2/2
    retrieved = ["gold1", "gold1", "x"]
    assert recall_at_k(retrieved, {"gold1", "gold2"}, k=3) == 0.5


def test_mrr_no_gold_retrieved():
    assert mrr(["a", "b", "c"], {"gold"}) == 0.0


def test_mrr_uses_first_gold_when_multiple():
    # gold2 at rank 1 -> MRR = 1.0 even though gold1 is later
    retrieved = ["gold2", "x", "gold1"]
    assert mrr(retrieved, {"gold1", "gold2"}) == 1.0


def test_ndcg_perfect_ranking_is_one():
    # both gold at the top, |gold| = 2, k = 5 -> DCG = IDCG
    retrieved = ["gold1", "gold2", "x", "y", "z"]
    assert ndcg_at_k(retrieved, {"gold1", "gold2"}, k=5) == pytest.approx(1.0)


def test_ndcg_hand_computed_single_gold_rank_3():
    # rel at rank 3 only: DCG = 1/log2(4) = 0.5
    # IDCG (1 gold, k=5)   = 1/log2(2) = 1.0
    # nDCG = 0.5
    retrieved = ["x", "y", "gold", "z", "w"]
    assert ndcg_at_k(retrieved, {"gold"}, k=5) == pytest.approx(0.5)


def test_ndcg_hand_computed_two_gold_ranks_1_and_3():
    # DCG  = 1/log2(2) + 1/log2(4)            = 1.0 + 0.5      = 1.5
    # IDCG = 1/log2(2) + 1/log2(3)            = 1.0 + 0.63093  = 1.63093
    # nDCG = 1.5 / 1.63093 = 0.919721...
    retrieved = ["gold1", "x", "gold2", "y", "z"]
    expected = 1.5 / (1.0 + 1.0 / math.log2(3))
    assert ndcg_at_k(retrieved, {"gold1", "gold2"}, k=5) == pytest.approx(expected)


def test_ndcg_duplicate_gold_not_credited_twice():
    # "gold" at ranks 1 and 2; only rank 1 counts.
    # DCG = 1.0, IDCG (1 gold) = 1.0 -> nDCG = 1.0 (not > 1)
    retrieved = ["gold", "gold", "x"]
    assert ndcg_at_k(retrieved, {"gold"}, k=3) == pytest.approx(1.0)


def test_empty_gold_set_is_zero_not_crash():
    retrieved = ["a", "b"]
    assert hit_at_k(retrieved, set(), 3) == 0.0
    assert recall_at_k(retrieved, set(), 3) == 0.0
    assert mrr(retrieved, set()) == 0.0
    assert ndcg_at_k(retrieved, set(), 3) == 0.0


def test_all_metrics_keys_and_consistency():
    retrieved = ["x", "gold", "y"]
    gold = {"gold"}
    m = all_metrics(retrieved, gold, k_values=[1, 3])
    assert set(m.keys()) == {
        "hit@1", "recall@1", "ndcg@1", "hit@3", "recall@3", "ndcg@3", "mrr"
    }
    assert m["hit@1"] == 0.0
    assert m["hit@3"] == 1.0
    assert m["recall@3"] == 1.0
    assert m["mrr"] == 0.5
    # single gold at rank 2, k=3: nDCG = (1/log2(3)) / 1.0
    assert m["ndcg@3"] == pytest.approx(1.0 / math.log2(3))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
