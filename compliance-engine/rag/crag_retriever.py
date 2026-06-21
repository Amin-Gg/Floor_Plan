"""
services/crag_retriever.py
==========================
Stage 2 / Step 5 — CorrectiveRetriever: the CRAG orchestration layer.

Ties together the confidence evaluator (Step 1), the query transformations
(Steps 2-3), and the rule-based router (Step 4) into one corrective flow:

  A: initial hybrid retrieval, rerank=True (the Stage 1 winning config)
  B: confidence check (sigmoid over the cross-encoder logits)
  C: HIGH  -> return immediately (zero LLM calls)
  D: route (R1 is unreachable here — HIGH already returned at C; it stays
     in the router for standalone use)
  E: primary transform; accept if confidence recovers above LOW
  F: fallback transform if primary stayed LOW
  G: give up -> return the initial result (downstream, the orchestrator's
     human-review queue is the final corrective fallback)

Pure orchestration: no new retrieval logic, no LLM in the verdict path.
Drop-in compatible with MabhasRetriever.retrieve() — same signature, same
default top_k=3, agent-supplied mabhas_part / rule_type filters are
forwarded into every retrieval leg at the SQL level (Stage 1 already
supports this; no post-filtering is needed). Each call additionally writes
`self.last_trace` for evaluation instrumentation.

Signal quality: confidence gaps need a few ranks of context, so retrieval
legs fetch max(top_k, 5) hits for evaluation and the final result is
truncated back to top_k. With the default top_k=3 this costs nothing extra
(the legs fetch candidate_k=50 internally anyway).

Imports of MabhasRetriever are type-checking-only, so this module has no
runtime dependency on rag.rag_retriever (no import cycle with the
factory; the orchestration is unit-testable against a fake base).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rag.retrieval_evaluator import evaluate_retrieval
from rag.query_router import route_query
from rag.query_transforms import detect_language

if TYPE_CHECKING:  # pragma: no cover
    from rag.rag_retriever import MabhasRetriever

_SCORE_TRANSFORM = "sigmoid"  # hits carry CE logits (services/reranker.py)


class CorrectiveRetriever:
    def __init__(self, base: "MabhasRetriever",
                 confidence_thresholds: Optional[dict] = None):
        self.base = base
        self.thresholds = confidence_thresholds
        self.last_trace: dict = {}

    @property
    def last_rerank_seconds(self):
        """Pass-through so latency probes keep working behind the wrapper."""
        return self.base.last_rerank_seconds

    @last_rerank_seconds.setter
    def last_rerank_seconds(self, value):
        self.base.last_rerank_seconds = value

    def retrieve(self, query: str, top_k: int = 3,
                 mabhas_part: Optional[str] = None,
                 rule_type: Optional[str] = None) -> list:
        """Same public signature (and default top_k) as
        MabhasRetriever.retrieve()."""
        eval_k = max(top_k, 5)  # signal-quality floor; result sliced to top_k
        lang = detect_language(query)

        # A + B
        initial = self.base.hybrid_retrieve(
            query, top_k=eval_k, rerank=True,
            mabhas_part=mabhas_part, rule_type=rule_type,
        )
        conf = evaluate_retrieval(
            query, initial, self.thresholds, score_transform=_SCORE_TRANSFORM
        )
        baseline = initial[:top_k]
        trace = {
            "confidence_initial": conf["confidence"],
            "rule": None, "primary": None, "fallback": None,
            "transforms_tried": [], "branch": None,
            "top5_changed": False,
        }

        # C: already confident -> zero LLM calls
        if conf["confidence"] == "HIGH":
            trace["branch"] = "high_no_transform"
            self.last_trace = trace
            return baseline

        # D: route
        decision = route_query(
            query, initial_hits=initial, score_transform=_SCORE_TRANSFORM
        )
        trace.update(rule=decision["rule"], primary=decision["primary"],
                     fallback=decision["fallback"])

        # E: primary transform
        if decision["primary"] != "none":
            transformed = self._dispatch(
                decision["primary"], query, eval_k, lang,
                mabhas_part, rule_type,
            )
            trace["transforms_tried"].append(decision["primary"])
            new_conf = evaluate_retrieval(
                query, transformed, self.thresholds,
                score_transform=_SCORE_TRANSFORM,
            )
            if new_conf["confidence"] != "LOW":
                trace["branch"] = "transform_primary"
                return self._finish(transformed, top_k, baseline, trace)
            # F: fallback transform
            if decision["fallback"] not in ("none", decision["primary"]):
                transformed2 = self._dispatch(
                    decision["fallback"], query, eval_k, lang,
                    mabhas_part, rule_type,
                )
                trace["transforms_tried"].append(decision["fallback"])
                trace["branch"] = "transform_fallback"
                return self._finish(transformed2, top_k, baseline, trace)
            trace["branch"] = "give_up_after_primary"
            self.last_trace = trace
            return baseline

        # G: nothing to try (router said none, e.g. R6 on MEDIUM confidence)
        trace["branch"] = "give_up_no_primary"
        self.last_trace = trace
        return baseline

    # --- helpers -----------------------------------------------------------

    def _dispatch(self, name: str, query: str, top_k: int, language: str,
                  mabhas_part: Optional[str], rule_type: Optional[str]) -> list:
        method = {
            "hyde": self.base.hyde_retrieve,
            "stepback": self.base.stepback_retrieve,
            "multi_query": self.base.multi_query_retrieve,
        }[name]
        return method(
            query, top_k=top_k, rerank=True, language=language,
            mabhas_part=mabhas_part, rule_type=rule_type,
        )

    def _finish(self, hits: list, top_k: int, baseline: list,
                trace: dict) -> list:
        final = hits[:top_k]
        trace["top5_changed"] = (
            [h["article_id"] for h in final[:5]]
            != [h["article_id"] for h in baseline[:5]]
        )
        self.last_trace = trace
        return final
