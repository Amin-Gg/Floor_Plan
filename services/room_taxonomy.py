"""
services/room_taxonomy.py
=========================
Issue 2 — one canonical room taxonomy, used everywhere (Section-1 copy).

This is the SAME mapping the compliance engine uses (engine:
ingest/category_normalizer.py). Section 1 normalizes here so the bim_data it
emits — and therefore the exported IFC — already carries canonical room_*
categories. The engine re-running the normalizer on ingest is then a confirming
no-op. Keep the two ALIASES dicts in sync when you add vocabulary.

The vision/OCR side emits whatever the floor plan is labelled with — English
("Kitchen"), Persian ("آشپزخانه"), or a broad bucket ("Service", "Accommodation",
"Unknown"). The compliance agents match rooms by EXACT canonical strings
(`room_kitchen`, `room_bedroom`, …). This module maps the former to the latter
*before* the agents run, and — crucially — **flags anything it cannot confidently
map as `needs_review` instead of guessing**, so an unknown room can never silently
satisfy or fail a rule.

    summary = normalize_room_categories(bim_data)   # mutates rooms in place

Each room gets: canonical `category`, plus `category_raw`, `category_source`
(canonical | name | label | unmapped), `category_confidence`, and `needs_review`
(set True only when the category could not be resolved).

The mapping below is the ONE place to edit when you see a new label in real plans.
Broad/ambiguous buckets ("service", "accommodation", "room", "unknown") are
deliberately NOT mapped to a specific type — they force review.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

CANONICAL = {
    "room_bedroom", "room_living", "room_kitchen", "room_bathroom",
    "room_toilet", "room_entry", "room_storage",
}

# alias (lowercased) → canonical. Edit here to fit your OCR vocabulary.
ALIASES: Dict[str, str] = {
    # — kitchen —
    "kitchen": "room_kitchen", "kitchenette": "room_kitchen", "آشپزخانه": "room_kitchen",
    # — bedroom —
    "bedroom": "room_bedroom", "bed room": "room_bedroom", "master bedroom": "room_bedroom",
    "اتاق خواب": "room_bedroom", "اتاق‌خواب": "room_bedroom", "خواب": "room_bedroom",
    # — bathroom —
    "bathroom": "room_bathroom", "bath": "room_bathroom", "shower": "room_bathroom",
    "حمام": "room_bathroom", "دوش": "room_bathroom",
    # — toilet —
    "toilet": "room_toilet", "wc": "room_toilet", "water closet": "room_toilet",
    "restroom": "room_toilet", "powder room": "room_toilet",
    "توالت": "room_toilet", "دستشویی": "room_toilet",
    "سرویس بهداشتی": "room_toilet", "سرویس": "room_toilet",
    # — living —
    "living": "room_living", "living room": "room_living", "lounge": "room_living",
    "sitting": "room_living", "sitting room": "room_living", "reception": "room_living",
    "salon": "room_living", "نشیمن": "room_living", "اتاق نشیمن": "room_living",
    "پذیرایی": "room_living", "هال": "room_living",
    # — entry / circulation —
    "entry": "room_entry", "entrance": "room_entry", "hall": "room_entry",
    "hallway": "room_entry", "corridor": "room_entry", "foyer": "room_entry",
    "lobby": "room_entry", "vestibule": "room_entry",
    "راهرو": "room_entry", "ورودی": "room_entry", "سرسرا": "room_entry",
    # — storage —
    "storage": "room_storage", "store": "room_storage", "store room": "room_storage",
    "storeroom": "room_storage", "closet": "room_storage", "pantry": "room_storage",
    "انباری": "room_storage", "انبار": "room_storage", "کمد": "room_storage",
}

# Broad/ambiguous buckets that must NOT be guessed → force review.
AMBIGUOUS = {"", "room", "space", "area", "unknown", "service", "accommodation",
             "other", "misc", "اتاق", "فضا", "نامشخص"}


def _match(text: Optional[str]) -> Optional[str]:
    """Resolve one label to a canonical category, or None."""
    if not text:
        return None
    t = str(text).strip().lower()
    if not t or t in AMBIGUOUS:
        return None
    if t in CANONICAL:
        return t
    if t in ALIASES:
        return ALIASES[t]
    # substring fallback (handles "master bedroom 2", "آشپزخانه اپن", …)
    for alias, canon in ALIASES.items():
        if alias in t:
            return canon
    return None


def normalize_room_categories(bim_data: Dict[str, Any],
                              extra_aliases: Optional[Dict[str, str]] = None
                              ) -> Dict[str, Any]:
    """Resolve each room's category to a canonical `room_*`; flag the rest."""
    if extra_aliases:
        ALIASES.update({k.strip().lower(): v for k, v in extra_aliases.items()})

    by_canonical: Dict[str, int] = {}
    unmapped_raw: List[str] = []
    n_canonical = n_normalized = n_unmapped = 0

    for room in bim_data.get("rooms", []) or []:
        raw = room.get("category", "")
        room["category_raw"] = raw

        # 1) already canonical
        if str(raw).strip().lower() in CANONICAL:
            canon = str(raw).strip().lower()
            room["category"] = canon
            room["category_source"] = "canonical"
            room["category_confidence"] = 1.0
            n_canonical += 1
        else:
            # 2) try category label, then OCR name, then local (Persian) name
            canon = _match(raw)
            source, conf = "label", 0.6
            if canon is None:
                canon = _match(room.get("name"))
                source, conf = "name", 0.8
            if canon is None:
                canon = _match(room.get("local_name"))
                source, conf = "name", 0.8

            if canon is not None:
                room["category"] = canon
                room["category_source"] = source
                room["category_confidence"] = conf
                n_normalized += 1
            else:
                # 3) unmapped → keep raw, force review, never guess a type
                room["category_source"] = "unmapped"
                room["category_confidence"] = 0.0
                room["needs_review"] = True
                reasons = room.setdefault("review_reasons", [])
                rr = (f"room category {raw!r} could not be normalized to a "
                      f"canonical room type")
                if rr not in reasons:
                    reasons.append(rr)
                n_unmapped += 1
                unmapped_raw.append(str(raw))

        if room.get("category_source") != "unmapped":
            by_canonical[room["category"]] = by_canonical.get(room["category"], 0) + 1

    summary = {
        "total_rooms": n_canonical + n_normalized + n_unmapped,
        "canonical": n_canonical,
        "normalized": n_normalized,
        "unmapped": n_unmapped,
        "by_canonical": by_canonical,
        "unmapped_raw": sorted(set(unmapped_raw)),
    }
    bim_data["_category_summary"] = summary
    return summary
