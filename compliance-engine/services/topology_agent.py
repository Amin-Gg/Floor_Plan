"""
services/topology_agent.py
==========================
Step 5 — Topology Agent  (deterministic spatial checks + conservative review)

Checks the spatial Mabhas clauses (rule_type == "spatial") that concern how
rooms relate to each other — adjacency, connectivity, enclosure — using the
SpatialGraph built in Step 3. Produces PASS / FAIL / NEEDS_REVIEW findings in
the SAME Finding format as the numeric checker (Step 9), so the orchestrator
and report can treat all findings uniformly.

What this agent checks deterministically
-----------------------------------------
Only the spatial relations that can be resolved from the room-adjacency graph
without guessing:

  must_not_connect_to   → FAIL if subject room directly connects to object room
  must_connect_to       → FAIL if it does NOT connect
  must_have_access_to   → uses reachability (path exists through doors)
  must_reach            → same as must_have_access_to (reachability)

Every other relation (≈70 distinct ones, most appearing once) is flagged
NEEDS_REVIEW — orientation rules go to the Opening agent (Step 6), egress rules
to the Safety agent (Step 7), and genuinely interpretive rules go to the LLM in
the orchestrator (Step 8). This agent never guesses.

Editing after model training (all at the top of the file)
---------------------------------------------------------
1. RELATION_HANDLERS — which relations this agent handles, and how.
2. CATEGORY_SYNONYMS  — maps clause subject/object phrases to YOUR model's
   room category strings. This is the main thing you tune as you see which
   phrases the corpus uses ("sanitary space" → "room_bathroom", etc.).
3. Nothing else couples to bim_data — all spatial queries go through
   SpatialGraph's API, so renamed bim fields are handled in Step 3, not here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

# Reuse the exact Finding/Verdict types from Step 9 so all findings are uniform.
from numeric_checker import Finding, Verdict

logger = logging.getLogger(__name__)


def _as_text(value):
    """Coerce an entity field to string safely (lists/dicts/None → str)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_as_text(v) for v in value.values())
    return str(value)



# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — the only things you normally edit
# ═══════════════════════════════════════════════════════════════════════════

# ── CATEGORY_SYNONYMS ───────────────────────────────────────────────────────
# Map the free-text subject/object phrases that appear in Mabhas clauses to
# YOUR model's room category strings. Add entries as you see new phrasings.
# Matching is done on a normalised (lowercased) phrase; partial keyword match
# is applied via _phrase_to_category() below.
CATEGORY_SYNONYMS: Dict[str, str] = {
    "kitchen":         "room_kitchen",
    "bathroom":        "room_bathroom",
    "toilet":          "room_bathroom",
    "sanitary":        "room_bathroom",
    "wc":              "room_bathroom",
    "bedroom":         "room_bedroom",
    "dwelling":        "room_bedroom",
    "living":          "room_living",
    "salon":           "room_living",
    # extend as you validate more phrasings against real plans
}

# Relations this agent handles. Anything not listed → NEEDS_REVIEW.
# The handler name maps to a method on TopologyAgent (see _dispatch).
RELATION_HANDLERS = {
    "must_not_connect_to":  "_check_must_not_connect",
    "must_connect_to":      "_check_must_connect",
    "must_have_access_to":  "_check_must_have_access",
    "must_have_direct_access_to": "_check_must_connect",   # direct = adjacency
    "must_reach":           "_check_must_have_access",
}


# ═══════════════════════════════════════════════════════════════════════════
# The agent
# ═══════════════════════════════════════════════════════════════════════════

