"""Configuration policy for IFC schema validation.

The policy is explicit and immutable so supported schema versions and strictness
cannot drift between API, CLI, tests, and background workers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


DEFAULT_SUPPORTED_VERSIONS: FrozenSet[str] = frozenset({"IFC4", "IFC4X1", "IFC4X3"})


@dataclass(frozen=True)
class SchemaValidationPolicy:
    """Controls the blocking IFC schema checks.

    ``IFC4X3`` includes official maintenance identifiers such as
    ``IFC4X3_ADD1``, ``IFC4X3_ADD2`` and ``IFC4X3_TC1``. IFC2X3 is disabled by
    default because downstream semantic mappings are IFC4-oriented; it may be
    enabled only for explicitly tested compatibility workflows.
    """

    supported_versions: FrozenSet[str] = field(default_factory=lambda: DEFAULT_SUPPORTED_VERSIONS)
    allow_ifc2x3: bool = False
    strict_mandatory_attributes: bool = True
    require_spatial_hierarchy: bool = True
    require_unique_global_ids: bool = True

    def __post_init__(self) -> None:
        normalized = frozenset(str(v).strip().upper() for v in self.supported_versions if str(v).strip())
        if not normalized:
            raise ValueError("supported_versions cannot be empty")
        object.__setattr__(self, "supported_versions", normalized)

    def supports(self, schema_identifier: str) -> bool:
        schema = str(schema_identifier or "").strip().upper()
        if schema == "IFC2X3":
            return self.allow_ifc2x3
        if schema in self.supported_versions:
            return True
        # Official IFC4X3 maintenance releases retain the IFC4X3 semantic family.
        return "IFC4X3" in self.supported_versions and schema.startswith("IFC4X3_")

    @property
    def supported_label(self) -> str:
        values = sorted(self.supported_versions)
        if self.allow_ifc2x3:
            values.insert(0, "IFC2X3")
        if "IFC4X3" in self.supported_versions:
            values.append("IFC4X3 maintenance releases")
        return ", ".join(values)
