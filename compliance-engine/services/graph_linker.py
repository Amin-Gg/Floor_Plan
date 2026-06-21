"""
services/graph_linker.py
========================
Stage 3, Step 3 — SpatialGraph ↔ RegulationGraph link layer.

Joins a concrete building (the SpatialGraph built from a floor plan) to the
Mabhas regulation graph (Stage 3, Step 2) **without merging the two graphs**.
The join key is the closed Element vocabulary: SpatialGraph room categories
("room_kitchen", …) map to canonical Element types ("kitchen", …), whose
Element nodes in the regulation graph carry incoming GOVERNS edges from every
clause that regulates that element type. The join is computed on demand —
no building-specific data is ever written into the regulation graph, and no
regulation data is copied onto the building graph.

Deviation from the Step 3 spec sketch (documented):
    The sketch constructs with networkx.read_graphml(...) and a default path
    "data/regulation_graph.gml". Step 2 serializes lists/None behind a
    "__json__:" sentinel that raw read_graphml would NOT decode (occupancy
    filters would silently compare against sentinel strings), so this class
    uses rag.build_regulation_graph.load_regulation_graph() and defaults to
    the Step 2 output path "data/regulation_graph.graphml".

Complexity: the constructor builds dict indexes in one O(E) pass; every
query method is O(degree of the touched node), never O(graph).

Deterministic only — no LLM calls. Verdict agents may safely consume this.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from rag.build_regulation_graph import CANONICAL_ELEMENTS, load_regulation_graph

logger = logging.getLogger(__name__)

# SpatialGraph room category -> canonical Element type (reverse of the
# CANONICAL_ELEMENTS mapping; only the categories the floor-plan model emits).
SPATIAL_CATEGORY_TO_ELEMENT: Dict[str, str] = {
    cat: elem for elem, cat in CANONICAL_ELEMENTS.items() if cat is not None
}

# Occupancy subsumption resolved at query time (schema §2.3):
# a clause tagged "any" applies to every occupancy; "all_residential"
# applies to every residential sub-group.
_RESIDENTIAL_GROUPS = {"M-1", "M-2", "M-3", "M-4"}


def _occupancy_applies(clause_occupancies: List[str],
                       query_occupancy: Optional[str]) -> bool:
    """True when a clause's occupancy tags cover the queried occupancy."""
    if query_occupancy is None:
        return True
    occs = set(clause_occupancies or [])
    if "any" in occs or query_occupancy in occs:
        return True
    if query_occupancy in _RESIDENTIAL_GROUPS and "all_residential" in occs:
        return True
    return False


