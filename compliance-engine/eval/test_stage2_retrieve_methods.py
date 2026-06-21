"""
eval/test_stage2_retrieve_methods.py
====================================
Unit tests for the Stage 2 methods added to MabhasRetriever
(hyde_retrieve / stepback_retrieve / multi_query_retrieve) — no DB, no
model, no API. Follows the test_hybrid_rerank.py pattern: instance methods
and module-level transforms / reranker are monkeypatched.

Run:
    python -m pytest eval/test_stage2_retrieve_methods.py -v
"""

from __future__ import annotations

import pytest

import eval.retrieval_eval  # noqa: F401  (bootstraps services.* namespace)
import rag.rag_retriever as rr
import rag.reranker as reranker_mod
from rag.rag_retriever import MabhasRetriever


def _hit(article_id: str, score: float = 0.5, **extra):
    base = {
        "mabhas_part": "4",
        "article_id": article_id,
        "heading_fa": f"heading-{article_id}",
        "text_fa": f"body-{article_id}",
        "text_en": f"english-{article_id}",
        "rule_type": "numeric",
        "entities": None,
        "score": score,
    }
    base.update(extra)
    return base


@pytest.fixture
def retriever(monkeypatch):
    r = MabhasRetriever("postgresql://unused")
    monkeypatch.setattr(r, "_fetch_passages", lambda ids: {})
    return r


def _fake_hybrid(per_query, calls):
    """hybrid_retrieve stand-in: per_query maps query -> ranked hit list."""

    def fake(query, top_k=3, candidate_k=50, rrf_k=60, rerank=False,
             mabhas_part=None, rule_type=None):
        calls.append(dict(query=query, top_k=top_k, rerank=rerank,
                          mabhas_part=mabhas_part, rule_type=rule_type))
        return [dict(h) for h in per_query[query][:top_k]]

    return fake


def _install_fake_ce(monkeypatch, pref, seen_queries):
    """Cross-encoder stand-in scoring by passage content."""

    def fake(q, passages):
        seen_queries.append(q)
        return [next(v for k, v in pref.items() if k in p) for p in passages]

    monkeypatch.setattr(reranker_mod, "rerank", fake)


# --- hyde_retrieve --------------------------------------------------------------

def test_hyde_retrieves_with_the_hypothetical_and_forwards_args(
        retriever, monkeypatch):
    monkeypatch.setattr(rr, "hyde_transform",
                        lambda q, language="auto": "HYPOTHETICAL CLAUSE")
    calls = []
    monkeypatch.setattr(
        retriever, "hybrid_retrieve",
        _fake_hybrid({"HYPOTHETICAL CLAUSE": [_hit("A"), _hit("B")]}, calls),
    )
    hits = retriever.hyde_retrieve("original question?", top_k=2, rerank=True,
                                   mabhas_part="4", rule_type="numeric")
    assert [h["article_id"] for h in hits] == ["A", "B"]
    assert calls == [dict(query="HYPOTHETICAL CLAUSE", top_k=2, rerank=True,
                          mabhas_part="4", rule_type="numeric")]


def test_hyde_transform_failure_degrades_to_plain_hybrid(retriever, monkeypatch):
    # hyde_transform's contract: returns the ORIGINAL query on API failure.
    monkeypatch.setattr(rr, "hyde_transform", lambda q, language="auto": q)
    calls = []
    monkeypatch.setattr(
        retriever, "hybrid_retrieve",
        _fake_hybrid({"original question?": [_hit("A")]}, calls),
    )
    hits = retriever.hyde_retrieve("original question?", top_k=1)
    assert [h["article_id"] for h in hits] == ["A"]
    assert calls[0]["query"] == "original question?"


# --- stepback_retrieve ------------------------------------------------------------

def test_stepback_fuses_and_reranks_against_original(retriever, monkeypatch):
    monkeypatch.setattr(rr, "stepback_transform",
                        lambda q, language="auto": "BROADER?")
    calls = []
    per_query = {
        "specific?": [_hit("A"), _hit("B"), _hit("C")],
        "BROADER?": [_hit("C"), _hit("D")],
    }
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid(per_query, calls))
    seen_q = []
    pref = {"english-D": 9.0, "english-B": 5.0, "english-A": 2.0,
            "english-C": -3.0}
    _install_fake_ce(monkeypatch, pref, seen_q)

    hits = retriever.stepback_retrieve("specific?", top_k=3, candidate_k=10)

    # Candidate gathering: TWO un-reranked hybrid legs, original first.
    assert [c["query"] for c in calls] == ["specific?", "BROADER?"]
    assert all(c["rerank"] is False for c in calls)
    # Rerank anchored to the ORIGINAL query, not the broader one.
    assert seen_q == ["specific?"]
    # CE ordering wins; the RRF fused score is preserved per hit.
    assert [h["article_id"] for h in hits] == ["D", "B", "A"]
    assert [h["score"] for h in hits] == [9.0, 5.0, 2.0]
    assert all("rrf_score" in h for h in hits)


