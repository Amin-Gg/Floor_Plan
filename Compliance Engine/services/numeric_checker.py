"""
services/numeric_checker.py
===========================
Step 9 — Numeric Rule Checker  (deterministic, no LLM)

Checks the numeric Mabhas clauses (rule_type == "numeric") against measurable
values pulled from bim_data. Produces PASS / FAIL / NEEDS_REVIEW findings.

Design principles (so this stays easy to edit after model training)
-------------------------------------------------------------------
1. CONSERVATIVE BY DEFAULT. A rule only gets an automatic PASS/FAIL when its
   `object` confidently maps to a value we can measure from bim_data AND the
   units are understood. Anything ambiguous → NEEDS_REVIEW, never a guess.
   This is the correct behaviour for a compliance tool: a wrong PASS is far
   worse than a human review.

2. SINGLE PLACE TO EDIT MAPPINGS.  The OBJECT_MAP dict (below) is the only
   thing you touch when:
     - your trained model uses slightly different room categories
     - you want to support a new measurable property
     - you discover a clause's `object` phrasing that should map to a value
   Add a line to OBJECT_MAP; nothing else changes.

3. SINGLE PLACE TO EDIT bim_data FIELD NAMES.  All reads of bim_data go through
   the `BimAdapter` class. If your real model output names a field differently
   (e.g. "area_sqm" instead of "area_m2"), you change ONE method there.

4. UNITS ARE NORMALISED ONCE.  All lengths → metres, all areas → m². The
   normalisation table is in _to_canonical(). Mabhas mixes mm/cm/m, so this
   prevents the classic "0.9 m door flagged as failing a 900 mm rule" bug.

5. LIST-FORM ENTITIES SUPPORTED.  10 clauses bundle several thresholds in a
   list. check_clause() handles both a single entity dict and a list of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

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

class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


# ── OBJECT_MAP ──────────────────────────────────────────────────────────────
# Maps a Mabhas clause `object` phrase → how to measure it from bim_data.
#
# Each entry: object_phrase -> (measure_kind, room_category_or_element)
#   measure_kind is one of:
#     "room_area"      → area_m2 of every room of the given category
#     "room_height"    → ceiling height of every room of the given category
#     "room_dim_min"   → shorter bounding-box side (width) of the room
#     "room_dim_max"   → longer bounding-box side (length) of the room
#     "door_width"     → width of every door
#     "window_dim"     → width/height of every window
#
# room categories use YOUR model's category strings (room_bedroom, etc.).
# To support a new rule object, add one line here. To adapt to renamed
# categories after training, edit the right-hand side here only.
#
# Anything NOT in this map → the rule is flagged NEEDS_REVIEW (never guessed).
OBJECT_MAP: Dict[str, tuple] = {
    # --- areas ---
    "kitchen":          ("room_area",   "room_kitchen"),
    "dwelling_space":   ("room_area",   "room_bedroom"),
    "dwelling space":   ("room_area",   "room_bedroom"),
    "sanitary_space":   ("room_area",   "room_bathroom"),
    "sanitary space":   ("room_area",   "room_bathroom"),
    "bedroom":          ("room_area",   "room_bedroom"),
    "living room":      ("room_area",   "room_living"),
    # --- doors ---
    "door_width":       ("door_width",  None),
    "main door":        ("door_width",  None),
    # --- (extend here as you validate more clauses against real plans) ---
}


# ── UNIT NORMALISATION ──────────────────────────────────────────────────────
# Everything is converted to canonical units before comparison:
#   lengths → metres, areas → m².  Edit here if a new unit appears.
_LENGTH_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}
_AREA_TO_M2  = {"mm2": 1e-6, "cm2": 1e-4, "m2": 1.0}

# Units we understand. Anything else (ratio, percent, count, lux, dB, …) is NOT
# auto-checkable here → NEEDS_REVIEW. (Ratios like glazing are the Opening
# agent's job; percent slopes need geometry we don't extract; etc.)
_LENGTH_UNITS = set(_LENGTH_TO_M)
_AREA_UNITS   = set(_AREA_TO_M2)


# ═══════════════════════════════════════════════════════════════════════════
# Finding data structure
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    article_id:   str
    verdict:      Verdict
    message:      str
    object:       Optional[str] = None
    measured:     Optional[float] = None
    required:     Optional[Any] = None
    unit:         Optional[str] = None
    element_id:   Optional[str] = None   # which room/door/window, if applicable
    rule_text_en: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "article_id":  self.article_id,
            "verdict":     self.verdict.value,
            "message":     self.message,
            "object":      self.object,
            "measured":    self.measured,
            "required":    self.required,
            "unit":        self.unit,
            "element_id":  self.element_id,
            "rule_text_en": self.rule_text_en,
        }


# ═══════════════════════════════════════════════════════════════════════════
# bim_data adapter — the ONLY place that reads bim_data field names
# ═══════════════════════════════════════════════════════════════════════════

class BimAdapter:
    """
    Wraps bim_data so the checker never touches raw field names directly.
    If your trained model renames a field, edit ONLY the methods here.
    """

    def __init__(self, bim_data: Dict[str, Any], wall_height_mm: float = 2800.0):
        self._bim = bim_data
        # Ceiling height isn't per-room in bim_data; walls carry a height.
        # Use the default wall height as the room clear height unless a better
        # source appears after training. Editable in one place.
        self._default_height_m = wall_height_mm / 1000.0

    def rooms_of_category(self, category: str) -> List[Dict[str, Any]]:
        return [r for r in self._bim.get("rooms", [])
                if r.get("category") == category]

    def room_area_m2(self, room: Dict[str, Any]) -> Optional[float]:
        v = room.get("area_m2")
        return float(v) if v is not None else None

    def room_height_m(self, room: Dict[str, Any]) -> Optional[float]:
        # No per-room height in current bim_data → use default wall height.
        # When the model provides real heights, read them here.
        return self._default_height_m

    def room_dim_min_m(self, room: Dict[str, Any]) -> Optional[float]:
        dims = room.get("dimensions", {})
        w = dims.get("width_mm")
        return float(w) / 1000.0 if w is not None else None

    def room_dim_max_m(self, room: Dict[str, Any]) -> Optional[float]:
        dims = room.get("dimensions", {})
        l = dims.get("length_mm")
        return float(l) / 1000.0 if l is not None else None

    def doors(self) -> List[Dict[str, Any]]:
        return self._bim.get("doors", [])

    def door_width_m(self, door: Dict[str, Any]) -> Optional[float]:
        v = door.get("width")
        return float(v) / 1000.0 if v is not None else None  # bim widths are mm

    def door_id(self, door: Dict[str, Any]) -> str:
        return door.get("id", "?")

    def room_id(self, room: Dict[str, Any]) -> str:
        return room.get("id", "?")


# ═══════════════════════════════════════════════════════════════════════════
# The checker
# ═══════════════════════════════════════════════════════════════════════════

class NumericChecker:
    """
    Runs all numeric clauses against bim_data.

    Usage:
        checker  = NumericChecker(bim_data)
        findings = checker.check_all(numeric_clauses)   # list[Finding]
    """

    def __init__(self, bim_data: Dict[str, Any], wall_height_mm: float = 2800.0):
        self.bim = BimAdapter(bim_data, wall_height_mm=wall_height_mm)

    # ── public API ────────────────────────────────────────────────────────────

    def check_all(self, clauses: List[Dict[str, Any]]) -> List[Finding]:
        findings: List[Finding] = []
        for clause in clauses:
            if clause.get("rule_type") != "numeric":
                continue
            findings.extend(self.check_clause(clause))
        return findings

    def check_clause(self, clause: Dict[str, Any]) -> List[Finding]:
        """Handle both single-dict and list-of-dict entity forms."""
        ents = clause.get("entities")
        if ents is None:
            return [self._review(clause, "No entities to check")]
        if isinstance(ents, dict):
            ents = [ents]
        out: List[Finding] = []
        for ent in ents:
            out.extend(self._check_entity(clause, ent))
        return out

    # ── per-entity checking ───────────────────────────────────────────────────

    def _check_entity(self, clause: Dict[str, Any], ent: Dict[str, Any]) -> List[Finding]:
        art   = clause.get("article_id", "?")
        text  = clause.get("text_en")
        obj   = _as_text(ent.get("object")).strip()
        prop  = _as_text(ent.get("property")).strip()
        comp  = ent.get("comparator")
        value = ent.get("value")
        unit  = _as_text(ent.get("unit")).strip()
        cond  = ent.get("condition")

        # 1. Conditional rules always need review — we can't verify the condition
        #    deterministically (e.g. "adjacent to open space").
        if cond:
            return [self._review(clause,
                f"Conditional rule (condition: {cond}) — needs human review",
                object=obj)]

        # 2. Comparator must be one we handle
        if comp not in (">=", "<=", ">", "<", "range"):
            return [self._review(clause,
                f"Unsupported comparator '{comp}' — needs review", object=obj)]

        # 3. Object must be mappable to a measurable value
        mapping = OBJECT_MAP.get(obj)
        if mapping is None:
            return [self._review(clause,
                f"Object '{obj}' not mapped to a measurable value — needs review",
                object=obj)]

        measure_kind, category = mapping

        # 4. Units must be understood for this property
        canonical_value = self._to_canonical(value, unit, prop, comp)
        if canonical_value is None:
            return [self._review(clause,
                f"Unit '{unit}' for property '{prop}' not auto-checkable — needs review",
                object=obj)]

        # 5. Measure from bim_data and compare
        return self._measure_and_compare(
            clause, ent, measure_kind, category,
            comp, canonical_value, unit, obj, prop, text)

    # ── measurement + comparison ───────────────────────────────────────────────

    def _measure_and_compare(self, clause, ent, measure_kind, category,
                             comp, required, unit, obj, prop, text) -> List[Finding]:
        art = clause.get("article_id", "?")
        out: List[Finding] = []

        # Gather (element_id, measured_value) pairs depending on measure kind
        measured_items: List[tuple] = []

        if measure_kind == "room_area":
            rooms = self.bim.rooms_of_category(category)
            if not rooms:
                return [self._review(clause,
                    f"No '{category}' rooms in plan to check — needs review",
                    object=obj)]
            for r in rooms:
                measured_items.append((self.bim.room_id(r), self.bim.room_area_m2(r)))

        elif measure_kind == "room_height":
            rooms = self.bim.rooms_of_category(category)
            for r in rooms:
                measured_items.append((self.bim.room_id(r), self.bim.room_height_m(r)))

        elif measure_kind == "room_dim_min":
            rooms = self.bim.rooms_of_category(category)
            for r in rooms:
                measured_items.append((self.bim.room_id(r), self.bim.room_dim_min_m(r)))

        elif measure_kind == "room_dim_max":
            rooms = self.bim.rooms_of_category(category)
            for r in rooms:
                measured_items.append((self.bim.room_id(r), self.bim.room_dim_max_m(r)))

        elif measure_kind == "door_width":
            for d in self.bim.doors():
                measured_items.append((self.bim.door_id(d), self.bim.door_width_m(d)))

        else:
            return [self._review(clause,
                f"Measure kind '{measure_kind}' not implemented — needs review",
                object=obj)]

        if not measured_items:
            return [self._review(clause,
                f"Nothing measurable for '{obj}' in this plan — needs review",
                object=obj)]

        # Compare each measured element against the threshold
        for elem_id, measured in measured_items:
            if measured is None:
                out.append(self._review(clause,
                    f"Could not measure {prop} of {elem_id} — needs review",
                    object=obj, element_id=elem_id))
                continue
            passed = self._compare(measured, comp, required)
            verdict = Verdict.PASS if passed else Verdict.FAIL
            req_str = (f"{required[0]}–{required[1]}"
                       if comp == "range" else f"{comp} {required}")
            out.append(Finding(
                article_id=art, verdict=verdict,
                message=(f"{elem_id}: {prop} = {round(measured,3)} m "
                         f"(required {req_str} m) → {verdict.value}"),
                object=obj, measured=round(measured, 3), required=required,
                unit="m", element_id=elem_id, rule_text_en=text,
            ))
        return out

    # ── helpers ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _compare(measured: float, comp: str, required: Any) -> bool:
        if comp == ">=":   return measured >= required
        if comp == "<=":   return measured <= required
        if comp == ">":    return measured >  required
        if comp == "<":    return measured <  required
        if comp == "range" and isinstance(required, (list, tuple)) and len(required) == 2:
            lo, hi = required
            return lo <= measured <= hi
        return False

    @staticmethod
    def _to_canonical(value: Any, unit: str, prop: str, comp: str) -> Optional[Any]:
        """
        Convert a threshold value to canonical units (metres / m²).
        Returns None when the unit isn't auto-checkable (→ NEEDS_REVIEW).
        Handles range values (list of two).
        """
        # range comes as [lo, hi]
        if comp == "range":
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                return None
            lo = NumericChecker._convert_scalar(value[0], unit, prop)
            hi = NumericChecker._convert_scalar(value[1], unit, prop)
            return None if lo is None or hi is None else [lo, hi]
        return NumericChecker._convert_scalar(value, unit, prop)

    @staticmethod
    def _convert_scalar(value: Any, unit: str, prop: str) -> Optional[float]:
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        # area properties expect area units; everything else expects length
        is_area = prop in ("area",) or unit in _AREA_UNITS
        if is_area:
            if unit in _AREA_TO_M2:
                return v * _AREA_TO_M2[unit]
            return None
        if unit in _LENGTH_TO_M:
            return v * _LENGTH_TO_M[unit]
        return None   # ratio / percent / count / lux / etc → not auto-checkable

    @staticmethod
    def _review(clause: Dict[str, Any], msg: str,
                object: Optional[str] = None,
                element_id: Optional[str] = None) -> Finding:
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NEEDS_REVIEW,
            message=msg, object=object, element_id=element_id,
            rule_text_en=clause.get("text_en"),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: summarise a findings list
# ═══════════════════════════════════════════════════════════════════════════

def summarise(findings: List[Finding]) -> Dict[str, int]:
    out = {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 0}
    for f in findings:
        out[f.verdict.value] += 1
    return out
