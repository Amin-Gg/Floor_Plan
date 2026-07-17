"""
Stage 1 — NOT_EVALUATED verdict semantics (CORENET X L2 gating).

Invariants locked here:
  * NOT_EVALUATED = "check impossible, required data absent/untrusted".
    NEEDS_REVIEW  = "check requires human judgment".
    The two must never be conflated again.
  * NOT_EVALUATED never enters the human review queue and never reaches the
    LLM interpretive pass.
  * summarise() carries all four verdict keys and never KeyErrors.
  * Coverage maps NOT_EVALUATED → BLOCKED_BY_MISSING_DATA at verdict level
    (no message sniffing).
  * The HTML report never claims "compliant" while checks are unevaluated,
    and BCF exports only model-anchored NOT_EVALUATED topics; global findings remain in JSON/HTML.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from numeric_checker import Finding, NumericChecker, Verdict, summarise
from services.coverage import BLOCKED, classify_finding
from reporting.generator import generate_report_bundle
from reporting.report_model import OverallCode, compute_overall_status
from services.review_queue import ReviewQueue


def _clause(art="4-5-1", obj="bedroom", prop="area", comp=">=", val=6.5,
            unit="m2"):
    return {"article_id": art, "rule_type": "numeric",
            "text_en": "test clause",
            "entities": {"object": obj, "property": prop, "comparator": comp,
                         "value": val, "unit": unit, "condition": None}}


# ── verdict + summarise ───────────────────────────────────────────────────────

def test_verdict_enum_has_not_evaluated():
    assert Verdict.NOT_EVALUATED.value == "NOT_EVALUATED"


def test_summarise_carries_all_four_keys_and_counts():
    fs = [Finding(article_id="a", verdict=Verdict.PASS, message=""),
          Finding(article_id="b", verdict=Verdict.NOT_EVALUATED, message="")]
    s = summarise(fs)
    assert s == {"PASS": 1, "FAIL": 0, "NEEDS_REVIEW": 0, "NOT_EVALUATED": 1}


# ── routing: data absence vs judgment ────────────────────────────────────────

def test_missing_room_category_is_not_evaluated():
    bim = {"rooms": [], "doors": [], "windows": []}
    f = NumericChecker(bim).check_all([_clause()])[0]
    assert f.verdict == Verdict.NOT_EVALUATED
    assert "not evaluated" in f.message.lower()


def test_unmeasurable_area_is_not_evaluated():
    bim = {"rooms": [{"id": "R1", "category": "room_bedroom", "area_m2": None,
                      "dimensions": {}}],
           "doors": [], "windows": []}
    f = NumericChecker(bim).check_all([_clause()])[0]
    assert f.verdict == Verdict.NOT_EVALUATED


def test_conditional_clause_stays_needs_review():
    c = _clause()
    c["entities"]["condition"] = "adjacent to open space"
    bim = {"rooms": [{"id": "R1", "category": "room_bedroom", "area_m2": 9.0}],
           "doors": [], "windows": []}
    f = NumericChecker(bim).check_all([c])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW      # judgment, not data


def test_unmapped_object_stays_needs_review():
    c = _clause(obj="mechanical shaft")
    bim = {"rooms": [], "doors": [], "windows": []}
    f = NumericChecker(bim).check_all([c])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW      # engine limitation, not data


# ── coverage mapping ─────────────────────────────────────────────────────────

def test_coverage_maps_not_evaluated_to_blocked_without_message_sniffing():
    assert classify_finding("NOT_EVALUATED", "any message at all") == BLOCKED


# ── review queue exclusion ───────────────────────────────────────────────────

def test_not_evaluated_never_enters_review_queue(tmp_path):
    q = ReviewQueue(str(tmp_path / "queue.json"))
    result = {"findings": [
        {"article_id": "a", "verdict": "NEEDS_REVIEW", "message": "judge me"},
        {"article_id": "b", "verdict": "NOT_EVALUATED", "message": "no data"},
    ]}
    items = q.enqueue_result(result, plan_id="p1")
    assert len(items) == 1
    assert items[0]["article_id"] == "a"


# ── report + BCF ─────────────────────────────────────────────────────────────

def test_overall_status_never_compliant_with_unevaluated_checks():
    findings = [
        {"finding_id": f"p{i}", "stage": "compliance", "category": "compliance", "severity": "info", "verdict": "PASS"}
        for i in range(5)
    ] + [
        {"finding_id": f"n{i}", "stage": "compliance", "category": "compliance", "severity": "alert", "verdict": "NOT_EVALUATED"}
        for i in range(2)
    ]
    status = compute_overall_status(
        mode="full_check",
        stages={"schema": None, "quality": None, "compliance": {"status": "completed", "findings": findings}},
    )
    assert status.code is OverallCode.INCOMPLETE


def test_bcf_skips_unanchored_not_evaluated_findings(tmp_path):
    """Global NOT_EVALUATED findings stay in JSON/HTML, not empty BCF topics."""
    result = {
        "summary": {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 0,
                    "NOT_EVALUATED": 1},
        "duration_s": 0.0, "by_agent": {},
        "findings": [{"article_id": "4-5-1", "verdict": "NOT_EVALUATED",
                      "message": "no bedroom in plan", "object": "bedroom"}],
    }
    paths = generate_report_bundle(result, {"plan_name": "t"}, output_dir=str(tmp_path))
    with zipfile.ZipFile(paths["bcf"]) as z:
        topics = [n for n in z.namelist() if n.endswith("markup.bcf")]
    assert topics == []

    manifest = json.loads(Path(paths["bcf_manifest"]).read_text(encoding="utf-8"))
    assert manifest["topics_total"] == 0
    assert manifest["skipped_total"] == 1
    assert manifest["skipped"][0]["reason"] == "global or unanchored finding"
