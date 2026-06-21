"""
tests/test_graph_linker.py
==========================
Stage 3, Step 3 — unit tests for services/graph_linker.py.

Reuses the synthetic 5-clause corpus from eval/test_regulation_graph.py
(Step 2): kitchen is governed by a numeric and a spatial clause; stair is
governed by a base rule 1-9-9 carrying the M-4 exception 2-9-9; bathroom is
only a RELATES target and therefore governed by nothing.

Run:
    pytest tests/test_graph_linker.py -v
"""

from __future__ import annotations

import pytest

from eval.test_regulation_graph import SYNTHETIC_CLAUSES
from rag.build_regulation_graph import build_graph, save_regulation_graph
from services.graph_linker import GraphLinker, _occupancy_applies


@pytest.fixture(scope="module")
def linker(tmp_path_factory) -> GraphLinker:
    """Build the synthetic graph once, save as GraphML, load via GraphLinker."""
    G, _ = build_graph(SYNTHETIC_CLAUSES)
    path = tmp_path_factory.mktemp("graph") / "regulation_graph.graphml"
    save_regulation_graph(G, path)
    return GraphLinker(regulation_graph_path=str(path))


# ── clauses_for_element ──────────────────────────────────────────────────────

def test_clauses_for_element_returns_expected_ids(linker):
    assert linker.clauses_for_element("kitchen") == ["1-1-9-9a", "2-1-9-9"]


def test_relates_target_is_not_governed(linker):
    # bathroom appears only as a RELATES object — RELATES does not govern
    assert linker.clauses_for_element("bathroom") == []


def test_unknown_element_returns_empty(linker):
    assert linker.clauses_for_element("swimming_pool") == []


# ── occupancy filtering ──────────────────────────────────────────────────────

def test_occupancy_subsumption_helper():
    assert _occupancy_applies(["all_residential"], "M-2") is True
    assert _occupancy_applies(["any"], "M-4") is True
    assert _occupancy_applies(["M-4"], "M-4") is True
    assert _occupancy_applies(["M-4"], "M-2") is False
    assert _occupancy_applies(["M-4"], None) is True       # no filter


def test_occupancy_filter_drops_mismatched_exception(linker):
    # base 1-9-9 is all_residential; its exception 2-9-9 is M-4-only
    assert linker.clauses_for_element("stair", occupancy="M-2") == ["1-9-9"]
    assert linker.clauses_for_element("stair", occupancy="M-4") == \
        ["1-9-9", "2-9-9"]


# ── exception expansion ──────────────────────────────────────────────────────

def test_include_exceptions_toggle(linker):
    assert linker.clauses_for_element("stair", include_exceptions=False) == \
        ["1-9-9"]
    assert linker.clauses_for_element("stair", include_exceptions=True) == \
        ["1-9-9", "2-9-9"]


def test_expand_with_exceptions_order_and_dedup(linker):
    # input order preserved, duplicate input dropped, exception appended last
    out = linker.expand_with_exceptions(["1-9-9", "1-1-9-9a", "1-9-9"])
    assert out == ["1-9-9", "1-1-9-9a", "2-9-9"]
    # already-present exception is not appended twice
    assert linker.expand_with_exceptions(["2-9-9", "1-9-9"]) == \
        ["2-9-9", "1-9-9"]


# ── clauses_for_room ─────────────────────────────────────────────────────────

def test_clauses_for_room_accepts_spatialgraph_category(linker):
    room = {"category": "room_kitchen", "area_m2": 7.2}
    assert linker.clauses_for_room(room) == ["1-1-9-9a", "2-1-9-9"]


def test_clauses_for_room_accepts_plain_type_and_override(linker):
    assert linker.clauses_for_room({"type": "stair"}) == ["1-9-9", "2-9-9"]
    assert linker.clauses_for_room(
        {"type": "stair", "occupancy_override": "M-2"}) == ["1-9-9"]


def test_clauses_for_room_unknown_type_is_empty(linker):
    assert linker.clauses_for_room({"type": "room_spaceship"}) == []
    assert linker.clauses_for_room({}) == []


# ── explain_link ─────────────────────────────────────────────────────────────

def test_explain_link_direct_path(linker):
    info = linker.explain_link("1-1-9-9a", "kitchen")
    assert info["found"] is True
    assert info["path_kind"] == "direct"
    assert len(info["edges"]) == 1
    edge = info["edges"][0]
    assert edge["from"] == "clause:1-1-9-9a"
    assert edge["to"] == "element:kitchen"
    assert edge["edge_type"] == "GOVERNS"
    assert edge["source"] == "metadata"


def test_explain_link_via_exception(linker):
    info = linker.explain_link("2-9-9", "stair")
    assert info["found"] is True
    assert info["path_kind"] == "via_exception"
    assert [e["edge_type"] for e in info["edges"]] == \
        ["GOVERNS", "HAS_EXCEPTION"]
    assert info["edges"][0]["from"] == "clause:1-9-9"
    assert info["edges"][1]["to"] == "clause:2-9-9"
    assert info["edges"][1]["match_method"] == "reversed"


def test_explain_link_no_path(linker):
    info = linker.explain_link("1-1-9-9a", "stair")
    assert info == {"found": False, "element": "stair",
                    "clause": "1-1-9-9a", "path_kind": None, "edges": []}
