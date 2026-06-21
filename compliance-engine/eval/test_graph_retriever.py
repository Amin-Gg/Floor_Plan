"""
eval/test_graph_retriever.py
============================
Stage 3, Step 4 — unit tests for services/graph_retriever.py.

Fully mocked: a fake base retriever (no DB), a fake linker (no GraphML),
and a monkeypatched cross-encoder (no model download). Verifies the four
spec behaviors: exception expansion adds the linked exception, bilingual
element extraction, rerank controls the final ordering, and the frozen
signature / hit shape / provenance tagging.

Run:
    pytest eval/test_graph_retriever.py -v
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from rag.graph_retriever import GraphRetriever, load_clauses_by_id


# ─────────────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────────────

def _clause(article_id: str, rule_type: str = "numeric",
            text_fa: str = "متن", text_en: str = "text",
            mabhas_part: str = "4") -> Dict:
    return {"mabhas_part": mabhas_part, "article_id": article_id,
            "heading_fa": None, "text_fa": text_fa,
            "text_fa_normalized": text_fa, "text_en": text_en,
            "rule_type": rule_type, "entities": None, "context_fa": None,
            "skip_category": None}


CLAUSES = {
    "S1": _clause("S1", text_fa="عرض پلکان"),          # seed 1 (stair base rule)
    "S2": _clause("S2", text_fa="ارتفاع پله"),          # seed 2
    "EXC1": _clause("EXC1", rule_type="exception",
                    text_fa="استثنای کاهش اندازه"),      # exception of S1
    "G1": _clause("G1", text_fa="پاگرد پله"),           # graph-only, governs stair
    "G2": _clause("G2", text_fa="شیب پله"),             # graph-only, governs stair
}


class FakeBase:
    """Stands in for MabhasRetriever / CorrectiveRetriever."""

    def __init__(self):
        self.calls: List[Dict] = []
        self.last_rerank_seconds = None
        self.last_trace = {"path": "crag_high_confidence"}

    def retrieve(self, query: str, top_k: int = 3,
                 mabhas_part: Optional[str] = None,
                 rule_type: Optional[str] = None) -> List[Dict]:
        self.calls.append({"query": query, "top_k": top_k,
                           "mabhas_part": mabhas_part, "rule_type": rule_type})
        hits = []
        for i, aid in enumerate(("S1", "S2")):
            h = dict(CLAUSES[aid])
            h["score"] = 10.0 - i
            hits.append(h)
        return hits[:top_k]


class FakeLinker:
    """Stands in for GraphLinker: S1 carries exception EXC1; the element
    'stair' is governed by S1, G1, G2 (+ EXC1 via expansion)."""

    G = None                                  # degree ranking degrades to 0

    def expand_with_exceptions(self, article_ids: List[str]) -> List[str]:
        out = list(dict.fromkeys(article_ids))
        if "S1" in out and "EXC1" not in out:
            out.append("EXC1")
        return out

    def clauses_for_element(self, element: str,
                            occupancy=None,
                            include_exceptions: bool = True) -> List[str]:
        if element == "stair":
            base = ["G1", "G2", "S1"]
            return base + (["EXC1"] if include_exceptions else [])
        return []


@pytest.fixture()
def patched_reranker(monkeypatch):
    """Deterministic fake cross-encoder: score = 100 - index of the
    candidate's article_id in `order` (so `order` IS the final ranking)."""
    state = {"order": ["S1", "EXC1", "G1", "S2", "G2"], "calls": []}

    def fake_rerank(query: str, passages: List[str]) -> List[float]:
        state["calls"].append({"query": query, "n": len(passages)})
        # passages arrive aligned with the candidates; the retriever zips
        # them back, so we score by matching passage text to clause text.
        scores = []
        for p in passages:
            aid = next((a for a, c in CLAUSES.items()
                        if c["text_fa"] in p), None)
            scores.append(100.0 - state["order"].index(aid)
                          if aid in state["order"] else -100.0)
        return scores

    import rag.reranker as rr
    monkeypatch.setattr(rr, "rerank", fake_rerank)
    return state


@pytest.fixture()
def retriever() -> GraphRetriever:
    return GraphRetriever(base=FakeBase(), linker=FakeLinker(),
                          clauses_by_id=dict(CLAUSES))


# ─────────────────────────────────────────────────────────────────────────────
# Element extraction (no reranker needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_elements_english(retriever):
    assert retriever._extract_elements(
        "minimum stair width in the kitchen") == {"stair", "kitchen"}


