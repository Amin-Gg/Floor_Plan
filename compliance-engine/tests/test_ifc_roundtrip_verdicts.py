"""
tests/test_ifc_roundtrip_verdicts.py
====================================
IFC Interface Spec §B3 — the round-trip *contract guarantee*, now realizable
because the private deterministic runner is here:

    bim_data ──Step1 export──► plan.ifc ──ifc_to_bim_data──► bim_data'
    _run_compliance_core(bim_data).summary  ==  _run_compliance_core(bim_data').summary

"the IFC is a faithful contract." This is wired into the same suite as
eval/test_verdict_regression.py and uses committed, self-contained fixtures:

    tests/fixtures/sample_plan_bim.json  — the exact source bim_data (= the
        canonical orchestrator BIM fixture + room centroids)
    tests/fixtures/sample_plan.ifc       — that plan exported by Step 1's
        enriched exporter (regenerate with tests/fixtures/regen_sample_plan.py)

No Step-1 exporter is needed at test time; the engine repo stays self-contained.

This module also unit-tests the §B2 honest-degradation post-pass
(downgrade_flagged_findings): a verdict that depends on a flagged element is
forced to NEEDS_REVIEW.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

# Flat imports (orchestrator/numeric_checker) resolve via services/ — same
# bootstrap as eval/test_verdict_regression.py and api/pipeline.py.
_ROOT = Path(__file__).resolve().parents[1]
_SERVICES = _ROOT / "services"
for _p in (str(_ROOT), str(_SERVICES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from validation.compliance.runner import _run_compliance_core, ComplianceResult   # noqa: E402
from numeric_checker import Finding, Verdict                # noqa: E402
from ingest.ifc_to_bim_data import ifc_to_bim_data          # noqa: E402
from ingest.review_prepass import downgrade_flagged_findings  # noqa: E402
from tests.test_orchestrator import CLAUSES                 # noqa: E402

_FIX = _ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def src_bim():
    with open(_FIX / "sample_plan_bim.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def roundtrip_bim():
    return ifc_to_bim_data(str(_FIX / "sample_plan.ifc"))


@pytest.fixture(scope="module")
def runs(src_bim, roundtrip_bim):
    a = _run_compliance_core(src_bim, CLAUSES, use_langgraph=False)
    b = _run_compliance_core(roundtrip_bim, CLAUSES, use_langgraph=False)
    return a, b


def _verdict_map(result):
    """(article_id, element_id) → verdict.name — the deterministic identity."""
    return {(f.article_id, f.element_id): f.verdict.name for f in result.findings}


# ── §B3 guarantee ─────────────────────────────────────────────────────────────
def test_summary_counts_identical(runs):
    src, rt = runs
    assert src.summary == rt.summary, f"{src.summary} != {rt.summary}"


def test_per_article_verdicts_identical(runs):
    src, rt = runs
    assert _verdict_map(src) == _verdict_map(rt)


def test_known_verdicts_present_in_both(runs):
    """Anchor the ground truth so a silently-empty run can't pass the equality."""
    for res in runs:
        fails = [f for f in res.findings if f.verdict == Verdict.FAIL]
        assert any("kitchen" in f.message.lower() and "bathroom" in f.message.lower()
                   for f in fails), "known kitchen↔bathroom FAIL missing"
        assert any(f.article_id == "N1" and f.verdict == Verdict.PASS
                   for f in res.findings), "known bedroom-area PASS missing"


def test_roundtrip_preserves_categories(roundtrip_bim):
    cats = sorted(r["category"] for r in roundtrip_bim["rooms"])
    assert cats == ["room_bathroom", "room_bedroom", "room_kitchen"]


# ── full ingest pipeline (Lane 2) runs end-to-end on the IFC ─────────────────
def test_ingest_pipeline_matches_raw(src_bim):
    """run_ifc_compliance on the file yields the same deterministic summary as
    running the engine on the source dict (the fixture has no uncertain
    elements, so the B2 pre-pass downgrades nothing)."""
    from tests.helpers import run_ifc_compliance
    result, bim_data = run_ifc_compliance(str(_FIX / "sample_plan.ifc"), CLAUSES)
    raw = _run_compliance_core(src_bim, CLAUSES, use_langgraph=False)
    assert result.summary == raw.summary
    assert bim_data["_review_summary"]["downgraded_count"] == 0
    assert bim_data["_categories_seen"] == {
        "room_bedroom": 1, "room_kitchen": 1, "room_bathroom": 1}


# ── §B2 honest-degradation post-pass ─────────────────────────────────────────
def test_downgrade_flagged_findings_forces_review():
    """A PASS/FAIL finding on a flagged element must become NOT_EVALUATED
    (Stage 1: untrusted data = check impossible, not human-judgment)."""
    result = ComplianceResult(
        findings=[
            Finding(article_id="N1", verdict=Verdict.PASS,
                    message="Rx: area = 9.0 m (required >= 8 m) → PASS",
                    element_id="Rx"),
            Finding(article_id="N2", verdict=Verdict.PASS,
                    message="Ry: area = 12.0 m → PASS", element_id="Ry"),
        ],
    )
    from numeric_checker import summarise
    result.summary = summarise(result.findings)
    assert result.summary["PASS"] == 2

    bim_data = {"_review_summary": {"flagged": [
        {"collection": "rooms", "id": "Rx",
         "reason": "detector confidence 0.30 < threshold 0.50"}]}}
    downgrade_flagged_findings(result, bim_data)

    by_id = {f.element_id: f for f in result.findings}
    assert by_id["Rx"].verdict == Verdict.NOT_EVALUATED
    assert "downgraded" in by_id["Rx"].message.lower()
    assert by_id["Ry"].verdict == Verdict.PASS           # untouched
    assert result.summary == {"PASS": 1, "FAIL": 0, "NEEDS_REVIEW": 0,
                              "NOT_EVALUATED": 1}
    assert bim_data["_review_summary"]["downgraded_count"] == 1
