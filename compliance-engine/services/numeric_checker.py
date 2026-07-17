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
from typing import Any, Dict, List, Optional, Union

from domain.findings import Finding, Verdict
from domain.units import area_to_m2, length_to_mm, normalise_unit_name
from standards.catalog_api import clause_property_semantics, supported_units

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

# Verdict is the shared domain contract (re-exported for compatibility).


# ── BUILDING PARAMETERS (user-supplied, not measured from the plan) ──────────
# Some Mabhas values cannot be read off a 2D plan — the clearest example is the
# room CLEAR/CEILING HEIGHT. Rather than always deferring those to human review,
# the operator supplies them once as building parameters (e.g. from the web UI),
# the checker uses them to produce a real PASS/FAIL, and every such verdict is
# tagged in its message as coming from a user parameter (so the report stays
# honest about provenance — the number was asserted, not measured).
#
# Add a new tunable here + read it in BimAdapter + route a property to it in
# _resolve_measure. These are the ONLY things you touch to add a knob.
BUILDING_PARAM_DEFAULTS: Dict[str, float] = {
    "ceiling_height_mm": 2800.0,   # clear floor-to-ceiling height (room "height")
}


# ── OBJECT_MAP ──────────────────────────────────────────────────────────────
# Maps a Mabhas clause `object` phrase → WHICH element (and, for rooms, which
# canonical category) the rule is about. It does NOT decide which property to
# measure — that comes from the clause's own `property` field (see
# _quantity_of + _resolve_measure below). This split fixes the class of bug
# where two clauses sharing an `object` but differing in `property`
# (door "clear width" vs "clear height"; dwelling "area" vs "width") were
# measured identically.
#
# Each entry: object_phrase -> (element_kind, room_category_or_None)
#   element_kind is one of: "room", "door", "window".
#   room_category is the canonical room_* string for "room" objects, else None.
#
# Canonical room_* vocabulary (must match ingest/category_normalizer.py):
#   room_bedroom room_living room_kitchen room_bathroom room_toilet
#   room_entry room_storage
#
# To support a new rule object, add one line here. To adapt to renamed
# categories after training, edit the right-hand side here only.
# Anything NOT in this map → the rule is flagged NEEDS_REVIEW (never guessed).
OBJECT_MAP: Dict[str, tuple] = {
    # --- rooms: living / habitable ---
    "dwelling_space":      ("room", "room_bedroom"),
    "dwelling space":      ("room", "room_bedroom"),
    "habitable_space":     ("room", "room_bedroom"),
    "habitable space":     ("room", "room_bedroom"),
    "bedroom":             ("room", "room_bedroom"),
    "room":                ("room", "room_bedroom"),
    "living room":         ("room", "room_living"),
    "living_room":         ("room", "room_living"),
    "living space":        ("room", "room_living"),
    # --- rooms: kitchen ---
    "kitchen":             ("room", "room_kitchen"),
    "wall kitchen":        ("room", "room_kitchen"),
    "kitchen space":       ("room", "room_kitchen"),
    # --- rooms: sanitary ---
    "sanitary_space":      ("room", "room_bathroom"),
    "sanitary space":      ("room", "room_bathroom"),
    "bathroom":            ("room", "room_bathroom"),
    "washroom":            ("room", "room_bathroom"),
    "toilet":              ("room", "room_toilet"),
    "water closet":        ("room", "room_toilet"),
    "wc":                  ("room", "room_toilet"),
    # --- rooms: circulation / service ---
    "entrance space":      ("room", "room_entry"),
    "entrance_space":      ("room", "room_entry"),
    "entrance":            ("room", "room_entry"),
    "entry":               ("room", "room_entry"),
    "foyer":               ("room", "room_entry"),
    "lobby":               ("room", "room_entry"),
    "storage":             ("room", "room_storage"),
    "storage_space":       ("room", "room_storage"),
    "storeroom":           ("room", "room_storage"),
    "pantry":              ("room", "room_storage"),
    # --- doors ---
    "door_width":          ("door", None),
    "door":                ("door", None),
    "main door":           ("door", None),
    "main_door":           ("door", None),
    "entrance door":       ("door", None),
    "room door":           ("door", None),
    # --- windows / openings (dimensional checks; ratio/site rules stay with
    #     the opening agent, which yields length-unit window clauses to here) ---
    "window":              ("window", None),
    "sauna window":        ("window", None),
    "emergency opening":   ("window", None),
    "ventilation_opening": ("window", None),
    "ventilation opening": ("window", None),
    "skylight":            ("window", None),
    # --- (extend here as you validate more clauses against real plans) ---
}


