"""
Unit tests for RAG/crag_retriever.CorrectiveRetriever.

Added during Stage 2 packaging (Step 5 spec required no tests, but branch
coverage of the orchestration is cheap and valuable). A FakeBase stands in
for MabhasRetriever — no API calls, no DB, no models.

Run from the project root:  python -m pytest eval/test_crag_retriever.py -v
"""

import pytest

from rag.crag_retriever import CorrectiveRetriever


def make_hits(scores, prefix="a", part_cycle=("3",)):
    return [
        {"mabhas_part": part_cycle[i % len(part_cycle)],
         "article_id": f"{prefix}{i}", "heading_fa": "ع", "text_fa": "م",
         "text_en": "t", "rule_type": "numeric", "entities": {}, "score": s}
        for i, s in enumerate(scores)
    ]


HIGH_SCORES = [0.85, 0.60, 0.50, 0.45, 0.40]
LOW_SCORES = [0.20, 0.18, 0.15, 0.10, 0.05]
MEDIUM_SCORES = [0.55, 0.48, 0.45, 0.40, 0.35]

# Queries chosen to hit specific router rules deterministically:
Q_SHORT_NUMERIC_FA = "حداقل عرض راهرو چقدر است؟"          # R3 -> hyde
Q_SHORT_NUMERIC_EN = "minimum ceiling height residential?"  # R3 -> hyde
Q_NEUTRAL_FA = "توضیح کلی درباره اهداف مقررات ملی ساختمان ایران بدهید لطفا"  # R6


class FakeBase:
    """Duck-typed stand-in for MabhasRetriever."""

    def __init__(self, initial, hyde=None, stepback=None, multi=None):
        self._initial = initial
        self._hyde = hyde or []
        self._stepback = stepback or []
        self._multi = multi or []
        self.calls = []  # (method, top_k, language-or-None)

    def hybrid_retrieve(self, query, top_k=5, rerank=True, **kw):
        self.calls.append(("hybrid", top_k, None))
        return list(self._initial)[:top_k]

    def hyde_retrieve(self, query, top_k=5, rerank=True, language="fa", **kw):
        self.calls.append(("hyde", top_k, language))
        return list(self._hyde)[:top_k]

    def stepback_retrieve(self, query, top_k=5, rerank=True, language="fa", **kw):
        self.calls.append(("stepback", top_k, language))
        return list(self._stepback)[:top_k]

    def multi_query_retrieve(self, query, top_k=5, rerank=True, language="fa", **kw):
        self.calls.append(("multi_query", top_k, language))
        return list(self._multi)[:top_k]


# --- Branch C: HIGH short-circuit -------------------------------------------

def test_high_confidence_returns_immediately_no_transforms():
    base = FakeBase(initial=make_hits(HIGH_SCORES))
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_NEUTRAL_FA, top_k=5)
    assert [h["article_id"] for h in out] == ["a0", "a1", "a2", "a3", "a4"]
    assert cr.last_trace["branch"] == "high_no_transform"
    assert cr.last_trace["confidence_initial"] == "HIGH"
    assert cr.last_trace["transforms_tried"] == []
    # Exactly one base retrieval, no transform methods touched.
    assert [c[0] for c in base.calls] == ["hybrid"]


# --- Branch E: primary transform accepted ------------------------------------

def test_primary_transform_accepted_when_confidence_recovers():
    base = FakeBase(
        initial=make_hits(LOW_SCORES, prefix="a"),
        hyde=make_hits(HIGH_SCORES, prefix="b"),
    )
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_SHORT_NUMERIC_FA, top_k=5)  # R3 -> primary hyde
    assert [h["article_id"] for h in out] == ["b0", "b1", "b2", "b3", "b4"]
    t = cr.last_trace
    assert t["branch"] == "transform_primary"
    assert t["rule"] == "R3"
    assert t["primary"] == "hyde"
    assert t["transforms_tried"] == ["hyde"]
    assert t["top5_changed"] is True


def test_top5_changed_false_when_transform_returns_same_ids():
    # Transform recovers confidence but lands on the same top-5 ids.
    base = FakeBase(
        initial=make_hits(LOW_SCORES, prefix="a"),
        hyde=make_hits(HIGH_SCORES, prefix="a"),  # same ids, better scores
    )
    cr = CorrectiveRetriever(base)
    cr.retrieve(Q_SHORT_NUMERIC_FA, top_k=5)
    assert cr.last_trace["branch"] == "transform_primary"
    assert cr.last_trace["top5_changed"] is False