class GraphLinker:
    """
    On-demand join between a building's SpatialGraph and the regulation graph.

    Parameters
    ----------
    regulation_graph_path : str
        GraphML file produced by rag.build_regulation_graph (Step 2).
    """

    #: edge types that mean "this clause regulates this element type"
    GOVERNING_EDGE_TYPES = ("GOVERNS",)

    def __init__(self,
                 regulation_graph_path: str = "data/regulation_graph.graphml"):
        self.G = load_regulation_graph(Path(regulation_graph_path))

        # ── O(E) index pass ────────────────────────────────────────────────
        self._clauses_by_element: Dict[str, List[str]] = {}
        self._exceptions_of: Dict[str, List[str]] = {}
        self._clause_occupancies: Dict[str, List[str]] = {}

        for node, attrs in self.G.nodes(data=True):
            if attrs.get("node_type") == "Clause":
                self._clause_occupancies[attrs["article_id"]] = (
                    attrs.get("applicable_occupancies") or [])

        for u, v, data in self.G.edges(data=True):
            etype = data.get("edge_type")
            if etype in self.GOVERNING_EDGE_TYPES:
                element = self.G.nodes[v].get("canonical_type")
                article = self.G.nodes[u].get("article_id")
                if element and article:
                    bucket = self._clauses_by_element.setdefault(element, [])
                    if article not in bucket:
                        bucket.append(article)
            elif etype == "HAS_EXCEPTION":
                base = self.G.nodes[u].get("article_id")
                exc = self.G.nodes[v].get("article_id")
                if base and exc:
                    bucket = self._exceptions_of.setdefault(base, [])
                    if exc not in bucket:
                        bucket.append(exc)

        for bucket in self._clauses_by_element.values():
            bucket.sort()                     # deterministic output order
        logger.info("GraphLinker ready: %d element types indexed, "
                    "%d clauses carry exceptions",
                    len(self._clauses_by_element), len(self._exceptions_of))

    # ── public queries ─────────────────────────────────────────────────────

    def clauses_for_element(self,
                            element_type: str,
                            occupancy: Optional[str] = None,
                            include_exceptions: bool = True) -> List[str]:
        """
        All clause article_ids that govern this element type.

        occupancy : optional occupancy code of the *building* (e.g. "M-4").
            Clauses whose applicable_occupancies do not cover it are dropped;
            "any" and (for M-groups) "all_residential" subsume. The same
            filter applies to appended exception clauses — an M-4-only
            exception is not applicable to an M-2 building.
        include_exceptions : append HAS_EXCEPTION children of the surviving
            base clauses (order preserved, exceptions appended at the end).
        """
        base = [a for a in self._clauses_by_element.get(element_type, [])
                if _occupancy_applies(self._clause_occupancies.get(a, []),
                                      occupancy)]
        if not include_exceptions:
            return base
        expanded = self.expand_with_exceptions(base)
        return [a for a in expanded
                if a in base or _occupancy_applies(
                    self._clause_occupancies.get(a, []), occupancy)]

    def clauses_for_room(self, room_node_attrs: Dict[str, Any]) -> List[str]:
        """
        Convenience entry point for SpatialGraph node attribute dicts.

        Accepts either the SpatialGraph 'category' string ("room_kitchen")
        or a plain 'type' ("kitchen" or "room_kitchen"). An optional
        'occupancy_override' key narrows the occupancy filter.
        """
        raw = room_node_attrs.get("type") or room_node_attrs.get("category")
        if not raw:
            return []
        element = SPATIAL_CATEGORY_TO_ELEMENT.get(raw, raw)
        if element not in CANONICAL_ELEMENTS:
            logger.warning("clauses_for_room: unknown room type %r", raw)
            return []
        return self.clauses_for_element(
            element, occupancy=room_node_attrs.get("occupancy_override"))

    def expand_with_exceptions(self, article_ids: List[str]) -> List[str]:
        """
        Return article_ids PLUS the HAS_EXCEPTION children of each — order
        preserved, new exceptions appended at the end, de-duplicated.
        """
        out: List[str] = []
        seen: set = set()
        for a in article_ids:
            if a not in seen:
                seen.add(a)
                out.append(a)
        for a in list(out):                   # children of the original set
            for exc in self._exceptions_of.get(a, []):
                if exc not in seen:
                    seen.add(exc)
                    out.append(exc)
        return out

    def explain_link(self, article_id: str, element_type: str) -> Dict[str, Any]:
        """
        Describe the regulation-graph path from element:{element_type} to
        clause:{article_id}, for thesis figures and debugging.

        Returns a dict:
            {"found": bool, "element": ..., "clause": ...,
             "path_kind": "direct" | "via_exception" | None,
             "edges": [{"from", "to", "edge_type", **edge_attrs}, ...]}
        """
        element_node = f"element:{element_type}"
        clause_node = f"clause:{article_id}"
        result: Dict[str, Any] = {"found": False, "element": element_type,
                                  "clause": article_id, "path_kind": None,
                                  "edges": []}
        if element_node not in self.G or clause_node not in self.G:
            return result

        def _edge_dicts(u: str, v: str, allowed: tuple) -> List[Dict[str, Any]]:
            found = []
            if self.G.has_edge(u, v):
                for data in self.G[u][v].values():
                    if data.get("edge_type") in allowed:
                        found.append({"from": u, "to": v, **data})
            return found

        direct = _edge_dicts(clause_node, element_node,
                             self.GOVERNING_EDGE_TYPES)
        if direct:
            result.update(found=True, path_kind="direct", edges=[direct[0]])
            return result

        # exception path: element <-GOVERNS- base -HAS_EXCEPTION-> clause
        for base_node in self.G.predecessors(clause_node):
            hop2 = _edge_dicts(base_node, clause_node, ("HAS_EXCEPTION",))
            if not hop2:
                continue
            hop1 = _edge_dicts(base_node, element_node,
                               self.GOVERNING_EDGE_TYPES)
            if hop1:
                result.update(found=True, path_kind="via_exception",
                              edges=[hop1[0], hop2[0]])
                return result
        return result
