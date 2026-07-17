"""Configuration-driven controlled-value normalization.

Room aliases and other controlled vocabularies live in
``standards/controlled_values.yaml``. Request-specific aliases are merged into
an isolated local dictionary and never mutate module or process state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from standards.loaders import load_controlled_values
from standards.models import NormalizedValue


def _clean(value: Any) -> str:
    return str(value).strip().casefold() if value is not None else ""


def normalize_controlled_value(
    vocabulary: str,
    raw_value: Any,
    *,
    extra_aliases: Optional[Mapping[str, str]] = None,
) -> NormalizedValue:
    vocab = load_controlled_values().vocabulary(vocabulary)
    text = _clean(raw_value)
    aliases = vocab.alias_map()

    if extra_aliases:
        # Request-local copy: no shared state mutation and canonical targets
        # must already exist in the configured vocabulary.
        known = set(vocab.entries)
        for alias, canonical in dict(extra_aliases).items():
            canonical_text = str(canonical)
            if canonical_text not in known:
                raise ValueError(
                    f"Extra alias {alias!r} targets unknown {vocabulary} value "
                    f"{canonical_text!r}"
                )
            aliases[_clean(alias)] = canonical_text

    if not text or text in vocab.ambiguous:
        return NormalizedValue(raw_value, None, vocabulary, None, 0.0, "ambiguous")
    if text in aliases:
        canonical = aliases[text]
        source = "canonical" if text == canonical.casefold() else "alias"
        return NormalizedValue(raw_value, canonical, vocabulary, text, 1.0, source)
    if vocab.substring_match:
        matches = [
            (len(alias), alias, canonical)
            for alias, canonical in aliases.items()
            if alias and alias in text
        ]
        if matches:
            _, alias, canonical = max(matches, key=lambda row: row[0])
            return NormalizedValue(raw_value, canonical, vocabulary, alias, 0.8, "substring")
    return NormalizedValue(raw_value, None, vocabulary, None, 0.0, "unmapped")


def normalize_room_categories(
    bim_data: Dict[str, Any],
    extra_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Normalize every room through the configured ``room_types`` vocabulary."""
    by_canonical: Dict[str, int] = {}
    unmapped_raw: List[str] = []
    n_canonical = n_normalized = n_unmapped = 0

    for room in bim_data.get("rooms", []) or []:
        raw = room.get("category", "")
        room["category_raw"] = raw

        result = normalize_controlled_value(
            "room_types", raw, extra_aliases=extra_aliases
        )
        source_field = "label"
        confidence = 0.6
        if not result.matched:
            result = normalize_controlled_value(
                "room_types", room.get("name"), extra_aliases=extra_aliases
            )
            source_field = "name"
            confidence = 0.8
        if not result.matched:
            result = normalize_controlled_value(
                "room_types", room.get("local_name"), extra_aliases=extra_aliases
            )
            source_field = "name"
            confidence = 0.8

        if result.matched:
            room["category"] = result.canonical_value
            if result.source == "canonical" and _clean(raw) == _clean(result.canonical_value):
                room["category_source"] = "canonical"
                room["category_confidence"] = 1.0
                n_canonical += 1
            else:
                room["category_source"] = source_field
                room["category_confidence"] = confidence
                n_normalized += 1
            room["category_vocabulary"] = result.vocabulary
            room["category_matched_alias"] = result.matched_alias
            by_canonical[result.canonical_value] = by_canonical.get(result.canonical_value, 0) + 1
        else:
            room["category_source"] = "unmapped"
            room["category_confidence"] = 0.0
            room["category_vocabulary"] = "room_types"
            room["category_matched_alias"] = None
            room["needs_review"] = True
            reasons = room.setdefault("review_reasons", [])
            reason = (
                f"room category {raw!r} could not be normalized to a "
                "canonical room type"
            )
            if reason not in reasons:
                reasons.append(reason)
            n_unmapped += 1
            unmapped_raw.append(str(raw))

    summary = {
        "total_rooms": n_canonical + n_normalized + n_unmapped,
        "canonical": n_canonical,
        "normalized": n_normalized,
        "unmapped": n_unmapped,
        "by_canonical": by_canonical,
        "unmapped_raw": sorted(set(unmapped_raw)),
        "vocabulary_version": load_controlled_values().version,
    }
    bim_data["_category_summary"] = summary
    return summary