def test_stepback_rrf_order_without_rerank(retriever, monkeypatch):
    monkeypatch.setattr(rr, "stepback_transform",
                        lambda q, language="auto": "BROADER?")
    per_query = {
        "specific?": [_hit("A"), _hit("B"), _hit("C")],
        "BROADER?": [_hit("C"), _hit("D")],
    }
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid(per_query, []))
    hits = retriever.stepback_retrieve("specific?", top_k=4, rerank=False)
    # RRF: C in both lists (1/61 + 1/63) > A (1/61) > B,D tie (1/62) -> id order.
    assert [h["article_id"] for h in hits] == ["C", "A", "B", "D"]
    assert hits[0]["score"] == max(h["score"] for h in hits)


def test_stepback_skips_duplicate_leg_when_transform_falls_back(
        retriever, monkeypatch):
    monkeypatch.setattr(rr, "stepback_transform",
                        lambda q, language="auto": q)  # API-failure contract
    calls = []
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid({"specific?": [_hit("A")]}, calls))
    hits = retriever.stepback_retrieve("specific?", top_k=1, rerank=False)
    assert len(calls) == 1  # no duplicate retrieval for an identical query
    assert [h["article_id"] for h in hits] == ["A"]


def test_stepback_forwards_filters_into_both_legs(retriever, monkeypatch):
    monkeypatch.setattr(rr, "stepback_transform",
                        lambda q, language="auto": "BROADER?")
    calls = []
    per_query = {"specific?": [_hit("A")], "BROADER?": [_hit("B")]}
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid(per_query, calls))
    retriever.stepback_retrieve("specific?", top_k=1, rerank=False,
                                mabhas_part="4", rule_type="spatial")
    assert all(c["mabhas_part"] == "4" and c["rule_type"] == "spatial"
               for c in calls)


# --- multi_query_retrieve ------------------------------------------------------------

def test_multi_query_fuses_n_lists_and_reranks_against_original(
        retriever, monkeypatch):
    monkeypatch.setattr(
        rr, "multi_query_transform",
        lambda q, n=3, language="auto": [q, "variant one?", "variant two?"],
    )
    calls = []
    per_query = {
        "orig?": [_hit("A"), _hit("B")],
        "variant one?": [_hit("B"), _hit("C")],
        "variant two?": [_hit("D")],
    }
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid(per_query, calls))
    seen_q = []
    pref = {"english-A": 1.0, "english-B": 4.0, "english-C": 2.0,
            "english-D": 3.0}
    _install_fake_ce(monkeypatch, pref, seen_q)

    hits = retriever.multi_query_retrieve("orig?", top_k=4, n=3)

    assert [c["query"] for c in calls] == ["orig?", "variant one?", "variant two?"]
    assert all(c["rerank"] is False for c in calls)
    assert seen_q == ["orig?"]  # rerank against the ORIGINAL
    assert [h["article_id"] for h in hits] == ["B", "D", "C", "A"]
    # B appeared in two lists -> largest fused (rrf_score) value.
    rrf = {h["article_id"]: h["rrf_score"] for h in hits}
    assert rrf["B"] == max(rrf.values())


def test_multi_query_single_query_shortcut(retriever, monkeypatch):
    # Transform's failure contract: [original] only -> one leg, no fusion.
    monkeypatch.setattr(rr, "multi_query_transform",
                        lambda q, n=3, language="auto": [q])
    calls = []
    monkeypatch.setattr(retriever, "hybrid_retrieve",
                        _fake_hybrid({"orig?": [_hit("A"), _hit("B")]}, calls))
    hits = retriever.multi_query_retrieve("orig?", top_k=2, rerank=False)
    assert len(calls) == 1
    assert [h["article_id"] for h in hits] == ["A", "B"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
