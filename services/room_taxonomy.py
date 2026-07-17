"""Canonical room taxonomy loaded from the shared controlled-values contract.

The monorepo source of truth is ``contracts/controlled_values_v1.yaml``. The
compliance engine resolves the same file when it is deployed inside this
workspace, and retains its bundled standards copy only as a standalone fallback.
Request-specific aliases are local and never mutate process-global state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "controlled_values_v1.yaml"


def _load_room_vocabulary() -> tuple[str, dict[str, str], frozenset[str], bool]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    version = str(raw["version"])
    room_types = raw["vocabularies"]["room_types"]
    aliases: dict[str, str] = {}
    for canonical, spec in room_types["values"].items():
        aliases[str(canonical).casefold()] = str(canonical)
        for alias in spec.get("aliases", []):
            aliases[str(alias).strip().casefold()] = str(canonical)
    ambiguous = frozenset(
        str(value).strip().casefold() for value in room_types.get("ambiguous", [])
    )
    return version, aliases, ambiguous, bool(room_types.get("substring_match", False))


VOCABULARY_VERSION, _ALIASES, _AMBIGUOUS, _SUBSTRING_MATCH = _load_room_vocabulary()
CANONICAL = frozenset(_ALIASES.values())


def _match(
    text: Optional[str], aliases: Mapping[str, str]
) -> tuple[Optional[str], Optional[str], str]:
    if text is None:
        return None, None, "empty"
    cleaned = str(text).strip().casefold()
    if not cleaned or cleaned in _AMBIGUOUS:
        return None, None, "ambiguous"
    if cleaned in aliases:
        canonical = aliases[cleaned]
        source = "canonical" if cleaned == canonical.casefold() else "alias"
        return canonical, cleaned, source
    if _SUBSTRING_MATCH:
        matches = [
            (len(alias), alias, canonical)
            for alias, canonical in aliases.items()
            if alias and alias in cleaned
        ]
        if matches:
            _, alias, canonical = max(matches, key=lambda row: row[0])
            return canonical, alias, "substring"
    return None, None, "unmapped"


def normalize_room_categories(
    bim_data: Dict[str, Any],
    extra_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    aliases = dict(_ALIASES)
    if extra_aliases:
        unknown_targets = sorted(set(extra_aliases.values()) - CANONICAL)
        if unknown_targets:
            raise ValueError(
                f"extra room aliases target unknown canonical values: {unknown_targets}"
            )
        aliases.update(
            {
                str(alias).strip().casefold(): str(canonical)
                for alias, canonical in extra_aliases.items()
            }
        )

    by_canonical: Dict[str, int] = {}
    unmapped_raw: list[str] = []
    n_canonical = n_normalized = n_unmapped = 0

    for room in bim_data.get("rooms", []) or []:
        raw = room.get("category", "")
        room["category_raw"] = raw

        candidates = [
            ("label", raw, 0.6),
            ("name", room.get("name"), 0.8),
            ("name", room.get("local_name"), 0.8),
        ]
        matched = None
        for source_field, value, confidence in candidates:
            canonical, alias, match_source = _match(value, aliases)
            if canonical is not None:
                matched = (canonical, alias, match_source, source_field, confidence)
                break

        if matched is None:
            room["category_source"] = "unmapped"
            room["category_confidence"] = 0.0
            room["category_vocabulary"] = "room_types"
            room["category_vocabulary_version"] = VOCABULARY_VERSION
            room["category_matched_alias"] = None
            room["needs_review"] = True
            reasons = room.setdefault("review_reasons", [])
            reason = f"room category {raw!r} could not be normalized to a canonical room type"
            if reason not in reasons:
                reasons.append(reason)
            n_unmapped += 1
            unmapped_raw.append(str(raw))
            continue

        canonical, alias, match_source, source_field, confidence = matched
        room["category"] = canonical
        is_canonical = (
            match_source == "canonical" and str(raw).strip().casefold() == canonical.casefold()
        )
        room["category_source"] = "canonical" if is_canonical else source_field
        room["category_confidence"] = 1.0 if is_canonical else confidence
        room["category_vocabulary"] = "room_types"
        room["category_vocabulary_version"] = VOCABULARY_VERSION
        room["category_matched_alias"] = alias
        if is_canonical:
            n_canonical += 1
        else:
            n_normalized += 1
        by_canonical[canonical] = by_canonical.get(canonical, 0) + 1

    summary = {
        "total_rooms": n_canonical + n_normalized + n_unmapped,
        "canonical": n_canonical,
        "normalized": n_normalized,
        "unmapped": n_unmapped,
        "by_canonical": by_canonical,
        "unmapped_raw": sorted(set(unmapped_raw)),
        "vocabulary_version": VOCABULARY_VERSION,
        "source_contract": "contracts/controlled_values_v1.yaml",
    }
    bim_data["_category_summary"] = summary
    return summary


def controlled_vocabulary_info() -> dict[str, Any]:
    """Return the shared room-taxonomy contract identity for diagnostics."""
    return {
        "vocabulary": "room_types",
        "version": VOCABULARY_VERSION,
        "source": "contracts/controlled_values_v1.yaml",
        "canonical_values": sorted(CANONICAL),
    }
