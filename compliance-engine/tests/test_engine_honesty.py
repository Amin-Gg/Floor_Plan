"""
tests/test_engine_honesty.py
============================
Tests for the Issue 2 / 8 / 9 "honesty & coverage" pass:

* canonical room-category normalization (+ needs_review for the unmappable)
* clause-coverage accounting (PASS/FAIL/NEEDS_REVIEW/UNSUPPORTED/BLOCKED)
* clause-corpus health / fail-fast loading
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SERVICES = _ROOT / "services"
for _p in (str(_ROOT), str(_SERVICES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Issue 2: category normalizer ─────────────────────────────────────────────
def test_normalizer_passes_canonical_through():
    from ingest.category_normalizer import normalize_room_categories
    bim = {"rooms": [{"id": "r1", "category": "room_kitchen"}]}
    summary = normalize_room_categories(bim)
    assert bim["rooms"][0]["category"] == "room_kitchen"
    assert bim["rooms"][0]["category_source"] == "canonical"
    assert summary["canonical"] == 1 and summary["unmapped"] == 0


def test_normalizer_maps_english_persian_and_broad_labels():
    from ingest.category_normalizer import normalize_room_categories
    bim = {"rooms": [
        {"id": "r1", "category": "Kitchen"},                 # english label
        {"id": "r2", "category": "آشپزخانه"},                # persian label
        {"id": "r3", "category": "Service", "name": "Bathroom"},  # broad+name
        {"id": "r4", "category": "Master Bedroom 2"},        # substring
    ]}
    normalize_room_categories(bim)
    cats = {r["id"]: r["category"] for r in bim["rooms"]}
    assert cats == {"r1": "room_kitchen", "r2": "room_kitchen",
                    "r3": "room_bathroom", "r4": "room_bedroom"}
    assert bim["rooms"][2]["category_source"] == "name"  # resolved via the name


def test_normalizer_flags_unmappable_as_needs_review():
    from ingest.category_normalizer import normalize_room_categories
    bim = {"rooms": [{"id": "r1", "category": "Accommodation"},
                     {"id": "r2", "category": "Unknown"}]}
    summary = normalize_room_categories(bim)
    for r in bim["rooms"]:
        assert r["needs_review"] is True
        assert r["category_source"] == "unmapped"
        assert r["review_reasons"]
    assert summary["unmapped"] == 2
    assert summary["normalized"] == 0


# ── Issue 8: coverage accounting ─────────────────────────────────────────────
def test_classify_finding_splits_review_into_classes():
    from coverage import classify_finding, UNSUPPORTED, BLOCKED, NEEDS_REVIEW, PASS, FAIL
    assert classify_finding("PASS", "anything") == PASS
    assert classify_finding("FAIL", "anything") == FAIL
    assert classify_finding(
        "NEEDS_REVIEW", "Object 'x' not mapped to a measurable value — needs review"
    ) == UNSUPPORTED
    # Stage 5: BLOCKED is verdict-level truth only — the legacy message no
    # longer sniffs; the NOT_EVALUATED verdict maps regardless of wording.
    assert classify_finding(
        "NOT_EVALUATED", "No 'room_kitchen' rooms in plan to check"
    ) == BLOCKED
    assert classify_finding(
        "NEEDS_REVIEW", "No 'room_kitchen' rooms in plan to check — needs review"
    ) == NEEDS_REVIEW
    assert classify_finding(
        "NEEDS_REVIEW", "Conditional rule (condition: x) — needs human review"
    ) == NEEDS_REVIEW


def test_build_coverage_counts_clause_level():
    from coverage import build_coverage

    class _F:  # minimal finding stand-in
        def __init__(self, article_id, verdict, message):
            self.article_id, self.message = article_id, message
            self.verdict = type("V", (), {"value": verdict})()

    class _R:
        findings = [
            _F("A", "PASS", "ok"),
            _F("B", "FAIL", "too small"),
            _F("C", "NEEDS_REVIEW", "Conditional rule — needs human review"),
            _F("D", "NOT_EVALUATED", "No 'room_kitchen' rooms in plan to check"),
            _F("E", "NEEDS_REVIEW", "Object 'x' not mapped to a measurable value"),
        ]

    clauses = [{"article_id": x} for x in ("A", "B", "C", "D", "E", "F")]  # F: no finding
    cov = build_coverage(_R(), clauses, corpus_total=10)
    assert cov["total_clauses"] == 6
    assert cov["passed"] == 1 and cov["failed"] == 1
    assert cov["needs_review"] == 1
    assert cov["blocked_by_missing_data"] == 1
    assert cov["unsupported"] == 2          # E (no logic) + F (no finding at all)
    assert cov["checked"] == 3              # pass + fail + needs_review
    assert cov["automatically_checkable"] == 4   # total - unsupported
    assert cov["corpus_total"] == 10 and cov["not_applicable"] == 4


# ── Issue 9: clause-corpus health / fail-fast ────────────────────────────────
def test_load_clauses_required_raises_on_missing(tmp_path):
    from api.pipeline import load_clauses
    assert load_clauses(None, required=False) == []
    with pytest.raises(RuntimeError):
        load_clauses(None, required=True)
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_clauses(str(empty), required=True)


def test_clause_health_reports_count(tmp_path):
    from api.pipeline import clause_health
    p = tmp_path / "clauses.json"
    p.write_text(json.dumps([{"article_id": "N1", "rule_type": "numeric"},
                             {"article_id": "S1", "rule_type": "spatial"}]),
                 encoding="utf-8")
    h = clause_health(str(p))
    assert h["clause_count"] == 2 and h["clause_status"] == "ok"
    assert clause_health(None)["clause_status"] == "degraded"
