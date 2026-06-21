"""
eval/test_regulation_graph.py
=============================
Stage 3, Step 2 — unit tests for RAG/build_regulation_graph.py.

Self-contained: a tiny synthetic clause list (no corpus, no network, no DB).
Covers every rule_type, the skip filter, the exception cross-reference
resolver (RTL reversed-segment path), the USES_TERM second pass, and the
GraphML round-trip.

Run:
    pytest eval/test_regulation_graph.py -v
"""

from __future__ import annotations

import pytest

from rag.build_regulation_graph import (
    build_graph,
    extract_term,
    load_regulation_graph,
    map_element,
    map_property,
    parse_article_refs,
    resolve_reference,
    save_regulation_graph,
)

# ─────────────────────────────────────────────────────────────────────────────
# Synthetic corpus: 5 ingestable clauses + 1 skipped.
# Article ids deliberately use the corpus's reversed-segment convention so the
# exception test exercises the RTL resolver, not the trivial exact match.
# ─────────────────────────────────────────────────────────────────────────────

SYNTHETIC_CLAUSES = [
    {   # 1) numeric — kitchen minimum area
        "mabhas_part": "4",
        "article_id": "1-1-9-9a",
        "heading_fa": None,
        "text_fa": "مساحت آشپزخانه نباید کمتر از 5.5 متر مربع باشد.",
        "text_fa_normalized": "مساحت آشپزخانه نباید کمتر از 5.5 متر مربع باشد.",
        "text_en": "The kitchen area must not be less than 5.5 square meters.",
        "skip_category": None,
        "applicable_occupancies": ["all_residential"],
        "applicable_height_groups": ["any"],
        "rule_type": "numeric",
        "entities": {"object": "kitchen", "property": "area",
                     "comparator": ">=", "value": 5.5, "unit": "m2",
                     "condition": None},
        "context_fa": "بافت: الزامات آشپزخانه",
    },
    {   # 2) spatial — kitchen must not connect to bathroom
        "mabhas_part": "4",
        "article_id": "2-1-9-9",
        "heading_fa": None,
        "text_fa": "آشپزخانه نباید در مستقیم به فضای بهداشتی داشته باشد.",
        "text_fa_normalized": "آشپزخانه نباید در مستقیم به فضای بهداشتی داشته باشد.",
        "text_en": "The kitchen must not have a direct door to a sanitary space.",
        "skip_category": None,
        "applicable_occupancies": ["all_residential"],
        "applicable_height_groups": ["any"],
        "rule_type": "spatial",
        "entities": {"subject": "kitchen",
                     "relation": "must_not_connect_to",
                     "object": "sanitary space"},
        "context_fa": None,
    },
    {   # 3) definition — defines the term آشپزخانه (glossary colon pattern)
        "mabhas_part": "4",
        "article_id": "3-1-9-9",
        "heading_fa": None,
        "text_fa": "-5 آشپزخانه: فضایی دارای نور و تهویه لازم برای پخت و پز.",
        "text_fa_normalized": "-5 آشپزخانه: فضایی دارای نور و تهویه لازم برای پخت و پز.",
        "text_en": "Kitchen: a space with required light and ventilation for cooking.",
        "skip_category": None,
        "applicable_occupancies": ["all_residential"],
        "applicable_height_groups": ["any"],
        "rule_type": "definition",
        "entities": None,
        "context_fa": None,
    },
    {   # 4) numeric base rule — stair width (target of the exception below)
        "mabhas_part": "4",
        "article_id": "1-9-9",          # reversed-order corpus id for "9-9-1"
        "heading_fa": None,
        "text_fa": "عرض پلکان نباید کمتر از 1.10 متر باشد.",
        "text_fa_normalized": "عرض پلکان نباید کمتر از 1.10 متر باشد.",
        "text_en": "The stair width must not be less than 1.10 meters.",
        "skip_category": None,
        "applicable_occupancies": ["all_residential"],
        "applicable_height_groups": ["any"],
        "rule_type": "numeric",
        "entities": {"object": "stair", "property": "width",
                     "comparator": ">=", "value": 1.10, "unit": "m",
                     "condition": None},
        "context_fa": None,
    },
    {   # 5) exception — cites the base rule by its printed (un-reversed) id
        "mabhas_part": "4",
        "article_id": "2-9-9",
        "heading_fa": None,
        "text_fa": "در ساختمان های گروه م-4، عرض تعیین شده در بند 9-9-1 "
                   "می تواند تا 0.90 متر کاهش یابد.",
        "text_fa_normalized": "در ساختمان های گروه م-4، عرض تعیین شده در بند "
                              "9-9-1 می تواند تا 0.90 متر کاهش یابد.",
        "text_en": "In group M-4 buildings, the width specified in clause "
                   "9-9-1 may be reduced to 0.90 meters.",
        "skip_category": None,
        "applicable_occupancies": ["M-4"],
        "applicable_height_groups": ["low_rise"],
        "rule_type": "exception",
        "entities": None,
        "context_fa": None,
    },
    {   # 6) skipped — must produce no node at all
        "mabhas_part": "4",
        "article_id": "9-9-9",
        "heading_fa": None,
        "text_fa": "مقاومت بتن باید محاسبه شود.",
        "text_fa_normalized": "مقاومت بتن باید محاسبه شود.",
        "text_en": "Concrete strength must be calculated.",
        "skip_category": "structural_calc",
        "applicable_occupancies": [],
        "applicable_height_groups": [],
        "rule_type": None,
        "entities": None,
        "context_fa": None,
    },
]