# ── PROPERTY → canonical quantity ───────────────────────────────────────────
# The clause `property` phrase ("area", "clear width", "ceiling height", "sill
# height", …) is reduced to one canonical quantity. Order matters: "floor area"
# must match `area` before the `width`/`length` substrings. Unknown phrasing →
# None → NEEDS_REVIEW (never measured as the wrong thing).
def _quantity_of(prop: str) -> Optional[str]:
    semantics = clause_property_semantics(prop)
    return str(semantics["key"]) if semantics is not None else None


# (element_kind, canonical_quantity) → concrete measure_kind for bim_data.
#   "room"   + area → area_m2 ; + width → shorter bbox side ; + length → longer ;
#            + height → CLEAR HEIGHT from the ceiling_height_mm building parameter
#                       (user-supplied; verdict is tagged as parameter-sourced).
#   "door"   + width → opening width ; + height → opening height.
#   "window" + width → window width ; + height → window height ; + sill → sill.
# A pairing absent here (e.g. door + area) → None → NEEDS_REVIEW.
def _resolve_measure(element_kind: str, quantity: Optional[str]) -> Optional[str]:
    if quantity is None:
        return None
    table = {
        ("room",   "area"):   "room_area",
        ("room",   "width"):  "room_dim_min",
        ("room",   "length"): "room_dim_max",
        ("room",   "height"): "room_height",
        ("door",   "width"):  "door_width",
        ("door",   "height"): "door_height",
        ("window", "width"):  "window_width",
        ("window", "height"): "window_height",
        ("window", "sill"):   "window_sill",
    }
    return table.get((element_kind, quantity))


# ── UNIT NORMALISATION ──────────────────────────────────────────────────────
# Supported unit vocabulary and property dimensions come from the Phase-5
# semantic catalog. Generic arithmetic conversion remains in domain.units.
_LENGTH_UNITS = {normalise_unit_name(v) for v in supported_units("length")}
_AREA_UNITS = {normalise_unit_name(v) for v in supported_units("area")}


# Finding is the shared domain contract imported above.

# ═══════════════════════════════════════════════════════════════════════════
# bim_data adapter — the ONLY place that reads bim_data field names
# ═══════════════════════════════════════════════════════════════════════════

