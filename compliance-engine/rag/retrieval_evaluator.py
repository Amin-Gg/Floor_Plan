"""
services/retrieval_evaluator.py
===============================
Stage 2 / Step 1 — retrieval confidence evaluator.

Classifies a retrieval result as HIGH / MEDIUM / LOW using ONLY signals the
retriever already emits (the per-hit "score"). No model is loaded, no I/O.
This is the gatekeeper for every later Stage 2 component: HIGH retrievals
pass through untouched (zero LLM calls); MEDIUM/LOW become candidates for
corrective query transformations before escalating to the human-review queue.

Score spaces (Stage 1 reality, per services/reranker.py)
--------------------------------------------------------
hybrid_retrieve(rerank=True) hits carry CROSS-ENCODER LOGITS in "score" —
raw, unbounded values comparable only within one query. The spec thresholds
(HIGH: top1 >= 0.6 & gap >= 0.15; LOW: top1 < 0.3 | gap < 0.03) assume a
[0, 1] score. Bridging the two is the caller's explicit choice:

    score_transform="sigmoid"  -> scores mapped through 1/(1+e^-x) first.
                                  USE THIS for reranked hits (the CRAG layer
                                  and the eval harness do). Sigmoid is
                                  monotonic, so ranking is unchanged; only
                                  the threshold space is normalized.
    score_transform="none"     -> scores used as-is (default). Correct for
                                  scores already in [0, 1], e.g. dense
                                  cosine similarities.

RRF-fused scores (rerank=False) live on a ~0.01-0.03 scale; neither
transform makes the default thresholds meaningful there — evaluate reranked
hits, or pass custom thresholds.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

# Tunable thresholds. Override any subset via the `thresholds` argument.
# Calibrated for scores in [0, 1] (use score_transform="sigmoid" for CE logits).
DEFAULT_THRESHOLDS: dict = {
    "high_top1": 0.6,   # top1_score at/above this is eligible for HIGH
    "high_gap": 0.15,   # top1_to_top3_gap at/above this is required for HIGH
    "low_top1": 0.3,    # top1_score below this forces LOW
    "low_gap": 0.03,    # top1_to_top3_gap below this forces LOW
}

_TRANSFORMS = ("none", "sigmoid")


def _sigmoid(x: float) -> float:
    # Clamp to avoid overflow on extreme logits.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-min(x, 60.0)))
    return math.exp(max(x, -60.0)) / (1.0 + math.exp(max(x, -60.0)))


def _safe_score(hit: dict) -> float:
    """Coerce a hit's score to float, defaulting to 0.0 on missing/bad data."""
    try:
        return float(hit.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def evaluate_retrieval(
    query: str,
    hits: list,
    thresholds: Optional[dict] = None,
    score_transform: str = "none",
) -> dict:
    """Classify a retrieval result as HIGH / MEDIUM / LOW confidence.

    Args:
        query: The query string (kept for symmetry/logging; the deterministic
            rules do not use it).
        hits: Ranked hit-dict list, best first, each with a "score" field.
        thresholds: Optional overrides for DEFAULT_THRESHOLDS.
        score_transform: "none" (default) or "sigmoid" — see module docstring.

    Returns:
        {"confidence": "HIGH"|"MEDIUM"|"LOW",
         "signals": {top1_score, top1_to_top3_gap, top1_to_top5_gap,
                     score_dispersion, hit_count},
         "explanation": "<one short sentence>"}
        Signal values are reported in the TRANSFORMED space (the space the
        thresholds were applied in), so signals and thresholds always agree.
    """
    if score_transform not in _TRANSFORMS:
        raise ValueError(f"score_transform must be one of {_TRANSFORMS}")
    t = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        t.update(thresholds)

    # --- Empty result: nothing to assess -> LOW ---------------------------
    if not hits:
        return {
            "confidence": "LOW",
            "signals": {
                "top1_score": 0.0,
                "top1_to_top3_gap": 0.0,
                "top1_to_top5_gap": 0.0,
                "score_dispersion": 0.0,
                "hit_count": 0,
            },
            "explanation": "no hits",
        }

    scores = [_safe_score(h) for h in hits]
    if score_transform == "sigmoid":
        scores = [_sigmoid(s) for s in scores]
    n = len(scores)
    top5 = scores[:5]

    # Index fallbacks so short result lists don't raise: a 2-hit list uses the
    # last hit for both the top-3 and top-5 gap.
    idx3 = min(2, n - 1)
    idx5 = min(4, n - 1)

    top1 = scores[0]
    gap3 = top1 - scores[idx3]
    gap5 = top1 - scores[idx5]
    dispersion = statistics.pstdev(top5) if len(top5) > 1 else 0.0

    signals = {
        "top1_score": top1,
        "top1_to_top3_gap": gap3,
        "top1_to_top5_gap": gap5,
        "score_dispersion": dispersion,
        "hit_count": n,
    }

    # --- Deterministic classification -------------------------------------
    # Order matters: HIGH is checked first. When HIGH fires, gap3 >= high_gap
    # (0.15) guarantees gap3 >= low_gap (0.03), so the LOW branch cannot also
    # fire — the bands do not overlap.
    #
    # >>> SINGLE CHANGE POINT for the "clustered-high -> HIGH" semantics <<<
    # Spec reconciliation (Stage 2 / Step 1): high-but-tightly-clustered
    # scores classify as LOW — a tie among candidates is ambiguity, not
    # confidence. To flip that, relax this condition (drop the gap test or
    # OR-in a low-dispersion test).
    if top1 >= t["high_top1"] and gap3 >= t["high_gap"]:
        confidence = "HIGH"
        explanation = (
            f"Strong top hit (top1={top1:.2f}) with a clear margin over "
            f"rank {idx3 + 1} (gap={gap3:.2f})."
        )
    elif top1 < t["low_top1"] or gap3 < t["low_gap"]:
        confidence = "LOW"
        if top1 < t["low_top1"]:
            explanation = (
                f"Weak top hit (top1={top1:.2f} < {t['low_top1']}); "
                f"retrieval is unreliable."
            )
        else:
            explanation = (
                f"Top hits are nearly tied (gap={gap3:.2f} < {t['low_gap']}); "
                f"the retriever cannot separate candidates."
            )
    else:
        confidence = "MEDIUM"
        explanation = (
            f"Borderline: top1={top1:.2f}, gap={gap3:.2f} fall between the "
            f"HIGH and LOW bands."
        )

    return {
        "confidence": confidence,
        "signals": signals,
        "explanation": explanation,
    }
