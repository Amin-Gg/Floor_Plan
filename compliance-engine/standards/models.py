"""Typed immutable views over the YAML standards contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class PropertyMapping:
    pset: Optional[str] = None
    property: Optional[str] = None
    attribute: Optional[str] = None
    source: str = "property"


@dataclass(frozen=True)
class PropertySpec:
    key: str
    aliases: tuple[str, ...] = ()
    ifc_mappings: tuple[PropertyMapping, ...] = ()
    data_type: str = "string"
    unit: Optional[str] = None
    required: bool = False
    required_for: tuple[str, ...] = ()
    min_value: Optional[float] = None
    max_value: Optional[float] = None


@dataclass(frozen=True)
class ElementSpec:
    key: str
    ifc_entities: tuple[str, ...]
    properties: Mapping[str, PropertySpec]


@dataclass(frozen=True)
class SemanticCatalog:
    version: str
    raw: Mapping[str, Any]
    elements: Mapping[str, ElementSpec]

    def property(self, element: str, name: str) -> PropertySpec:
        try:
            return self.elements[element].properties[name]
        except KeyError as exc:
            raise KeyError(f"Unknown semantic property {element}.{name}") from exc

    def requirements(self) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for kind, element in self.elements.items():
            for key, spec in element.properties.items():
                if not spec.required:
                    continue
                result.setdefault(kind, {})[key] = {
                    "required": True,
                    "unit": spec.unit,
                    "required_for": list(spec.required_for),
                    "data_type": spec.data_type,
                    "min_value": spec.min_value,
                    "max_value": spec.max_value,
                }
        return result


@dataclass(frozen=True)
class VocabularyEntry:
    canonical: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ControlledVocabulary:
    name: str
    entries: Mapping[str, VocabularyEntry]
    ambiguous: frozenset[str] = frozenset()
    substring_match: bool = False

    def alias_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for canonical, entry in self.entries.items():
            result[canonical.casefold()] = canonical
            for alias in entry.aliases:
                result[alias.casefold()] = canonical
        return result


@dataclass(frozen=True)
class ControlledValues:
    version: str
    vocabularies: Mapping[str, ControlledVocabulary]

    def vocabulary(self, name: str) -> ControlledVocabulary:
        try:
            return self.vocabularies[name]
        except KeyError as exc:
            raise KeyError(f"Unknown controlled vocabulary {name!r}") from exc


@dataclass(frozen=True)
class NormalizedValue:
    raw_value: Any
    canonical_value: Optional[str]
    vocabulary: str
    matched_alias: Optional[str]
    confidence: float
    source: str

    @property
    def matched(self) -> bool:
        return self.canonical_value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_value": self.raw_value,
            "canonical_value": self.canonical_value,
            "vocabulary": self.vocabulary,
            "matched_alias": self.matched_alias,
            "confidence": self.confidence,
            "source": self.source,
        }
