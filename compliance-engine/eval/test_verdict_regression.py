"""
eval/test_verdict_regression.py
===============================
Permanent regression guard for the "deterministic spine" invariant
(Stage 1 / Step 7):

    Retrieval improvements must NEVER change deterministic PASS/FAIL
    verdicts. The retriever feeds ONLY the LLM interpretive pass, which
    annotates NEEDS_REVIEW findings; agents compute verdicts from the
    BIM data and the clause list directly.

The test runs the full orchestrator twice on a fixed BIM fixture —
once with a mock of the OLD retriever (dense cosine ordering) and once
with a mock of the NEW default retriever (hybrid + rerank ordering,
returning DIFFERENT hits on purpose) — and asserts the deterministic
findings are identical while advisory text is allowed to differ (and
demonstrably does differ, so the test has teeth).

Fully self-contained: no database, no embedding model, no network.

Run:
    python -m pytest eval/test_verdict_regression.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Imports: the orchestrator uses flat module names (`from spatial_graph
# import ...`) resolved via the services/ directory, mirroring api/pipeline.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
_SERVICES = _ROOT / "services"
for p in (str(_ROOT), str(_SERVICES)):
    if p not in sys.path:
        sys.path.insert(0, p)

from orchestrator import run_compliance  # noqa: E402
from numeric_checker import Verdict  # noqa: E402

# Reuse the established BIM fixture (known FAIL: kitchen↔bathroom door;
# known PASS: bedroom area) and its clause set.
from tests.test_orchestrator import BIM, CLAUSES  # noqa: E402


# ---------------------------------------------------------------------------
# Mock retrievers — same hit-dict shape as MabhasRetriever, no DB.
# They return deliberately DIFFERENT hits/orderings so that any leakage of
# retrieval results into verdicts would be caught.
# ---------------------------------------------------------------------------

def _hit(article_id: str, text_en: str, score: float, **extra):
    h = {
        "mabhas_part": "4",
        "article_id": article_id,
        "heading_fa": f"عنوان {article_id}",
        "text_fa": f"متن {article_id}",
        "text_en": text_en,
        "rule_type": "numeric",
        "entities": None,
        "context_fa": None,
        "score": score,
    }
    h.update(extra)
    return h


class OldDenseMockRetriever:
    """Simulates the pre-Stage-1 dense retriever: cosine scores in [0,1]."""

    def retrieve(self, query, top_k=3, mabhas_part=None, rule_type=None):
        return [
            _hit("D-1", "Dense context: habitable rooms must have daylight.", 0.83),
            _hit("D-2", "Dense context: ventilation requirements for kitchens.", 0.78),
        ][:top_k]


class NewDefaultMockRetriever:
    """Simulates the Step 7 default (hybrid + rerank): different hits,
    different ordering, CE logit scores, extra rrf_score key."""

    def retrieve(self, query, top_k=3, mabhas_part=None, rule_type=None):
        return [
            _hit("H-9", "Hybrid context: window-to-floor ratio definitions.",
                 4.21, rrf_score=0.0328),
            _hit("H-3", "Hybrid context: exception for service spaces.",
                 1.07, rrf_score=0.0317),
            _hit("D-2", "Dense context: ventilation requirements for kitchens.",
                 0.55, rrf_score=0.0161),
        ][:top_k]


def _context_sensitive_llm(prompt: str) -> str:
    """Fake LLM whose advisory text depends on the retrieved context, so
    advisory notes MUST differ between the two retrievers — proving the
    test tolerates exactly (and only) that difference."""
    return f"Reviewer note derived from context fingerprint {hash(prompt) & 0xffff:04x}."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deterministic_findings(result):
    """The invariant set: every finding's (agent-independent) identity and
    verdict, for ALL verdict classes. Advisory text is excluded — but for
    PASS/FAIL we additionally pin the message, which is deterministic."""
    return {
        (f.article_id, getattr(f, "object_id", None), f.verdict.name)
        for f in result.findings
    }


def _pass_fail_messages(result):
    return {
        (f.article_id, getattr(f, "object_id", None), f.verdict.name, f.message)
        for f in result.findings
        if f.verdict in (Verdict.PASS, Verdict.FAIL)
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def runs():
    old = run_compliance(BIM, CLAUSES, retriever=OldDenseMockRetriever(),
                         llm=_context_sensitive_llm, use_langgraph=False)
    new = run_compliance(BIM, CLAUSES, retriever=NewDefaultMockRetriever(),
                         llm=_context_sensitive_llm, use_langgraph=False)
    return old, new


def test_deterministic_verdict_sets_identical(runs):
    old, new = runs
    assert _deterministic_findings(old) == _deterministic_findings(new)


def test_pass_fail_messages_identical(runs):
    """PASS/FAIL findings are never touched by the LLM pass, so even their
    messages must be byte-identical across retrievers."""
    old, new = runs
    assert _pass_fail_messages(old) == _pass_fail_messages(new)


def test_summary_counts_identical(runs):
    old, new = runs
    assert old.summary == new.summary


def test_known_verdicts_present_in_both(runs):
    """Anchor the fixture's ground truth so a silently-empty run can't pass."""
    for res in runs:
        fails = [f for f in res.findings if f.verdict == Verdict.FAIL]
        assert any("kitchen" in f.message.lower() and "bathroom" in f.message.lower()
                   for f in fails), "known kitchen↔bathroom FAIL missing"
        assert any(f.article_id == "N1" and f.verdict == Verdict.PASS
                   for f in res.findings), "known bedroom-area PASS missing"


def test_advisory_text_differs_proving_test_has_teeth(runs):
    """The two retrievers feed different context to the LLM, so at least one
    NEEDS_REVIEW advisory note must differ — i.e. the invariant above is not
    vacuously true."""
    old, new = runs

    def notes(res):
        return {
            (f.article_id, getattr(f, "object_id", None)): f.message
            for f in res.findings
            if f.verdict == Verdict.NEEDS_REVIEW and "[AI note:" in f.message
        }

    n_old, n_new = notes(old), notes(new)
    assert n_old and n_new, "expected annotated NEEDS_REVIEW items in both runs"
    common = set(n_old) & set(n_new)
    assert common, "expected overlapping NEEDS_REVIEW items"
    assert any(n_old[k] != n_new[k] for k in common), (
        "advisory notes identical across different retrieval contexts — "
        "the regression test would be vacuous"
    )


def test_default_factory_routes_to_hybrid_rerank(monkeypatch):
    """build_default_retriever().retrieve() must call
    hybrid_retrieve(rerank=True) and forward agent filters — no DB needed."""
    import eval.retrieval_eval  # noqa: F401  (bootstraps services.* namespace)
    from rag.rag_retriever import build_default_retriever

    r = build_default_retriever()
    captured = {}

    def fake_hybrid(query, top_k=3, candidate_k=50, rrf_k=60, rerank=False,
                    mabhas_part=None, rule_type=None):
        captured.update(query=query, top_k=top_k, rerank=rerank,
                        mabhas_part=mabhas_part, rule_type=rule_type)
        return []

    monkeypatch.setattr(r, "hybrid_retrieve", fake_hybrid)
    r.retrieve("حداقل مساحت", top_k=4, mabhas_part="4", rule_type="numeric")

    assert captured == {
        "query": "حداقل مساحت", "top_k": 4, "rerank": True,
        "mabhas_part": "4", "rule_type": "numeric",
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