def test_extract_elements_persian(retriever):
    assert retriever._extract_elements(
        "حداقل عرض راه پله و مساحت آشپزخانه") == {"stair", "kitchen"}


def test_extract_elements_homograph_and_stoplist(retriever):
    # bare «در» (in/door) must NOT trigger door; «درب» must.
    assert retriever._extract_elements("نور طبیعی در فضا") == set()
    assert retriever._extract_elements("عرض درب ورودی") >= {"door"}
    # «نورگیری» (lighting) must not trigger light_well; «نورگیر» must.
    assert "light_well" not in retriever._extract_elements("نورگیری راه پله")
    assert "light_well" in retriever._extract_elements("ابعاد نورگیر")
    # generic words excluded by design
    assert retriever._extract_elements("building room requirements") == set()


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval flow
# ─────────────────────────────────────────────────────────────────────────────

def test_exception_expansion_adds_exception(retriever, patched_reranker):
    hits = retriever.retrieve("حداقل عرض راه پله", top_k=5)
    ids = [h["article_id"] for h in hits]
    assert "EXC1" in ids                       # vector alone never finds it
    exc_hit = next(h for h in hits if h["article_id"] == "EXC1")
    assert exc_hit["provenance"] == "exception_expansion"
    assert retriever.last_graph_trace["exception_added_n"] == 1


def test_graph_element_candidates_join_the_pool(retriever, patched_reranker):
    hits = retriever.retrieve("minimum stair width", top_k=5)
    by_id = {h["article_id"]: h for h in hits}
    assert {"G1", "G2"} <= set(by_id)          # graph-only candidates surfaced
    assert by_id["G1"]["provenance"] == "graph_element"
    assert by_id["S1"]["provenance"] == "vector"
    assert retriever.last_graph_trace["elements_detected"] == ["stair"]


def test_rerank_controls_final_ordering(retriever, patched_reranker):
    patched_reranker["order"] = ["G2", "EXC1", "S2", "S1", "G1"]
    hits = retriever.retrieve("minimum stair width", top_k=5)
    assert [h["article_id"] for h in hits] == patched_reranker["order"]
    # exactly one cross-encoder call, against the ORIGINAL query
    assert len(patched_reranker["calls"]) == 1
    assert patched_reranker["calls"][0]["query"] == "minimum stair width"


def test_no_elements_no_graph_candidates(retriever, patched_reranker):
    retriever.retrieve("حداقل مساحت", top_k=3)   # no element word
    trace = retriever.last_graph_trace
    assert trace["elements_detected"] == []
    assert trace["graph_candidates_n"] == 0
    assert trace["llm_calls_added"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Contract: signature, hit shape, filters, pass-throughs
# ─────────────────────────────────────────────────────────────────────────────

def test_frozen_signature_and_hit_shape(retriever, patched_reranker):
    hits = retriever.retrieve("stair", top_k=2)   # positional drop-in call
    assert len(hits) == 2
    required = {"mabhas_part", "article_id", "heading_fa", "text_fa",
                "text_en", "rule_type", "entities", "score"}
    for h in hits:
        assert required <= set(h)
        assert "rrf_score" in h                  # additive, like Stage 1/2

    import inspect
    params = list(inspect.signature(retriever.retrieve).parameters)
    assert params == ["query", "top_k", "mabhas_part", "rule_type"]
    assert inspect.signature(retriever.retrieve).parameters["top_k"].default == 3


def test_filters_apply_to_graph_candidates(retriever, patched_reranker):
    hits = retriever.retrieve("minimum stair width", top_k=5,
                              rule_type="numeric")
    assert all(h["rule_type"] == "numeric" for h in hits
               if h["article_id"] in ("G1", "G2", "EXC1"))
    assert "EXC1" not in {h["article_id"] for h in hits}  # contract-correct
    # filters are forwarded to the base retriever unchanged
    assert retriever.base.calls[-1]["rule_type"] == "numeric"


def test_crag_trace_passthrough(retriever):
    assert retriever.last_trace == {"path": "crag_high_confidence"}


def test_load_clauses_by_id_filters_skipped(tmp_path):
    import json
    f = tmp_path / "clauses.json"
    f.write_text(json.dumps([
        _clause("A1"),
        {**_clause("A2"), "skip_category": "administrative"},
    ]), encoding="utf-8")
    loaded = load_clauses_by_id(str(f))
    assert set(loaded) == {"A1"}
