"""
Stage 6 — field-first UNSUPPORTED classification + LLM-build honesty tests.

Invariants locked here:
  * classify_finding trusts Finding.unsupported over message wording in BOTH
    directions: a marker-looking message with unsupported=False stays
    NEEDS_REVIEW; an interpretive-looking message with unsupported=True is
    UNSUPPORTED. Wording can change freely without breaking coverage.
  * Marker sniffing survives only for legacy dict findings lacking the field
    (unsupported=None).
  * Every live engine-limitation emitter actually sets the flag.
  * A groq-fallback dispatch in a build without rag/groq_client fails with an
    actionable RuntimeError, never a bare ModuleNotFoundError mid-request.
"""
from __future__ import annotations

import pytest

from numeric_checker import NumericChecker, Verdict
from services.coverage import (NEEDS_REVIEW, UNSUPPORTED, classify_finding)


# ── field beats wording, both directions ─────────────────────────────────────

def test_field_true_wins_over_interpretive_wording():
    assert classify_finding(
        "NEEDS_REVIEW", "Confirm the site condition with the municipality",
        unsupported=True) == UNSUPPORTED


def test_field_false_wins_over_marker_wording():
    assert classify_finding(
        "NEEDS_REVIEW", "Object 'x' not mapped to a measurable value",
        unsupported=False) == NEEDS_REVIEW


def test_legacy_dicts_without_field_still_sniff():
    assert classify_finding(
        "NEEDS_REVIEW", "Object 'x' not mapped to a measurable value",
        unsupported=None) == UNSUPPORTED
    assert classify_finding(
        "NEEDS_REVIEW", "Conditional rule — needs human review",
        unsupported=None) == NEEDS_REVIEW


# ── emitters set the flag ────────────────────────────────────────────────────

def _clause(obj="mechanical shaft", comp=">=", prop="area", unit="m2"):
    return {"article_id": "X", "rule_type": "numeric", "text_en": "t",
            "entities": {"object": obj, "property": prop, "comparator": comp,
                         "value": 1.0, "unit": unit, "condition": None}}


def test_numeric_unmapped_object_sets_flag():
    f = NumericChecker({"rooms": [], "doors": [],
                        "windows": []}).check_all([_clause()])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW and f.unsupported is True
    assert f.to_dict()["unsupported"] is True


def test_numeric_unsupported_comparator_sets_flag():
    f = NumericChecker({"rooms": [{"id": "R1", "category": "room_bedroom",
                                   "area_m2": 9.0}], "doors": [],
                        "windows": []}).check_all(
        [_clause(obj="bedroom", comp="≈")])[0]
    assert f.unsupported is True


def test_numeric_interpretive_condition_does_not_set_flag():
    c = _clause(obj="bedroom")
    c["entities"]["condition"] = "adjacent to open space"
    f = NumericChecker({"rooms": [{"id": "R1", "category": "room_bedroom",
                                   "area_m2": 9.0}], "doors": [],
                        "windows": []}).check_all([c])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW and f.unsupported is False


def test_topology_unhandled_relation_sets_flag():
    from spatial_graph import SpatialGraph
    from topology_agent import TopologyAgent
    clause = {"article_id": "T1", "rule_type": "spatial", "text_en": "t",
              "entities": {"subject": "kitchen", "object": "toilet",
                           "relation": "must_levitate_above"}}
    bim = {"rooms": [], "walls": [], "doors": [], "windows": []}
    f = TopologyAgent(SpatialGraph(bim)).check_all([clause])[0]
    assert f.unsupported is True


def test_opening_unparseable_glazing_form_sets_flag():
    from spatial_graph import SpatialGraph
    from opening_agent import OpeningAgent
    clause = {"article_id": "O1", "rule_type": "numeric", "text_en": "t",
              "entities": {"object": "glazing", "property": "quality",
                           "comparator": None, "value": None, "unit": None}}
    bim = {"rooms": [], "walls": [], "doors": [], "windows": []}
    fs = OpeningAgent(SpatialGraph(bim)).check_all([clause])
    f = [x for x in fs if x.article_id == "O1"][0]
    assert f.unsupported is True


# ── LLM build honesty ────────────────────────────────────────────────────────

def test_groq_dispatch_raises_removal_error():
    """Groq removal (2026-07) superseded the Stage-6 'module absent' guard:
    the branch now always raises the explicit removal message."""
    import rag.llm_client as lc
    with pytest.raises(RuntimeError, match="Groq was removed"):
        lc.llm_chat([{"role": "user", "content": "hi"}], provider="groq")
