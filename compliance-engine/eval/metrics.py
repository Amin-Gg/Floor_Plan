"""
eval/metrics.py
===============
Pure retrieval-metric functions for the Stage 1 evaluation harness.

Design constraints (deliberate):
  - No database access, no model loading, no I/O — pure functions only.
    This makes them trivially unit-testable and reusable in every later
    ablation step (Steps 2-7) without dragging in infrastructure.
  - Binary relevance everywhere: a retrieved clause is either one of the
    gold articles for the question or it is not. The Mabhas eval set has
    no graded relevance labels, so graded variants would be fiction.

Conventions:
  - `retrieved_ids` is an ORDERED list of article_id strings, best first
    (i.e. exactly what MabhasRetriever.retrieve() returns, mapped to ids).
  - `gold_ids` is a set of article_id strings (1 for zero-hop items,
    2 for multi-hop items in the current eval set).
  - All functions return a float in [0, 1].
  - Degenerate inputs (k <= 0, empty retrieved list, empty gold set)
    return 0.0 rather than raising — an evaluation run must never crash
    on one bad item.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Set


def hit_at_k(retrieved_ids: Sequence[str], gold_ids: Set[str], k: int) -> float:
    """1.0 if ANY gold id appears in the top-k retrieved ids, else 0.0."""
    if k <= 0 or not retrieved_ids or not gold_ids:
        return 0.0
    return 1.0 if any(rid in gold_ids for rid in retrieved_ids[:k]) else 0.0


def recall_at_k(retrieved_ids: Sequence[str], gold_ids: Set[str], k: int) -> float:
    """|gold ∩ top-k| / |gold|.

    For zero-hop items (|gold| = 1) this equals hit@k. For multi-hop items
    (|gold| = 2) it rewards retrieving BOTH supporting clauses, which is
    exactly the capability later ablation steps target.
    """
    if k <= 0 or not retrieved_ids or not gold_ids:
        return 0.0
    found = sum(1 for rid in set(retrieved_ids[:k]) if rid in gold_ids)
    return found / len(gold_ids)


def mrr(retrieved_ids: Sequence[str], gold_ids: Set[str]) -> float:
    """Reciprocal rank of the FIRST gold id (1-based); 0.0 if none retrieved."""
    if not retrieved_ids or not gold_ids:
        return 0.0
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in gold_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: Sequence[str], gold_ids: Set[str], k: int) -> float:
    """Standard nDCG@k with binary relevance.

    DCG@k  = Σ_{i=1..k} rel_i / log2(i + 1), rel_i ∈ {0, 1}
    IDCG@k = DCG of the ideal ranking: min(|gold|, k) relevant items
             placed at the top.
    A duplicated article_id in the ranking is only credited once
    (first occurrence) so a retriever cannot inflate nDCG by repetition.
    """
    if k <= 0 or not retrieved_ids or not gold_ids:
        return 0.0

    dcg = 0.0
    seen: set = set()
    for i, rid in enumerate(retrieved_ids[:k], start=1):
        if rid in gold_ids and rid not in seen:
            dcg += 1.0 / math.log2(i + 1)
            seen.add(rid)

    ideal_hits = min(len(gold_ids), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def all_metrics(
    retrieved_ids: Sequence[str], gold_ids: Set[str], k_values: List[int]
) -> dict:
    """Convenience: compute every metric for every k in one dict.

    Keys: 'hit@K', 'recall@K', 'ndcg@K' for each K, plus 'mrr'.
    """
    out: dict = {}
    for k in k_values:
        out[f"hit@{k}"] = hit_at_k(retrieved_ids, gold_ids, k)
        out[f"recall@{k}"] = recall_at_k(retrieved_ids, gold_ids, k)
        out[f"ndcg@{k}"] = ndcg_at_k(retrieved_ids, gold_ids, k)
    out["mrr"] = mrr(retrieved_ids, gold_ids)
    return out
