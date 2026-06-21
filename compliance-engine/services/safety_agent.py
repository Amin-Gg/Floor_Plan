"""
services/safety_agent.py
========================
Step 7 — Safety Agent  (egress, stairs, railings/guards)

Checks life-safety Mabhas clauses using the SpatialGraph from Step 3. Emits the
same Finding objects as Steps 5, 6, 9 so all findings stay uniform.

What it checks deterministically
--------------------------------
1. EGRESS REACHABILITY — every habitable room must be able to reach an exit
   (a room with an exterior door) through the door-graph. Uses
   SpatialGraph.can_reach_exit(). Rooms that cannot reach an exit → FAIL.

2. STAIR PRESENCE — when a clause requires a stair/egress stair, the plan must
   actually contain a stair element. Checkable from bim_data["stairs"].

3. RAILING / GUARD PRESENCE — balconies, terraces, and accessible window
   openings that require a guard must have a railing element nearby. The model
   detects "railing" (class 14); we check whether one exists for the balcony.

Everything else (rescue-station rules, fire signage, mechanical exhaust,
door-swing direction, "must comply with section X" cross-references) → flagged
NEEDS_REVIEW. The agent never guesses on life-safety rules — a wrong PASS here
is the most dangerous possible error, so the bar for an automatic verdict is
deliberately high.

Editing after model training (top of file)
-------------------------------------------
1. HABITABLE_CATEGORIES   — which rooms must have egress.
2. EGRESS_RELATIONS / STAIR_KEYWORDS / GUARD_KEYWORDS — phrase triggers.
3. GUARD_REQUIRED_CATEGORIES — spaces that need a railing.
All bim_data access for rooms/doors is via SpatialGraph; stairs/railings are
read through the small BimSafetyAdapter (one place to edit field names).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from numeric_checker import Finding, Verdict, _as_text

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — the only things you normally edit
# ═══════════════════════════════════════════════════════════════════════════

# Rooms that must be able to reach an exit.
HABITABLE_CATEGORIES = {
    "room_bedroom",
    "room_living",
    "room_kitchen",
}

# Spaces that, when present, must be protected by a railing/guard.
GUARD_REQUIRED_CATEGORIES = {
    "balcony",
    "room_balcony",
    "terrace",
}

# Relation/keyword triggers that route a clause to each check kind.
EGRESS_KEYWORDS = (
    "egress", "escape", "exit", "evacuat", "must_reach",
    "safe_egress", "emergency_route", "emergency route",
)
STAIR_KEYWORDS = (
    "stair", "staircase", "stairway", "stairwell", "roof_access",
)
GUARD_KEYWORDS = (
    "railing", "guard", "refuge", "fence", "balustrade",
    "must_have_refuge_or_guard", "protected_by",
)


# ═══════════════════════════════════════════════════════════════════════════
# bim_data adapter for stair/railing elements (rooms/doors come via SpatialGraph)
# ═══════════════════════════════════════════════════════════════════════════

class BimSafetyAdapter:
    """Reads stair and railing elements from bim_data. Edit field names here only."""

    def __init__(self, bim_data: Dict[str, Any]):
        self._bim = bim_data

    def has_stairs(self) -> bool:
        return len(self._bim.get("stairs", [])) > 0

    def stair_count(self) -> int:
        return len(self._bim.get("stairs", []))

    def railings(self) -> List[Dict[str, Any]]:
        # Railings may live in their own list, or be detected elements with a
        # category/class marker. Edit here to match your model's output.
        rails = self._bim.get("railings")
        if rails is not None:
            return rails
        # fallback: elements whose category mentions 'railing'
        return [e for e in self._bim.get("elements", [])
                if "railing" in _as_text(e.get("category")).lower()]

    def has_any_railing(self) -> bool:
        return len(self.railings()) > 0


# ═══════════════════════════════════════════════════════════════════════════
# The agent
# ═══════════════════════════════════════════════════════════════════════════

class SafetyAgent:
    """
    Checks egress, stair-presence, and guard/railing clauses.

    Parameters
    ----------
    spatial_graph : SpatialGraph (Step 3)
    bim_data      : the raw bim_data dict (for stair/railing elements)
    retriever     : optional MabhasRetriever (Step 2) — not required.
    """

    def __init__(self, spatial_graph: Any, bim_data: Dict[str, Any],
                 retriever: Optional[Any] = None):
        self.sg = spatial_graph
        self.bim = BimSafetyAdapter(bim_data)
        self.retriever = retriever

    # ── public API ────────────────────────────────────────────────────────────

    def check_all(self, clauses: List[Dict[str, Any]]) -> List[Finding]:
        """Claim only safety clauses; return [] for clauses outside the domain."""
        findings: List[Finding] = []
        for clause in clauses:
            findings.extend(self.check_clause(clause))
        return findings

    def check_clause(self, clause: Dict[str, Any]) -> List[Finding]:
        ents = clause.get("entities")
        if ents is None:
            return []
        if isinstance(ents, dict):
            ents = [ents]
        out: List[Finding] = []
        for ent in ents:
            f = self._route(clause, ent)
            if f:
                out.append(f)
        return out

    def check_egress_all_rooms(self) -> List[Finding]:
        """
        Standalone (not clause-driven): every habitable room must reach an exit.
        Call once per plan. Returns one finding per room that cannot.
        """
        out: List[Finding] = []
        any_exit = any(
            self.sg.graph.nodes[n].get("has_exterior_door")
            for n in self.sg.graph.nodes()
        )
        if not any_exit:
            return [Finding(
                article_id="egress_presence",
                verdict=Verdict.FAIL,
                message="Plan has no exterior door — no exit exists for any room",
                object="egress")]

        for cat in HABITABLE_CATEGORIES:
            for rid in self.sg.get_rooms_by_category(cat):
                if not self.sg.can_reach_exit(rid):
                    out.append(Finding(
                        article_id="egress_reachability",
                        verdict=Verdict.FAIL,
                        message=f"{rid} ({cat}) cannot reach any exit through the plan",
                        object="egress", element_id=rid))
        return out

    # ── routing ────────────────────────────────────────────────────────────────

    def _route(self, clause: Dict[str, Any], ent: Dict[str, Any]) -> Optional[Finding]:
        subj = _as_text(ent.get("subject")).lower()
        rel  = _as_text(ent.get("relation")).lower()
        obj  = _as_text(ent.get("object")).lower()
        blob = f"{subj} {rel} {obj}".lower()

        is_egress = any(k in blob for k in EGRESS_KEYWORDS)
        is_stair  = any(k in blob for k in STAIR_KEYWORDS)
        is_guard  = any(k in blob for k in GUARD_KEYWORDS)

        if not (is_egress or is_stair or is_guard):
            return None  # not a safety rule we recognise → another agent / review

        # Guard/railing has priority (most specific), then stair, then egress.
        if is_guard:
            return self._check_guard(clause)
        if is_stair:
            return self._check_stair_presence(clause)
        if is_egress:
            return self._check_egress_rule(clause)
        return None

    # ── checks ─────────────────────────────────────────────────────────────────

    def _check_egress_rule(self, clause: Dict[str, Any]) -> Finding:
        """
        A clause about egress routes. We can deterministically verify that
        habitable rooms can reach an exit; the more specific egress requirements
        (signage, swing direction, width along the path) are flagged for review.
        """
        text = _as_text(clause.get("text_en")).lower()
        # Only the reachability aspect is deterministic.
        failed = [rid for cat in HABITABLE_CATEGORIES
                  for rid in self.sg.get_rooms_by_category(cat)
                  if not self.sg.can_reach_exit(rid)]
        if failed:
            return Finding(
                article_id=clause.get("article_id", "?"),
                verdict=Verdict.FAIL,
                message=(f"Egress rule: {len(failed)} room(s) cannot reach an exit "
                         f"({', '.join(failed[:3])}{'…' if len(failed)>3 else ''})"),
                object="egress", rule_text_en=clause.get("text_en"))
        # Reachability OK, but the clause may demand more we can't verify.
        return self._review(clause,
            "Egress reachability OK; specific egress details (width, signage, "
            "swing direction) need human review", object="egress")

    def _check_stair_presence(self, clause: Dict[str, Any]) -> Finding:
        if self.bim.has_stairs():
            return Finding(
                article_id=clause.get("article_id", "?"),
                verdict=Verdict.PASS,
                message=f"Plan contains {self.bim.stair_count()} stair element(s) — present",
                object="stair", rule_text_en=clause.get("text_en"))
        # A stair clause but no stair in the plan. On a single-storey plan this
        # may be fine (no stair needed), so flag for review rather than fail.
        return self._review(clause,
            "Stair-related rule but no stair element detected — confirm whether "
            "a stair is required for this plan", object="stair")

    def _check_guard(self, clause: Dict[str, Any]) -> Finding:
        """Spaces requiring a guard (balcony/terrace) must have a railing."""
        guarded_rooms = [rid for cat in GUARD_REQUIRED_CATEGORIES
                         for rid in self.sg.get_rooms_by_category(cat)]
        # If the plan has no balcony/terrace, the rule doesn't apply here.
        if not guarded_rooms:
            return self._review(clause,
                "Guard/railing rule but no balcony/terrace in plan — not applicable "
                "or needs review", object="guard")
        if self.bim.has_any_railing():
            return Finding(
                article_id=clause.get("article_id", "?"),
                verdict=Verdict.PASS,
                message=("Balcony/terrace present and railing element(s) detected "
                         "— guard present"),
                object="guard", rule_text_en=clause.get("text_en"))
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.FAIL,
            message=("Balcony/terrace present but no railing element detected "
                     "— missing required guard"),
            object="guard", element_id=guarded_rooms[0],
            rule_text_en=clause.get("text_en"))

    # ── helper ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _review(clause: Dict[str, Any], msg: str,
                object: Optional[str] = None) -> Finding:
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NEEDS_REVIEW,
            message=msg, object=object,
            rule_text_en=clause.get("text_en"))