class TopologyAgent:
    """
    Checks spatial adjacency/connectivity clauses against a SpatialGraph.

    Parameters
    ----------
    spatial_graph : SpatialGraph
        The graph built in Step 3 (services/spatial_graph.py).
    retriever : optional
        A MabhasRetriever (Step 2). Not required for deterministic checks;
        reserved for future use when surfacing the exact clause text. The
        agent works fully without it.
    """

    def __init__(self, spatial_graph: Any, retriever: Optional[Any] = None):
        self.sg = spatial_graph
        self.retriever = retriever

    # ── public API ────────────────────────────────────────────────────────────

    def check_all(self, clauses: List[Dict[str, Any]]) -> List[Finding]:
        findings: List[Finding] = []
        for clause in clauses:
            if clause.get("rule_type") != "spatial":
                continue
            findings.extend(self.check_clause(clause))
        return findings

    def check_clause(self, clause: Dict[str, Any]) -> List[Finding]:
        ents = clause.get("entities")
        if ents is None:
            return [self._review(clause, "No entities to check")]
        if isinstance(ents, dict):
            ents = [ents]
        out: List[Finding] = []
        for ent in ents:
            out.append(self._check_entity(clause, ent))
        return out

    # ── dispatch ────────────────────────────────────────────────────────────────

    def _check_entity(self, clause: Dict[str, Any], ent: Dict[str, Any]) -> Finding:
        relation = _as_text(ent.get("relation")).strip()
        handler_name = RELATION_HANDLERS.get(relation)
        if handler_name is None:
            return self._review(clause,
                f"Relation '{relation}' not handled by topology agent — needs review",
                subject=ent.get("subject"))
        handler: Callable = getattr(self, handler_name)
        return handler(clause, ent)

    # ── relation handlers ─────────────────────────────────────────────────────

    def _check_must_not_connect(self, clause, ent) -> Finding:
        """subject must NOT have a direct door to object. FAIL if they connect."""
        subj_cat = self._phrase_to_category(ent.get("subject"))
        obj_cat  = self._phrase_to_category(ent.get("object"))
        if subj_cat is None or obj_cat is None:
            return self._review(clause,
                f"Could not map subject/object to room categories "
                f"(subject={ent.get('subject')!r}, object={ent.get('object')!r})",
                subject=ent.get("subject"))

        subj_rooms = self.sg.get_rooms_by_category(subj_cat)
        obj_rooms  = self.sg.get_rooms_by_category(obj_cat)
        if not subj_rooms or not obj_rooms:
            return self._review(clause,
                f"No {subj_cat} or {obj_cat} rooms in plan to check",
                subject=ent.get("subject"))

        violations = []
        for s in subj_rooms:
            for o in obj_rooms:
                if self.sg.are_directly_connected(s, o):
                    violations.append((s, o))

        if violations:
            s, o = violations[0]
            return Finding(
                article_id=clause.get("article_id", "?"),
                verdict=Verdict.FAIL,
                message=(f"{subj_cat} ({s}) directly connects to {obj_cat} ({o}) "
                         f"by a door — violates 'must not connect'"),
                object=ent.get("object"), element_id=f"{s}↔{o}",
                rule_text_en=clause.get("text_en"),
            )
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.PASS,
            message=f"No direct door between {subj_cat} and {obj_cat} — compliant",
            object=ent.get("object"), rule_text_en=clause.get("text_en"),
        )

    def _check_must_connect(self, clause, ent) -> Finding:
        """subject must have a direct door to object. FAIL if none."""
        subj_cat = self._phrase_to_category(ent.get("subject"))
        obj_cat  = self._phrase_to_category(ent.get("object"))
        if subj_cat is None or obj_cat is None:
            return self._review(clause,
                f"Could not map subject/object to room categories "
                f"(subject={ent.get('subject')!r}, object={ent.get('object')!r})",
                subject=ent.get("subject"))

        subj_rooms = self.sg.get_rooms_by_category(subj_cat)
        obj_rooms  = self.sg.get_rooms_by_category(obj_cat)
        if not subj_rooms or not obj_rooms:
            return self._review(clause,
                f"No {subj_cat} or {obj_cat} rooms in plan to check",
                subject=ent.get("subject"))

        for s in subj_rooms:
            if any(self.sg.are_directly_connected(s, o) for o in obj_rooms):
                return Finding(
                    article_id=clause.get("article_id", "?"),
                    verdict=Verdict.PASS,
                    message=f"{subj_cat} ({s}) connects directly to {obj_cat} — compliant",
                    object=ent.get("object"), element_id=s,
                    rule_text_en=clause.get("text_en"))
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.FAIL,
            message=f"No direct door between {subj_cat} and {obj_cat} — required connection missing",
            object=ent.get("object"), rule_text_en=clause.get("text_en"))

    def _check_must_have_access(self, clause, ent) -> Finding:
        """
        subject must be able to REACH object (path through doors).
        Uses graph reachability. If object is an exterior/exit concept,
        falls back to can_reach_exit.
        """
        subj_cat = self._phrase_to_category(ent.get("subject"))
        if subj_cat is None:
            return self._review(clause,
                f"Could not map subject {ent.get('subject')!r} to a room category",
                subject=ent.get("subject"))

        subj_rooms = self.sg.get_rooms_by_category(subj_cat)
        if not subj_rooms:
            return self._review(clause,
                f"No {subj_cat} rooms in plan to check", subject=ent.get("subject"))

        obj_cat = self._phrase_to_category(ent.get("object"))

        for s in subj_rooms:
            if obj_cat:
                # reachability to a specific room category
                targets = self.sg.get_rooms_by_category(obj_cat)
                reachable = any(self._has_path(s, t) for t in targets) if targets else False
            else:
                # generic access (e.g. to exit) → use can_reach_exit
                reachable = self.sg.can_reach_exit(s)
            if not reachable:
                return Finding(
                    article_id=clause.get("article_id", "?"),
                    verdict=Verdict.FAIL,
                    message=(f"{subj_cat} ({s}) cannot reach "
                             f"{obj_cat or 'an exit'} through the plan — access missing"),
                    object=ent.get("object"), element_id=s,
                    rule_text_en=clause.get("text_en"))
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.PASS,
            message=f"All {subj_cat} rooms can reach {obj_cat or 'an exit'} — compliant",
            object=ent.get("object"), rule_text_en=clause.get("text_en"))

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _has_path(self, a: str, b: str) -> bool:
        """True if there's a door-path from room a to room b."""
        if a == b:
            return True
        try:
            import networkx as nx
            return nx.has_path(self.sg.graph, a, b)
        except Exception:
            return False

    def _phrase_to_category(self, phrase: Optional[str]) -> Optional[str]:
        """
        Map a free-text subject/object phrase to a room category using
        CATEGORY_SYNONYMS via keyword matching. Returns None if no confident
        match (→ caller emits NEEDS_REVIEW). Conservative on purpose.
        """
        if not phrase:
            return None
        norm = re.sub(r"[^a-z\s]", " ", str(phrase).lower())
        words = set(norm.split())
        for keyword, category in CATEGORY_SYNONYMS.items():
            if keyword in words or keyword in norm:
                return category
        return None

    @staticmethod
    def _review(clause: Dict[str, Any], msg: str,
                subject: Optional[str] = None) -> Finding:
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NEEDS_REVIEW,
            message=msg, object=subject,
            rule_text_en=clause.get("text_en"),
        )