class BimAdapter:
    """
    Wraps bim_data so the checker never touches raw field names directly.
    If your trained model renames a field, edit ONLY the methods here.
    """

    def __init__(self, bim_data: Dict[str, Any],
                 building_params: Optional[Dict[str, Any]] = None):
        self._bim = bim_data
        # Merge: explicit building_params arg > bim_data["building_params"] >
        # defaults. These supply values that cannot be measured from a 2D plan
        # (currently the clear ceiling height). One place to read a knob.
        _block = dict(bim_data.get("building_params") or {})
        # "_provided" (written by Step 1 / the IFC ingest) lists exactly which
        # keys the OPERATOR asserted; every other value in the block is a
        # recorded default. Legacy flat dicts without "_provided" keep the old
        # semantics: any key present counts as supplied.
        _provided = _block.pop("_provided", None)
        _explicit = {k: v for k, v in (building_params or {}).items()
                     if k != "_provided" and v is not None}
        self.params: Dict[str, Any] = dict(BUILDING_PARAM_DEFAULTS)
        self.params.update(_block)
        self.params.update(_explicit)
        # Contract alias: Step 1 calls the FFL→underside-of-slab dimension
        # "wall_height" (the engine API's canonical spelling is
        # "wall_height_mm"); for room clear-height checks that IS the
        # engine's ceiling_height_mm. An explicit ceiling_height_mm wins.
        _ceiling_explicit = ("ceiling_height_mm" in _block
                             or "ceiling_height_mm" in _explicit)
        _wall_val = self.params.get("wall_height_mm",
                                    self.params.get("wall_height"))
        if not _ceiling_explicit and _wall_val is not None:
            self.params["ceiling_height_mm"] = float(_wall_val)
        # Provenance: which keys the OPERATOR actually asserted (via the API
        # or the IFC contract Pset) vs which are engine fallback defaults.
        # A compliance report must never claim a default was a user input.
        self.user_supplied_params = (
            set(_provided) if _provided is not None else set(_block)
        ) | set(_explicit)
        if ("ceiling_height_mm" not in self.user_supplied_params
                and self.user_supplied_params
                & {"wall_height", "wall_height_mm"}):
            self.user_supplied_params.add("ceiling_height_mm")

    def param_is_user_supplied(self, key: str) -> bool:
        """True if the operator asserted this parameter; False = engine default."""
        return key in self.user_supplied_params

    def ceiling_height_m(self) -> float:
        """User-supplied clear ceiling height (metres). Not measured — asserted."""
        return float(self.params.get("ceiling_height_mm",
                                     BUILDING_PARAM_DEFAULTS["ceiling_height_mm"])) / 1000.0

    def rooms_of_category(self, category: str) -> List[Dict[str, Any]]:
        return [r for r in self._bim.get("rooms", [])
                if r.get("category") == category]

    def room_area_m2(self, room: Dict[str, Any]) -> Optional[float]:
        v = room.get("area_m2")
        return float(v) if v is not None else None

    def room_height_m(self, room: Dict[str, Any]) -> Optional[float]:
        # Per-room ceiling height is not extracted from a 2D plan; use the
        # user-supplied building parameter (same for every room). Verdicts that
        # use this are tagged as parameter-sourced in their message.
        return self.ceiling_height_m()

    # Review fix C3 (2026-07): dim_min/dim_max are ORDER-INDEPENDENT reads.
    # The ingest now guarantees width<=length, but bim_data also arrives
    # hand-built through POST /analyze, where nothing enforces the labels.
    # A "min width" rule must measure the SHORTER side no matter which key
    # it was stored under — trusting the label produced a verified false
    # PASS. When only one dimension is present we cannot know whether it is
    # the shorter or longer side, so both reads return None (→ NEEDS_REVIEW,
    # never a guess), matching design principle 1.
    def _room_dims_mm(self, room: Dict[str, Any]) -> tuple:
        dims = room.get("dimensions", {}) or {}
        vals = [float(v) for v in (dims.get("width_mm"), dims.get("length_mm"))
                if v is not None]
        if len(vals) != 2:
            return (None, None)
        return (min(vals), max(vals))

    def room_dim_min_m(self, room: Dict[str, Any]) -> Optional[float]:
        w, _ = self._room_dims_mm(room)
        return w / 1000.0 if w is not None else None

    def room_dim_max_m(self, room: Dict[str, Any]) -> Optional[float]:
        _, l = self._room_dims_mm(room)
        return l / 1000.0 if l is not None else None

    def doors(self) -> List[Dict[str, Any]]:
        return self._bim.get("doors", [])

    def door_width_m(self, door: Dict[str, Any]) -> Optional[float]:
        v = door.get("width")
        return float(v) / 1000.0 if v is not None else None  # bim widths are mm

    def door_height_m(self, door: Dict[str, Any]) -> Optional[float]:
        v = door.get("height")
        return float(v) / 1000.0 if v is not None else None  # bim heights are mm

    def door_id(self, door: Dict[str, Any]) -> str:
        return door.get("id", "?")

    def windows(self) -> List[Dict[str, Any]]:
        return self._bim.get("windows", [])

    def window_width_m(self, win: Dict[str, Any]) -> Optional[float]:
        v = win.get("width")
        return float(v) / 1000.0 if v is not None else None   # bim widths are mm

    def window_height_m(self, win: Dict[str, Any]) -> Optional[float]:
        v = win.get("height")
        return float(v) / 1000.0 if v is not None else None   # bim heights are mm

    def window_sill_m(self, win: Dict[str, Any]) -> Optional[float]:
        v = win.get("sill_height")
        return float(v) / 1000.0 if v is not None else None   # bim sill is mm

    def window_id(self, win: Dict[str, Any]) -> str:
        return win.get("id", "?")

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

    def __init__(self, bim_data: Dict[str, Any],
                 building_params: Optional[Dict[str, Any]] = None):
        self.bim = BimAdapter(bim_data, building_params=building_params)

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
            return [self._review(clause, "No entities to check",
                                 unsupported=True)]
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
                f"Unsupported comparator '{comp}' — needs review", object=obj,
                unsupported=True)]

        # 3. Object must map to an element (and, for rooms, a category)
        mapping = OBJECT_MAP.get(obj)
        if mapping is None:
            return [self._review(clause,
                f"Object '{obj}' not mapped to a measurable value — needs review",
                unsupported=True,
                object=obj)]

        element_kind, category = mapping

        # 3b. The PROPERTY decides what to measure on that element. Same object
        #     + different property (door clear-width vs clear-height; dwelling
        #     area vs width) now resolve to different measurements instead of
        #     silently reusing one. Unknown property → NEEDS_REVIEW.
        quantity = _quantity_of(prop)
        measure_kind = _resolve_measure(element_kind, quantity)
        if measure_kind is None:
            return [self._review(clause,
                f"Property '{prop}' is not auto-measurable for '{obj}' "
                f"— needs review", object=obj)]

        # 4. Units must be understood for this property
        canonical_value = self._to_canonical(value, unit, prop, comp)
        if canonical_value is None:
            return [self._review(clause,
                f"Unit '{unit}' for property '{prop}' not auto-checkable — needs review",
                unsupported=True,
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
                return [self._blocked(clause,
                    f"No '{category}' rooms in plan to check — not evaluated "
                    f"(add/tag the room or confirm it is absent)",
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

        elif measure_kind == "door_height":
            for d in self.bim.doors():
                measured_items.append((self.bim.door_id(d), self.bim.door_height_m(d)))

        elif measure_kind == "window_width":
            for w in self.bim.windows():
                measured_items.append((self.bim.window_id(w), self.bim.window_width_m(w)))

        elif measure_kind == "window_height":
            for w in self.bim.windows():
                measured_items.append((self.bim.window_id(w), self.bim.window_height_m(w)))

        elif measure_kind == "window_sill":
            for w in self.bim.windows():
                measured_items.append((self.bim.window_id(w), self.bim.window_sill_m(w)))

        else:
            return [self._review(clause,
                f"Measure kind '{measure_kind}' not implemented — needs review",
                unsupported=True,
                object=obj)]

        if not measured_items:
            return [self._blocked(clause,
                f"Nothing measurable for '{obj}' in this plan — not evaluated",
                object=obj)]

        # Honest provenance: room "height" comes from a building parameter
        # (asserted, not measured from the plan). Policy decision (operator
        # sign-off, 2026-07): a defaulted parameter must NOT produce a
        # PASS/FAIL verdict — a compliance report resting on an unconfirmed
        # assumption is deceptive. Unasserted ceiling height → NEEDS_REVIEW
        # per room, with the instruction to assert the real value. When the
        # operator HAS asserted it, the verdict is real and tagged as such.
        src_note = ""
        if measure_kind == "room_height":
            _mm = int(self.bim.ceiling_height_m() * 1000)
            if self.bim.param_is_user_supplied("ceiling_height_mm"):
                src_note = (f"  [ceiling height = {_mm} mm, "
                            f"user building parameter — not measured]")
            else:
                return [self._blocked(clause,
                    f"{elem_id}: ceiling height not asserted — the plan "
                    f"cannot yield it and the engine default ({_mm} mm) is "
                    f"not used for a verdict. Supply building_params."
                    f"wall_height (mm) to assert the real floor-to-slab "
                    f"height and get a PASS/FAIL verdict — not evaluated",
                    object=obj, element_id=elem_id)
                    for elem_id, _ in measured_items]

        # Compare each measured element against the threshold
        for elem_id, measured in measured_items:
            if measured is None:
                out.append(self._blocked(clause,
                    f"Could not measure {prop} of {elem_id} — not evaluated "
                    f"(value missing from the model)",
                    object=obj, element_id=elem_id))
                continue
            passed = self._compare(measured, comp, required)
            verdict = Verdict.PASS if passed else Verdict.FAIL
            req_str = (f"{required[0]}–{required[1]}"
                       if comp == "range" else f"{comp} {required}")
            out.append(Finding(
                article_id=art, verdict=verdict,
                message=(f"{elem_id}: {prop} = {round(measured,3)} m "
                         f"(required {req_str} m) → {verdict.value}{src_note}"),
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
        normalized_unit = normalise_unit_name(unit)
        semantics = clause_property_semantics(prop)
        dimension = semantics.get("dimension") if semantics else None
        if dimension == "area" or normalized_unit in _AREA_UNITS:
            return area_to_m2(v, normalized_unit)
        if dimension == "length" or normalized_unit in _LENGTH_UNITS:
            millimetres = length_to_mm(v, normalized_unit)
            return None if millimetres is None else millimetres / 1000.0
        return None   # ratio / percent / count / lux / etc → not auto-checkable

    @staticmethod
    def _review(clause: Dict[str, Any], msg: str,
                object: Optional[str] = None,
                element_id: Optional[str] = None,
                unsupported: bool = False) -> Finding:
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NEEDS_REVIEW,
            message=msg, object=object, element_id=element_id,
            rule_text_en=clause.get("text_en"),
            unsupported=unsupported,
        )

    @staticmethod
    def _blocked(clause: Dict[str, Any], msg: str,
                 object: Optional[str] = None,
                 element_id: Optional[str] = None) -> Finding:
        """Data-absence finding: the engine HAS logic for this clause but the
        plan/model lacks the required data. Distinct from _review (judgment):
        the fix is 'supply the data', so the verdict is NOT_EVALUATED and the
        item is routed to the modeler, not the human review queue."""
        return Finding(
            article_id=clause.get("article_id", "?"),
            verdict=Verdict.NOT_EVALUATED,
            message=msg, object=object, element_id=element_id,
            rule_text_en=clause.get("text_en"),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: summarise a findings list
# ═══════════════════════════════════════════════════════════════════════════

def assign_finding_ordinals(findings: List[Finding]) -> List[Finding]:
    """Disambiguate finding_ids for findings sharing an identity basis within
    one run. MUST be called on the final merged list (order is deterministic:
    agents iterate clauses and elements in stable order), otherwise duplicate
    BCF topic GUIDs silently drop topics in BCF readers."""
    seen: Dict[str, int] = {}
    for f in findings:
        b = f._id_basis
        f.ordinal = seen.get(b, 0)
        seen[b] = f.ordinal + 1
    return findings


def summarise(findings: List[Finding]) -> Dict[str, int]:
    out = {k: 0 for k in ("PASS", "FAIL", "NEEDS_REVIEW", "NOT_EVALUATED")}
    for f in findings:
        out[f.verdict.value] = out.get(f.verdict.value, 0) + 1
    return out