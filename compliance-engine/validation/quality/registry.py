"""Explicit ordered registry for model-quality plugins."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .checks import (
    ContractReadCheck,
    ElementConfidenceCheck,
    IdentityIntegrityCheck,
    ManualParametersCheck,
    OpeningPlacementCheck,
    RequiredPropertiesCheck,
    ScaleConfidenceCheck,
    SpaceTaggingCheck,
    StoreyConsistencyCheck,
    UnitConsistencyCheck,
)
from .protocol import QualityCheck


RESERVED_QUALITY_CODES = {"QC-INTERNAL-001"}


DEFAULT_QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    ContractReadCheck(),
    IdentityIntegrityCheck(),
    SpaceTaggingCheck(),
    RequiredPropertiesCheck(),
    UnitConsistencyCheck(),
    StoreyConsistencyCheck(),
    ElementConfidenceCheck(),
    ScaleConfidenceCheck(),
    ManualParametersCheck(),
    OpeningPlacementCheck(),
)


def validate_registry(checks: Sequence[QualityCheck]) -> tuple[QualityCheck, ...]:
    """Validate plugin metadata and return an immutable ordered registry."""
    result = tuple(checks)
    names: set[str] = set()
    prefixes: set[str] = set()
    codes: set[str] = set()

    for check in result:
        for attr in ("name", "code_prefix", "codes", "blocking"):
            if not hasattr(check, attr):
                raise TypeError(f"Quality check {check!r} is missing {attr!r}")
        if not callable(getattr(check, "applies_to", None)) or not callable(getattr(check, "run", None)):
            raise TypeError(f"Quality check {check!r} does not implement applies_to/run")
        if not check.name or check.name in names:
            raise ValueError(f"Duplicate or empty quality-check name: {check.name!r}")
        if not check.code_prefix or check.code_prefix in prefixes:
            raise ValueError(f"Duplicate or empty quality code prefix: {check.code_prefix!r}")
        names.add(check.name)
        prefixes.add(check.code_prefix)

        if not isinstance(check.codes, tuple) or not check.codes:
            raise ValueError(f"Quality check {check.name!r} must declare a non-empty codes tuple")
        for code in check.codes:
            if code in RESERVED_QUALITY_CODES:
                raise ValueError(f"Quality finding code is reserved by the framework: {code!r}")
            if not code.startswith(check.code_prefix):
                raise ValueError(
                    f"Code {code!r} does not start with prefix {check.code_prefix!r}"
                )
            if code in codes:
                raise ValueError(f"Duplicate quality finding code: {code!r}")
            codes.add(code)
    return result


def build_registry(extra_checks: Iterable[QualityCheck] = ()) -> tuple[QualityCheck, ...]:
    """Return the default ordered registry plus request-local extensions."""
    return validate_registry((*DEFAULT_QUALITY_CHECKS, *tuple(extra_checks)))
