"""
Stage 7 — LLM advisory targeting + report review-split tests.

Invariants locked here:
  * The interpretive LLM pass NEVER calls the LLM for unsupported findings
    (engine-limitation reviews) — advisory budget goes to judgment clauses
    only. Interpretive findings still get exactly one call per clause.
  * The HTML report shows, per review card, whether the ask is judgment or
    "manual — outside engine scope", and the review tile carries the split.
  * Groq is fully gone: nothing in services/ingest/api imports it, and the
    provider seam rejects it with the removal message (locked in
    test_llm_provider / test_critical_fixes; re-asserted here end-to-end via
    provider_status).
"""
from __future__ import annotations

from numeric_checker import Finding, Verdict
from reporting.generator import generate_report_bundle


# ── LLM targeting ────────────────────────────────────────────────────────────

class _CountingLLM:
    def __init__(self):
        self.calls = 0
    def __call__(self, prompt: str) -> str:
        self.calls += 1
        return "Check the condition on site."


def _run_pass(findings, llm):
    from validation.compliance.runner import _llm_review_interpretive
    clauses_by_id = {f.article_id: {"article_id": f.article_id,
                                    "text_en": f.rule_text_en or "t"}
                     for f in findings}
    _llm_review_interpretive(findings, clauses_by_id, llm=llm, retriever=None)


def test_llm_pass_skips_unsupported_findings():
    llm = _CountingLLM()
    findings = [
        Finding(article_id="A", verdict=Verdict.NEEDS_REVIEW,
                message="Conditional rule — needs human review",
                rule_text_en="if adjacent to open space...",
                unsupported=False),
        Finding(article_id="B", verdict=Verdict.NEEDS_REVIEW,
                message="Unsupported comparator '≈' — needs review",
                rule_text_en="approximately equal rule",
                unsupported=True),
        Finding(article_id="C", verdict=Verdict.NOT_EVALUATED,
                message="no data"),
        Finding(article_id="D", verdict=Verdict.PASS, message="ok"),
    ]
    _run_pass(findings, llm)
    assert llm.calls == 1                       # only clause A
    assert "Check the condition" in findings[0].message
    assert "Check the condition" not in findings[1].message


def test_llm_pass_still_one_call_per_interpretive_clause():
    llm = _CountingLLM()
    findings = [Finding(article_id="A", verdict=Verdict.NEEDS_REVIEW,
                        message=f"room R{i}: judgment needed",
                        element_id=f"R{i}", rule_text_en="t",
                        unsupported=False) for i in range(4)]
    _run_pass(findings, llm)
    assert llm.calls == 1                       # grouped by clause (M3)
    assert all("Check the condition" in f.message for f in findings)


# ── report split ─────────────────────────────────────────────────────────────

def _result_with_split():
    return {"summary": {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 2,
                        "NOT_EVALUATED": 0},
            "duration_s": 0.0, "by_agent": {},
            "findings": [
                Finding(article_id="A", verdict=Verdict.NEEDS_REVIEW,
                        message="confirm on site",
                        unsupported=False).to_dict(),
                Finding(article_id="B", verdict=Verdict.NEEDS_REVIEW,
                        message="Unsupported comparator",
                        unsupported=True).to_dict(),
            ]}


def test_html_badges_unsupported_cards_and_splits_tile(tmp_path):
    paths = generate_report_bundle(_result_with_split(), {"plan_name": "t"},
                             output_dir=str(tmp_path))
    h = open(paths["html"], encoding="utf-8").read()
    assert h.count("manual — outside engine scope") == 1   # card B only
    assert "1 judgment · 1 outside scope" in h             # tile split


def test_html_no_split_line_when_no_reviews(tmp_path):
    result = {"summary": {"PASS": 1, "FAIL": 0, "NEEDS_REVIEW": 0,
                          "NOT_EVALUATED": 0},
              "duration_s": 0.0, "by_agent": {},
              "findings": [Finding(article_id="A", verdict=Verdict.PASS,
                                   message="ok").to_dict()]}
    paths = generate_report_bundle(result, {"plan_name": "t"}, output_dir=str(tmp_path))
    h = open(paths["html"], encoding="utf-8").read()
    assert "outside scope" not in h


# ── groq is gone, end to end ─────────────────────────────────────────────────

def test_provider_status_has_no_groq_fields(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    import rag.llm_client as lc
    st = lc.provider_status()
    assert st["provider"] == "agentrouter"
    assert not any("groq" in k for k in st)
