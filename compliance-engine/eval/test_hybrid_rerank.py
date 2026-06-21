"""
eval/test_hybrid_rerank.py
==========================
Unit tests for the Step 4 rerank branch of hybrid_retrieve — no DB, no
model. dense_retrieve / lexical_retrieve / _fetch_passages and the
cross-encoder are all mocked.

Run:
    python -m pytest eval/test_hybrid_rerank.py -v
"""

from __future__ import annotations

import sys

import pytest

import eval.retrieval_eval  # noqa: F401  (bootstraps services.* namespace)
from rag.rag_retriever import MabhasRetriever, _build_rerank_passage


def _hit(article_id: str, **extra):
    base = {
        "mabhas_part": "4",
        "article_id": article_id,
        "heading_fa": f"heading-{article_id}",
        "text_fa": f"body-{article_id}",
        "text_en": f"english-{article_id}",
        "rule_type": "numeric",
        "entities": None,
        "score": 0.5,
    }
    base.update(extra)
    return base


@pytest.fixture
def retriever(monkeypatch):
    r = MabhasRetriever("postgresql://unused")
    # dense ranks A,B,C ; lexical ranks C,D  ->  RRF order: C, A, B, D
    monkeypatch.setattr(
        r, "dense_retrieve",
        lambda q, top_k=3, **kw: [_hit("A"), _hit("B"), _hit("C")][:top_k],
    )
    monkeypatch.setattr(
        r, "lexical_retrieve",
        lambda q, top_k=3, **kw: [_hit("C"), _hit("D")][:top_k],
    )
    monkeypatch.setattr(r, "_fetch_passages", lambda ids: {})
    return r


def _install_fake_reranker(monkeypatch, score_fn):
    import rag.reranker as rr
    monkeypatch.setattr(rr, "rerank", lambda q, ps: [score_fn(p) for p in ps])


def test_no_rerank_returns_rrf_order_and_scores(retriever):
    hits = retriever.hybrid_retrieve("q", top_k=4, candidate_k=10)
    assert [h["article_id"] for h in hits] == ["C", "A", "B", "D"]
    assert all("rrf_score" not in h for h in hits)  # only set when reranking


def test_rerank_reorders_by_cross_encoder_score(retriever, monkeypatch):
    # cross-encoder strongly prefers D, then B, then A, then C
    pref = {"english-D": 9.0, "english-B": 5.0, "english-A": 2.0, "english-C": -3.0}
    _install_fake_reranker(
        monkeypatch, lambda p: next(v for k, v in pref.items() if k in p)
    )
    hits = retriever.hybrid_retrieve("q", top_k=3, candidate_k=10, rerank=True)
    assert [h["article_id"] for h in hits] == ["D", "B", "A"]
    assert [h["score"] for h in hits] == [9.0, 5.0, 2.0]


def test_rerank_preserves_rrf_score(retriever, monkeypatch):
    _install_fake_reranker(monkeypatch, lambda p: 1.0)
    hits = retriever.hybrid_retrieve("q", top_k=4, candidate_k=10, rerank=True)
    for h in hits:
        assert "rrf_score" in h and h["rrf_score"] > 0.0
        assert h["score"] == 1.0  # replaced by CE score
    # C was rank-1 in both lists -> largest fused score
    rrf = {h["article_id"]: h["rrf_score"] for h in hits}
    assert rrf["C"] == max(rrf.values())


def test_rerank_sets_latency_probe(retriever, monkeypatch):
    _install_fake_reranker(monkeypatch, lambda p: 0.0)
    assert retriever.last_rerank_seconds is None
    retriever.hybrid_retrieve("q", top_k=2, candidate_k=10, rerank=True)
    assert retriever.last_rerank_seconds is not None
    assert retriever.last_rerank_seconds >= 0.0


def test_rerank_prefers_stored_passage_over_rebuilt(retriever, monkeypatch):
    seen_passages = []
    import rag.reranker as rr
    monkeypatch.setattr(
        rr, "rerank",
        lambda q, ps: (seen_passages.extend(ps), [0.0] * len(ps))[1],
    )
    monkeypatch.setattr(
        retriever, "_fetch_passages",
        lambda ids: {"A": "STORED-PASSAGE-A"},  # only A has a stored passage
    )
    retriever.hybrid_retrieve("q", top_k=4, candidate_k=10, rerank=True)
    assert "STORED-PASSAGE-A" in seen_passages              # stored used
    assert any(p.startswith("heading-C") for p in seen_passages)  # fallback used


def test_rerank_false_never_imports_reranker(monkeypatch):
    # A fresh retriever with rerank=False must not touch rag.reranker.
    r = MabhasRetriever("postgresql://unused")
    monkeypatch.setattr(r, "dense_retrieve", lambda q, top_k=3, **kw: [_hit("A")])
    monkeypatch.setattr(r, "lexical_retrieve", lambda q, top_k=3, **kw: [])
    saved = sys.modules.pop("rag.reranker", None)
    try:
        sys.modules["rag.reranker"] = None  # import would raise TypeError
        r.hybrid_retrieve("q", top_k=1, candidate_k=10)  # must not raise
    finally:
        sys.modules.pop("rag.reranker", None)
        if saved is not None:
            sys.modules["rag.reranker"] = saved


def test_build_rerank_passage_fallback_shape():
    p = _build_rerank_passage(_hit("X"))
    assert p == "heading-X\nbody-X\nenglish-X"
    # forward-compat: contextual/normalized fields take precedence if present
    p2 = _build_rerank_passage(
        _hit("X", context_fa="CTX", text_fa_normalized="NORM")
    )
    assert p2.splitlines() == ["CTX", "heading-X", "NORM", "english-X"]


def test_candidate_k_must_cover_top_k(retriever):
    with pytest.raises(ValueError):
        retriever.hybrid_retrieve("q", top_k=20, candidate_k=10)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