def test_language_detected_from_script():
    base_en = FakeBase(initial=make_hits(LOW_SCORES),
                       hyde=make_hits(HIGH_SCORES, prefix="b"))
    CorrectiveRetriever(base_en).retrieve(Q_SHORT_NUMERIC_EN, top_k=5)
    assert ("hyde", 5, "en") in base_en.calls

    base_fa = FakeBase(initial=make_hits(LOW_SCORES),
                       hyde=make_hits(HIGH_SCORES, prefix="b"))
    CorrectiveRetriever(base_fa).retrieve(Q_SHORT_NUMERIC_FA, top_k=5)
    assert ("hyde", 5, "fa") in base_fa.calls


# --- Branch F: fallback transform ---------------------------------------------

def test_fallback_runs_when_primary_stays_low():
    base = FakeBase(
        initial=make_hits(LOW_SCORES, prefix="a"),
        hyde=make_hits(LOW_SCORES, prefix="b"),    # primary stays LOW
        multi=make_hits(MEDIUM_SCORES, prefix="c"),
    )
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_SHORT_NUMERIC_FA, top_k=5)  # R3: hyde -> multi_query
    assert [h["article_id"] for h in out] == ["c0", "c1", "c2", "c3", "c4"]
    t = cr.last_trace
    assert t["branch"] == "transform_fallback"
    assert t["transforms_tried"] == ["hyde", "multi_query"]
    assert t["top5_changed"] is True


# --- Branch G: give-up paths ---------------------------------------------------

def test_give_up_no_primary_on_medium_default_route():
    # MEDIUM confidence + no lexical triggers -> R6 (primary none) -> give up.
    base = FakeBase(initial=make_hits(MEDIUM_SCORES))
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_NEUTRAL_FA, top_k=5)
    assert [h["article_id"] for h in out] == ["a0", "a1", "a2", "a3", "a4"]
    t = cr.last_trace
    assert t["branch"] == "give_up_no_primary"
    assert t["rule"] == "R6"
    assert t["fallback"] == "hyde"          # recorded but (by spec) not run
    assert t["transforms_tried"] == []
    assert [c[0] for c in base.calls] == ["hybrid"]


def test_give_up_after_primary_when_fallback_invalid(monkeypatch):
    # No current rule emits fallback in {none, primary}; force it to cover
    # the defensive branch.
    monkeypatch.setattr(
        "rag.crag_retriever.route_query",
        lambda q, initial_hits=None: {"primary": "hyde", "fallback": "none",
                                      "reason": "forced", "rule": "RX"},
    )
    base = FakeBase(
        initial=make_hits(LOW_SCORES, prefix="a"),
        hyde=make_hits(LOW_SCORES, prefix="b"),  # primary stays LOW
    )
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_NEUTRAL_FA, top_k=5)
    # Original result returned; the failed transform's hits are discarded.
    assert [h["article_id"] for h in out] == ["a0", "a1", "a2", "a3", "a4"]
    assert cr.last_trace["branch"] == "give_up_after_primary"
    assert cr.last_trace["transforms_tried"] == ["hyde"]


# --- Filters + over-fetch --------------------------------------------------------

def test_filters_overfetch_and_postfilter():
    # 25 HIGH-confidence hits alternating mabhas_part 3/4.
    scores = [0.95, 0.70, 0.50] + [0.50 - 0.01 * i for i in range(1, 23)]
    base = FakeBase(initial=make_hits(scores, part_cycle=("3", "4")))
    cr = CorrectiveRetriever(base)
    out = cr.retrieve(Q_NEUTRAL_FA, top_k=5, mabhas_part="4")
    # Over-fetch: hybrid was asked for max(4*5, 20) = 20 candidates.
    assert ("hybrid", 20, None) in base.calls
    assert len(out) == 5
    assert all(h["mabhas_part"] == "4" for h in out)
    assert cr.last_trace["branch"] == "high_no_transform"


def test_rule_type_filter_applied():
    hits = make_hits([0.95, 0.70, 0.50, 0.45, 0.40, 0.35, 0.30, 0.28,
                      0.26, 0.24, 0.22, 0.20, 0.18, 0.16, 0.14, 0.12,
                      0.11, 0.10, 0.09, 0.08])
    for i, h in enumerate(hits):
        h["rule_type"] = "spatial" if i % 2 else "numeric"
    cr = CorrectiveRetriever(FakeBase(initial=hits))
    out = cr.retrieve(Q_NEUTRAL_FA, top_k=5, rule_type="spatial")
    assert len(out) == 5
    assert all(h["rule_type"] == "spatial" for h in out)


def test_no_filter_means_exact_topk_fetch():
    base = FakeBase(initial=make_hits(HIGH_SCORES))
    CorrectiveRetriever(base).retrieve(Q_NEUTRAL_FA, top_k=5)
    assert ("hybrid", 5, None) in base.calls  # no over-fetch without filters


# --- Drop-in compatibility -----------------------------------------------------

def test_retrieve_signature_matches_contract():
    import inspect
    params = list(inspect.signature(CorrectiveRetriever.retrieve).parameters)
    assert params == ["self", "query", "top_k", "mabhas_part", "rule_type"]
