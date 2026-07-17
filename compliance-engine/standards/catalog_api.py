"""Typed query API over the canonical semantic property catalog."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from standards.loaders import clear_standards_caches, load_semantic_catalog

_active_path: Optional[str] = None


def _catalog(path: Optional[str] = None, *, force_reload: bool = False):
    return load_semantic_catalog(path if path is not None else _active_path, force_reload=force_reload)


def _raw(path: Optional[str] = None, *, force_reload: bool = False) -> Dict[str, Any]:
    return deepcopy(dict(_catalog(path, force_reload=force_reload).raw))


def load_catalog(path: Optional[str] = None) -> Dict[str, Any]:
    return _raw(path)


def reload_catalog(path: Optional[str] = None) -> Dict[str, Any]:
    global _active_path
    _active_path = path
    clear_standards_caches()
    return _raw(path, force_reload=True)


def pset_name(pset_key: str) -> str:
    return str(_catalog().raw["psets"][pset_key]["name"])


def prop(pset_key: str, prop_key: str) -> str:
    return str(_catalog().raw["psets"][pset_key]["properties"][prop_key])


def param_map() -> Dict[str, str]:
    catalog = _catalog()
    props = catalog.raw["psets"]["contract"]["properties"]
    return {str(props[key]): str(value) for key, value in catalog.raw["param_map"].items()}


def quality_requirements() -> Dict[str, Dict[str, Dict[str, Any]]]:
    return _catalog().requirements()


def property_spec(element_key: str, property_key: str) -> Dict[str, Any]:
    spec = _catalog().property(element_key, property_key)
    return {
        "aliases": list(spec.aliases),
        "ifc_mappings": [
            {
                "pset": mapping.pset,
                "property": mapping.property,
                "attribute": mapping.attribute,
                "source": mapping.source,
            }
            for mapping in spec.ifc_mappings
        ],
        "data_type": spec.data_type,
        "unit": spec.unit,
        "required": spec.required,
        "required_for": list(spec.required_for),
        "min_value": spec.min_value,
        "max_value": spec.max_value,
    }


def ifc_mappings(element_key: str, property_key: str) -> tuple[dict[str, Any], ...]:
    return tuple(property_spec(element_key, property_key)["ifc_mappings"])


def supported_units(dimension: str) -> tuple[str, ...]:
    spec = _catalog().raw["units"][dimension]
    return tuple(str(value) for value in spec.get("supported_input") or [])


def clause_property_semantics(property_text: str) -> Optional[Dict[str, Any]]:
    text = str(property_text or "").strip().casefold()
    if not text:
        return None
    definitions = _catalog().raw.get("compliance", {}).get("clause_properties", {})
    # Exact match first, then longest substring. This retains the conservative
    # Phase-4 behavior while moving vocabulary and units into configuration.
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for key, spec in definitions.items():
        aliases = [str(key), *(str(v) for v in (spec.get("aliases") or []))]
        for alias in aliases:
            normalized = alias.strip().casefold()
            if text == normalized:
                return {"key": str(key), **deepcopy(dict(spec))}
            if normalized and normalized in text:
                candidates.append((len(normalized), str(key), dict(spec)))
    if not candidates:
        return None
    _, key, spec = max(candidates, key=lambda row: row[0])
    return {"key": key, **deepcopy(spec)}