@pytest.fixture()
def built():
    return build_graph(SYNTHETIC_CLAUSES)


# ─────────────────────────────────────────────────────────────────────────────
# Helper-level tests
# ─────────────────────────────────────────────────────────────────────────────

def test_map_element_aliases():
    assert map_element("kitchen") == "kitchen"
    assert map_element("sanitary space") == "bathroom"
    assert map_element("wall kitchen") == "kitchen"
    assert map_element("glazing_in_corridor") == "window"   # glazing before corridor
    assert map_element("video_display_or_equivalent") is None  # 'play ' guard
    assert map_element("totally unknown thing") is None
    assert map_element(None) is None


def test_map_property_aliases():
    assert map_property("clear height") == "clear_height"
    assert map_property("usable_width") == "clear_width"
    assert map_property("minimum") is None                  # ambiguous -> logged


def test_extract_term_colon_pattern_strips_list_marker():
    term = extract_term(SYNTHETIC_CLAUSES[2])
    assert term == "آشپزخانه"


def test_parse_refs_skips_printed_self_label():
    clause = {"article_id": "10-1-5-4c",
              "text_fa_normalized": "3-10-1-5-4 متن بند مطابق قسمت 4-9-7 مجاز است.",
              "text_en": None}
    assert parse_article_refs(clause) == ["4-9-7"]


def test_resolve_reference_paths():
    ids = ["1-9-9", "2-7-9-4", "5-1-4-4"]
    assert resolve_reference("9-9-1", ids, "x") == (["1-9-9"], "reversed")
    assert resolve_reference("4-9-7", ids, "x") == (["2-7-9-4"], "section_family")
    assert resolve_reference("4-4-1-5-1", ids, "x") == (["5-1-4-4"], "parent_section")
    assert resolve_reference("8-8-8", ids, "x") == ([], "unresolved")


# ─────────────────────────────────────────────────────────────────────────────
# Whole-graph expectations
# ─────────────────────────────────────────────────────────────────────────────

def test_node_counts_by_type(built):
    G, stats = built
    assert stats.node_counts == {
        "Clause": 5,        # the skipped clause produced no node
        "Element": 3,       # kitchen, bathroom, stair
        "Property": 2,      # area, width
        "Occupancy": 2,     # all_residential, M-4
        "Term": 1,          # آشپزخانه
    }
    assert "clause:9-9-9" not in G


def test_edge_counts_by_type(built):
    G, stats = built
    assert stats.edge_counts == {
        "GOVERNS": 3,               # c1->kitchen, c2->kitchen, c4->stair
        "CONSTRAINS_PROPERTY": 2,   # c1->area, c4->width
        "RELATES": 1,               # c2->bathroom
        "APPLIES_TO_OCCUPANCY": 5,  # one per ingestable clause
        "HAS_EXCEPTION": 1,         # base 1-9-9 -> exception 2-9-9
        "DEFINES": 1,
        "USES_TERM": 2,             # c1 and c2 use آشپزخانه; definer excluded
    }
    assert G.number_of_edges() == sum(stats.edge_counts.values())


def test_exception_edge_direction_and_provenance(built):
    G, stats = built
    edges = [(u, v, d) for u, v, d in G.edges(data=True)
             if d["edge_type"] == "HAS_EXCEPTION"]
    assert len(edges) == 1
    u, v, d = edges[0]
    assert u == "clause:1-9-9" and v == "clause:2-9-9"   # base -> exception
    assert d["match_method"] == "reversed"
    assert d["cited_ref"] == "9-9-1"
    assert d["source"] == "derived"
    assert stats.exception_resolved == 1 and stats.exception_total == 1


def test_constrains_property_carries_threshold(built):
    G, _ = built
    edge = next(d for _, v, d in G.edges("clause:1-1-9-9a", data=True)
                if d["edge_type"] == "CONSTRAINS_PROPERTY")
    assert (edge["comparator"], edge["value"], edge["unit"]) == (">=", 5.5, "m2")
    assert edge["condition"] is None


def test_uses_term_excludes_defining_clause(built):
    G, _ = built
    users = {u for u, v, d in G.edges(data=True)
             if d["edge_type"] == "USES_TERM" and v == "term:آشپزخانه"}
    assert users == {"clause:1-1-9-9a", "clause:2-1-9-9"}
    assert "clause:3-1-9-9" not in users                  # no self-loop


def test_numeric_coverage_stats(built):
    _, stats = built
    assert stats.numeric_total == 2 and stats.numeric_linked == 2
    assert stats.unmapped_elements == []


# ─────────────────────────────────────────────────────────────────────────────
# GraphML round-trip
# ─────────────────────────────────────────────────────────────────────────────

def _edge_multiset(G):
    return sorted(
        (u, v, d["edge_type"],
         tuple(sorted((k, repr(val)) for k, val in d.items())))
        for u, v, d in G.edges(data=True))


def test_graphml_round_trip(built, tmp_path):
    G, _ = built
    path = tmp_path / "regulation_graph.graphml"
    save_regulation_graph(G, path)
    H = load_regulation_graph(path)

    assert set(G.nodes) == set(H.nodes)
    for n in G.nodes:
        assert G.nodes[n] == H.nodes[n], f"node attrs differ for {n}"
    assert _edge_multiset(G) == _edge_multiset(H)

    # lists and None survived the scalar-only format
    c = H.nodes["clause:1-1-9-9a"]
    assert c["applicable_occupancies"] == ["all_residential"]
    assert c["heading_fa"] is None
