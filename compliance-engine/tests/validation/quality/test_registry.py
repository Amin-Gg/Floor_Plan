from __future__ import annotations

import pytest

from domain.findings import Finding
from domain.model import BuildingModel
from validation.quality import DEFAULT_QUALITY_CHECKS, build_registry, validate_registry
from validation.quality.context import QualityContext


class DummyCheck:
    code_prefix = "QC-DUMMY"
    codes = ("QC-DUMMY-001",)
    name = "dummy"
    blocking = False

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        return False

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        return []


def test_default_registry_is_explicit_and_ordered():
    assert [check.name for check in DEFAULT_QUALITY_CHECKS] == [
        "contract_read",
        "identity_integrity",
        "space_tagging",
        "required_properties",
        "unit_consistency",
        "storey_consistency",
        "element_confidence",
        "scale_confidence",
        "manual_parameters",
        "opening_placement",
    ]


def test_all_builtin_codes_are_unique():
    codes = [code for check in DEFAULT_QUALITY_CHECKS for code in check.codes]
    assert len(codes) == len(set(codes))


def test_extra_check_requires_registration_only():
    registry = build_registry([DummyCheck()])
    assert registry[-1].name == "dummy"
    assert len(registry) == len(DEFAULT_QUALITY_CHECKS) + 1


def test_duplicate_check_name_is_rejected():
    with pytest.raises(ValueError, match="name"):
        validate_registry([DummyCheck(), DummyCheck()])


def test_code_must_match_declared_prefix():
    class BadPrefix(DummyCheck):
        name = "bad_prefix"
        code_prefix = "QC-OTHER"

    with pytest.raises(ValueError, match="does not start"):
        validate_registry([BadPrefix()])


def test_duplicate_code_across_plugins_is_rejected():
    class DuplicateCode(DummyCheck):
        name = "duplicate_code"
        code_prefix = "QC-DUMMY-001"
        codes = ("QC-DUMMY-001",)

    with pytest.raises(ValueError, match="Duplicate quality finding code"):
        validate_registry([DummyCheck(), DuplicateCode()])


def test_framework_internal_code_is_reserved():
    class Reserved(DummyCheck):
        name = "reserved"
        code_prefix = "QC-INTERNAL"
        codes = ("QC-INTERNAL-001",)

    with pytest.raises(ValueError, match="reserved"):
        validate_registry([Reserved()])
