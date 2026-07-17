"""Explicit unit conversion helpers for the canonical domain model.

No conversion is performed for an unknown or missing unit. Callers must treat
that as missing/untrusted data rather than guessing.
"""
from __future__ import annotations

from typing import Optional

_LENGTH_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}
_AREA_TO_M2 = {"mm2": 1e-6, "cm2": 1e-4, "m2": 1.0}


def normalise_unit_name(unit: str | None) -> str | None:
    if unit is None:
        return None
    return str(unit).strip().lower().replace("²", "2").replace("^2", "2")


def length_to_mm(value: object, unit: str | None) -> Optional[float]:
    key = normalise_unit_name(unit)
    if value is None or key not in _LENGTH_TO_MM:
        return None
    try:
        return float(value) * _LENGTH_TO_MM[key]
    except (TypeError, ValueError):
        return None


def area_to_m2(value: object, unit: str | None) -> Optional[float]:
    key = normalise_unit_name(unit)
    if value is None or key not in _AREA_TO_M2:
        return None
    try:
        return float(value) * _AREA_TO_M2[key]
    except (TypeError, ValueError):
        return None


def supported_length_unit(unit: str | None) -> bool:
    return normalise_unit_name(unit) in _LENGTH_TO_MM


def supported_area_unit(unit: str | None) -> bool:
    return normalise_unit_name(unit) in _AREA_TO_M2
