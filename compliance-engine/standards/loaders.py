"""Thread-safe fail-fast loaders for the Phase-5 standards contracts."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

import yaml

from .models import (
    ControlledValues,
    ControlledVocabulary,
    ElementSpec,
    PropertyMapping,
    PropertySpec,
    SemanticCatalog,
    VocabularyEntry,
)

_ROOT = Path(__file__).resolve().parent
_DEFAULT_SEMANTIC = _ROOT / "semantic_property_catalog.yaml"
_DEFAULT_CONTROLLED = _ROOT / "controlled_values.yaml"
_LOCK = threading.RLock()
_SEMANTIC_CACHE: dict[str, SemanticCatalog] = {}
_CONTROLLED_CACHE: dict[str, ControlledValues] = {}


def _path(explicit: Optional[str], env_names: tuple[str, ...], default: Path) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    for name in env_names:
        value = os.getenv(name)
        if value:
            return Path(value).expanduser().resolve()
    return default.resolve()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required standards file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in standards file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Standards file {path} must contain a mapping")
    return raw


def _number(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric, not boolean")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc


def _parse_semantic(raw: dict[str, Any], path: Path) -> SemanticCatalog:
    version = raw.get("catalog_version")
    if not isinstance(version, (str, int, float)) or not str(version).strip():
        raise ValueError(f"Semantic catalog {path}: catalog_version is required")
    psets = raw.get("psets")
    param_map = raw.get("param_map")
    elements_raw = raw.get("elements")
    units = raw.get("units")
    if not isinstance(psets, dict) or not isinstance(param_map, dict):
        raise ValueError(f"Semantic catalog {path}: psets and param_map are required")
    if not isinstance(elements_raw, dict) or not elements_raw:
        raise ValueError(f"Semantic catalog {path}: elements mapping is required")
    if not isinstance(units, dict) or not units:
        raise ValueError(f"Semantic catalog {path}: units mapping is required")

    for pset_key, pset in psets.items():
        if not isinstance(pset, dict) or not isinstance(pset.get("name"), str):
            raise ValueError(f"Semantic catalog {path}: malformed pset {pset_key!r}")
        if not isinstance(pset.get("properties"), dict):
            raise ValueError(f"Semantic catalog {path}: pset {pset_key!r} needs properties")

    compatibility = raw.get("compatibility") or {}
    required_pset_properties = compatibility.get("required_pset_properties") or {}
    if not isinstance(required_pset_properties, dict):
        raise ValueError(f"Semantic catalog {path}: compatibility.required_pset_properties must be a mapping")
    for pset_key, required_keys in required_pset_properties.items():
        if pset_key not in psets or not isinstance(required_keys, list):
            raise ValueError(f"Semantic catalog {path}: compatibility requirement for {pset_key!r} malformed")
        missing = set(str(x) for x in required_keys) - set(psets[pset_key]["properties"])
        if missing:
            raise ValueError(
                f"Semantic catalog {path}: pset {pset_key!r} missing properties {sorted(missing)}"
            )
    contract_properties = psets.get("contract", {}).get("properties", {})
    missing_param_keys = set(param_map) - set(contract_properties)
    if missing_param_keys:
        raise ValueError(
            f"Semantic catalog {path}: param_map references missing contract keys {sorted(missing_param_keys)}"
        )

    parsed_elements: dict[str, ElementSpec] = {}
    for element_key, element_raw in elements_raw.items():
        if not isinstance(element_raw, dict):
            raise ValueError(f"Semantic catalog {path}: element {element_key!r} malformed")
        entities = element_raw.get("ifc_entities")
        properties_raw = element_raw.get("properties")
        if not isinstance(entities, list) or not all(isinstance(x, str) and x for x in entities):
            raise ValueError(f"Semantic catalog {path}: {element_key}.ifc_entities invalid")
        if not isinstance(properties_raw, dict) or not properties_raw:
            raise ValueError(f"Semantic catalog {path}: {element_key}.properties invalid")
        parsed_properties: dict[str, PropertySpec] = {}
        for prop_key, prop_raw in properties_raw.items():
            if not isinstance(prop_raw, dict):
                raise ValueError(f"Semantic catalog {path}: {element_key}.{prop_key} malformed")
            mappings_raw = prop_raw.get("ifc_mappings") or []
            if not isinstance(mappings_raw, list):
                raise ValueError(f"Semantic catalog {path}: {element_key}.{prop_key}.ifc_mappings must be a list")
            mappings: list[PropertyMapping] = []
            for index, mapping in enumerate(mappings_raw):
                if not isinstance(mapping, dict):
                    raise ValueError(f"Semantic catalog {path}: mapping {element_key}.{prop_key}[{index}] malformed")
                attribute = mapping.get("attribute")
                pset = mapping.get("pset")
                property_key = mapping.get("property")
                if attribute is None and (pset is None or property_key is None):
                    raise ValueError(
                        f"Semantic catalog {path}: mapping {element_key}.{prop_key}[{index}] "
                        "needs attribute or pset+property"
                    )
                if pset is not None:
                    if pset not in psets:
                        raise ValueError(f"Semantic catalog {path}: unknown pset key {pset!r}")
                    if property_key not in psets[pset]["properties"]:
                        raise ValueError(
                            f"Semantic catalog {path}: unknown property key {pset}.{property_key}"
                        )
                mappings.append(PropertyMapping(
                    pset=str(pset) if pset is not None else None,
                    property=str(property_key) if property_key is not None else None,
                    attribute=str(attribute) if attribute is not None else None,
                    source=str(mapping.get("source") or "property"),
                ))
            aliases = prop_raw.get("aliases") or []
            required_for = prop_raw.get("required_for") or []
            if not isinstance(aliases, list) or not all(isinstance(x, str) for x in aliases):
                raise ValueError(f"Semantic catalog {path}: aliases for {element_key}.{prop_key} invalid")
            if not isinstance(required_for, list) or not all(isinstance(x, str) for x in required_for):
                raise ValueError(f"Semantic catalog {path}: required_for for {element_key}.{prop_key} invalid")
            unit = prop_raw.get("unit")
            if unit is not None:
                known_units = {
                    str(item)
                    for spec in units.values() if isinstance(spec, dict)
                    for item in (spec.get("supported_input") or [])
                } | {
                    str(spec.get("canonical_internal"))
                    for spec in units.values() if isinstance(spec, dict) and spec.get("canonical_internal")
                }
                if str(unit) not in known_units:
                    raise ValueError(f"Semantic catalog {path}: unknown unit {unit!r} for {element_key}.{prop_key}")
            min_value = _number(prop_raw.get("min_value"), f"{element_key}.{prop_key}.min_value")
            max_value = _number(prop_raw.get("max_value"), f"{element_key}.{prop_key}.max_value")
            if min_value is not None and max_value is not None and min_value > max_value:
                raise ValueError(f"Semantic catalog {path}: min_value > max_value for {element_key}.{prop_key}")
            parsed_properties[str(prop_key)] = PropertySpec(
                key=str(prop_key),
                aliases=tuple(str(x) for x in aliases),
                ifc_mappings=tuple(mappings),
                data_type=str(prop_raw.get("data_type") or "string"),
                unit=str(unit) if unit is not None else None,
                required=bool(prop_raw.get("required", False)),
                required_for=tuple(str(x) for x in required_for),
                min_value=min_value,
                max_value=max_value,
            )
        parsed_elements[str(element_key)] = ElementSpec(
            key=str(element_key),
            ifc_entities=tuple(entities),
            properties=MappingProxyType(parsed_properties),
        )
    return SemanticCatalog(
        version=str(version),
        raw=MappingProxyType(raw),
        elements=MappingProxyType(parsed_elements),
    )


def _parse_controlled(raw: dict[str, Any], path: Path) -> ControlledValues:
    version = raw.get("version")
    vocabularies_raw = raw.get("vocabularies")
    if not isinstance(version, (str, int, float)) or not str(version).strip():
        raise ValueError(f"Controlled values {path}: version is required")
    if not isinstance(vocabularies_raw, dict) or not vocabularies_raw:
        raise ValueError(f"Controlled values {path}: vocabularies mapping is required")
    result: dict[str, ControlledVocabulary] = {}
    for name, vocab_raw in vocabularies_raw.items():
        if not isinstance(vocab_raw, dict):
            raise ValueError(f"Controlled values {path}: vocabulary {name!r} malformed")
        entries_raw = vocab_raw.get("values")
        if not isinstance(entries_raw, dict) or not entries_raw:
            raise ValueError(f"Controlled values {path}: {name}.values required")
        entries: dict[str, VocabularyEntry] = {}
        seen_aliases: dict[str, str] = {}
        for canonical, spec in entries_raw.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Controlled values {path}: {name}.{canonical} malformed")
            aliases = spec.get("aliases") or []
            if not isinstance(aliases, list) or not all(isinstance(x, (str, bool, int, float)) for x in aliases):
                raise ValueError(f"Controlled values {path}: aliases for {name}.{canonical} invalid")
            normalized_aliases = tuple(str(x).strip() for x in aliases if str(x).strip())
            for alias in (str(canonical), *normalized_aliases):
                key = alias.casefold()
                previous = seen_aliases.get(key)
                if previous is not None and previous != str(canonical):
                    raise ValueError(
                        f"Controlled values {path}: alias {alias!r} maps to both "
                        f"{previous!r} and {canonical!r}"
                    )
                seen_aliases[key] = str(canonical)
            entries[str(canonical)] = VocabularyEntry(
                canonical=str(canonical), aliases=normalized_aliases
            )
        ambiguous = vocab_raw.get("ambiguous") or []
        if not isinstance(ambiguous, list):
            raise ValueError(f"Controlled values {path}: {name}.ambiguous must be a list")
        result[str(name)] = ControlledVocabulary(
            name=str(name),
            entries=MappingProxyType(entries),
            ambiguous=frozenset(str(x).strip().casefold() for x in ambiguous),
            substring_match=bool(vocab_raw.get("substring_match", False)),
        )
    return ControlledValues(version=str(version), vocabularies=MappingProxyType(result))


def load_semantic_catalog(path: Optional[str] = None, *, force_reload: bool = False) -> SemanticCatalog:
    resolved = _path(path, ("SEMANTIC_PROPERTY_CATALOG", "IRPSET_CATALOG"), _DEFAULT_SEMANTIC)
    key = str(resolved)
    with _LOCK:
        if force_reload or key not in _SEMANTIC_CACHE:
            _SEMANTIC_CACHE[key] = _parse_semantic(_load_yaml(resolved), resolved)
        return _SEMANTIC_CACHE[key]


def load_controlled_values(path: Optional[str] = None, *, force_reload: bool = False) -> ControlledValues:
    resolved = _path(path, ("CONTROLLED_VALUES_CATALOG",), _DEFAULT_CONTROLLED)
    key = str(resolved)
    with _LOCK:
        if force_reload or key not in _CONTROLLED_CACHE:
            _CONTROLLED_CACHE[key] = _parse_controlled(_load_yaml(resolved), resolved)
        return _CONTROLLED_CACHE[key]


def clear_standards_caches() -> None:
    with _LOCK:
        _SEMANTIC_CACHE.clear()
        _CONTROLLED_CACHE.clear()


def validate_standards() -> tuple[SemanticCatalog, ControlledValues]:
    """Load both contracts now so invalid deployment configuration fails early."""
    return load_semantic_catalog(), load_controlled_values()
