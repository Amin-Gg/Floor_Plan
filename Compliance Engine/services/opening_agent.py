"""
services/opening_agent.py
=========================
Step 6 — Opening Agent  (windows, natural light, glazing ratios)

Checks Mabhas clauses about windows and natural light using the SpatialGraph
from Step 3. Emits the same Finding objects as Steps 5 and 9, so all findings
are uniform across the pipeline.

What it checks deterministically
--------------------------------
1. GLAZING RATIO rules — the cluster of clauses requiring window area to be at
   least some fraction of floor area (e.g. "window_area ratio_to_floor_area
   >= 0.125"). The SpatialGraph already computes glazing_ratio(room) from the
   exterior windows assigned to each room, so these are directly checkable.

2. NATURAL LIGHT PRESENCE — clauses requiring a habitable room to have at least
   one exterior window. Checkable: does the room have any exterior window?

The genuinely hard problem (acknowledged from the start)
--------------------------------------------------------
Whether a window faces an *adequately large open space* vs a narrow light well
CANNOT be determined from a 2D plan — we don't know what's outside the building
boundary. Every clause with a condition like "adjacent_to open_space",
"light well", "courtyard width", "distance between opposite window walls" is
therefore flagged NEEDS_REVIEW with a clear explanation, so a human confirms
the site condition. This is by design, not a gap.

Editing after model training (top of file)
-------------------------------------------
1. GLAZING_RATIO_OBJECTS — which clause `object` phrases mean "glazing ratio".
2. LIGHT_REQUIRED_CATEGORIES — room categories that must have natural light.
3. SITE_DEPENDENT_KEYWORDS — phrases that force NEEDS_REVIEW (site conditions
   we can't see from a plan).
All bim_data access is via SpatialGraph — no field names in this file.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from numeric_checker import Finding, Verdict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — the only things you normally edit
# ═══════════════════════════════════════════════════════════════════════════

# Clause `object`/`property` phrases that denote a glazing-to-floor ratio.
# When a numeric clause's object/property contains one of these AND the unit is
# "ratio", the agent checks it with SpatialGraph.glazing_ratio().
GLAZING_RATIO_KEYWORDS = (
    "ratio_to_floor_area",
    "area ratio",
    "area_ratio",
    "window_area",
    "window",
    "glazing",
    "glass",
    "transparent_glazing",
    "skylight",
    "daylight",
    "light well",
    "light_well",
    "lighting",
)

# Room categories that must receive natural light (have ≥1 exterior window).
# Edit to match YOUR model's category strings.
LIGHT_REQUIRED_CATEGORIES = {
    "room_bedroom",
    "room_living",
    "room_kitchen",
}

# If a clause's condition or object mentions any of these, the rule depends on
# what is OUTSIDE the building (open space, light well geometry, distance to
# the opposite building) — not knowable from a single floor plan → NEEDS_REVIEW.
SITE_DEPENDENT_KEYWORDS = (
    "open_space", "open space", "light well", "light_well", "courtyard",
    "passage", "opposite window", "opposite_window", "distance_between",
    "setback", "patio", "sky_view", "sky view", "adjacent_to",
)


# ═══════════════════════════════════════════════════════════════════════════
# The agent
# ═══════════════════════════════════════════════════════════════════════════

class OpeningAgent:
    """
    Checks window / natural-light clauses against a SpatialGraph.

    Parameters
    ----------
    spatial_graph : SpatialGraph (Step 3)
    retriever : optional MabhasRetriever (Step 2) — not required.
    """

    def __init__(self, spatial_graph: Any, retriever: Optional[Any] = None):
        self.sg = spatial_graph
        self.retriever = retriever

    # ── public API ────────────────────────────────────────────────────────────

    def check_all(self, clauses: List[Dict[str, Any]]) -> List[Finding]:
        findings: List[Finding] = []
        for clause in clauses:
            f = self.check_clause(clause)
            if f:
                findings.extend(f)
        return findings

    def check_clause(self, clause: Dict[str, Any]) -> List[Finding]:
        """
        Returns findings ONLY for clauses this agent recognises as window/light
        rules. Returns [] for clauses outside its domain (so it can be run over
        the whole corpus without claiming rules it shouldn't).
        """
        ents = clause.get("entities")
        if ents is None:
            return []
        if isinstance(ents, dict):
            ents = [ents]

        out: List[Finding] = []
        for ent in ents:
            f = self._check_entity(clause, ent)
            if f:
                out.append(f)
        return out

    # ── per-entity ─────────────────────────────────────────────────────────────

    def _check_entity(self, clause: Dict[str, Any],
                      ent: Dict[str, Any]) -> Optional[Finding]:
        obj   = _as_text(ent.get("object")).lower()
        prop  = _as_text(ent.get("property")).lower()
        unit  = _as_text(ent.get("unit")).lower()
        cond  = _as_text(ent.get("condition"))
        blob  = f"{obj} {prop} {cond}".lower()

        is_glazing = any(k in obj or k in prop for k in GLAZING_RATIO_KEYWORDS)

        # Not a window/light rule we recognise → let another agent handle it.
        if not is_glazing:
            return None

        # Site-dependent → cannot verify from a 2D plan → NEEDS_REVIEW.
        if any(k in blob for k in SITE_DEPENDENT_KEYWORDS):
            return self._review(clause,
                "Window faces external open space / light well — site condition "
                "cannot be verified from a 2D plan; confirm on site", object=obj)

        # Glazing ratio rule with a numeric threshold → check it.
        comp  = ent.get("comparator")
        value = ent.get("value")
        if unit == "ratio" and comp in (">=", "<=", ">", "<") and value is not None:
            return self._check_glazing_ratio(clause, ent, comp, float(value))

        # Recognised as glazing but not in a checkable numeric form.
        return self._review(clause,
            "Glazing rule not in a directly checkable ratio form — needs review",
            object=obj)

    def _check_glazing_ratio(self, clause, ent, comp, required) -> Finding:
        """Check glazing-to-floor ratio for every light-required room."""
        rooms_checked = []
        for cat in LIGHT_REQUIRED_CATEGORIES:
            rooms_checked.extend(self.sg.get_rooms_by_category(cat))

        if not rooms_checked:
            return self._review(clause,
                "No habitable rooms in plan to check glazing ratio",
                object=ent.get("object"))

        worst = None  # track the worst (failing) room for the message
        all_pass = True
        for rid in rooms_checked:
            ratio = self.sg.glazing_ratio(rid)
            ok = self._compare(ratio, comp, required)
            if not ok:
                all_pass = False
                if worst is None or ratio < worst[1]:
                    worst = (rid, ratio)

        if all_pass:
            return Finding(
                article_id=clause.get("article_id", "?"),
                verdict=Verdict.PASS,
                message=(f"All habitable rooms meet glazing ratio "
                         f"{comp} {required} — compliant"),
                object=ent.get("object"), required=required, unit="ratio",
                rule_text_en=clause.get("text_en"))

        rid, ratio = worst
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.FAIL,
            message=(f"{rid}: glazing ratio {round(ratio,3)} fails "
                     f"requirement {comp} {required}"),
            object=ent.get("object"), measured=round(ratio, 3),
            required=required, unit="ratio", element_id=rid,
            rule_text_en=clause.get("text_en"))

    # ── natural light presence (called separately, see check_light_presence) ──

    def check_light_presence(self) -> List[Finding]:
        """
        Standalone check (not clause-driven): every habitable room should have
        at least one exterior window. Returns one finding per room lacking light.
        Call this once per plan in addition to check_all().
        """
        out: List[Finding] = []
        for cat in LIGHT_REQUIRED_CATEGORIES:
            for rid in self.sg.get_rooms_by_category(cat):
                ext_windows = self.sg.get_exterior_windows(rid)
                if not ext_windows:
                    out.append(Finding(
                        article_id="natural_light_presence",
                        verdict=Verdict.FAIL,
                        message=f"{rid} ({cat}) has no exterior window — lacks natural light",
                        object="natural_light", element_id=rid))
        return out

    # ── helpers ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _compare(measured, comp, required) -> bool:
        if comp == ">=": return measured >= required
        if comp == "<=": return measured <= required
        if comp == ">":  return measured >  required
        if comp == "<":  return measured <  required
        return False

    @staticmethod
    def _review(clause: Dict[str, Any], msg: str,
                object: Optional[str] = None) -> Finding:
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NEEDS_REVIEW,
            message=msg, object=object,
            rule_text_en=clause.get("text_en"))


# ── module helper ─────────────────────────────────────────────────────────────

def _as_text(value: Any) -> str:
    """
    Coerce an entity field to a string safely. Some clauses store a LIST as the
    `object` (e.g. ['moving_objects', 'intense_sunlight', ...]). Join lists with
    spaces; stringify dicts; None → ''. Prevents '.lower()' crashes on real data.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_as_text(v) for v in value.values())
    return str(value)
