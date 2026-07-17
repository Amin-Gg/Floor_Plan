from __future__ import annotations

import json
from pathlib import Path

import yaml

from config.runtime_classes import (
    PRIMARY_CLASS_ID_TO_NAME,
    PRIMARY_CLASS_NAME_TO_ID,
    PRIMARY_NUM_CLASSES,
    primary_class_name,
)

ROOT = Path(__file__).resolve().parents[1]


def test_active_primary_class_registry_is_exact_and_unambiguous():
    assert PRIMARY_NUM_CLASSES == 4
    assert dict(PRIMARY_CLASS_ID_TO_NAME) == {1: "wall", 2: "window", 3: "door"}
    assert dict(PRIMARY_CLASS_NAME_TO_ID) == {"wall": 1, "window": 2, "door": 3}
    assert primary_class_name(0) == "unknown"
    assert primary_class_name(4) == "unknown"


def test_dead_runtime_files_and_transition_artifacts_are_absent():
    removed = [
        "config/classes.py",
        "symbol_detector.py",
        "icon_prep.py",
        "analysis/slab_analysis.py",
        "analysis/stair_analysis.py",
        "docker-compose.cors-fixed.yml",
        "docker-compose.debug.yml",
        "README_UNIFIED.md",
        "readme.md",
        "delivery",
        "baseline",
        "compliance-engine/_delivery",
    ]
    assert all(not (ROOT / rel).exists() for rel in removed)
    assert not list(ROOT.glob("PHASE[0-8]_*.md"))
    assert not list(ROOT.glob("PHASE[0-8]_*.json"))
    assert not list(ROOT.glob("PHASE[0-8]_*.txt"))


def test_only_current_openapi_snapshots_are_published():
    assert (ROOT / "contracts/openapi_stage1.json").is_file()
    assert (ROOT / "compliance-engine/docs/contracts/openapi.json").is_file()
    assert not list((ROOT / "contracts").glob("openapi_stage1_phase*.json"))
    assert not list((ROOT / "compliance-engine/docs/contracts").glob("openapi_phase*.json"))


def test_stable_release_versions_are_consistent():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    stage1_spec = json.loads((ROOT / "contracts/openapi_stage1.json").read_text(encoding="utf-8"))
    engine_spec = json.loads(
        (ROOT / "compliance-engine/docs/contracts/openapi.json").read_text(encoding="utf-8")
    )
    assert 'version = "2.8.0"' in pyproject
    assert stage1_spec["info"]["version"] == "2.8.0"
    assert engine_spec["info"]["version"] == "1.4.0"
    assert compose["services"]["floorplan-api"]["image"] == "floorplan3d-api:2.8.0"
    assert compose["services"]["compliance-api"]["image"] == "mabhas-compliance:1.4.0"


def test_confidence_diagnostics_do_not_claim_measured_accuracy():
    text = (ROOT / "services/accuracy_service.py").read_text(encoding="utf-8")
    assert '"accuracy_claim": False' in text
    assert "Results appear very accurate" not in text
    assert "Ground-truth evaluation" in text


def test_final_documentation_surface_exists():
    for rel in (
        "README.md",
        "FINAL_CHANGELOG_FA.md",
        "FINAL_RUNBOOK_FA.md",
        "docs/ADR-009_FINAL_RELEASE_CLEANUP.md",
    ):
        assert (ROOT / rel).is_file(), rel


def test_zero_debt_lint_baseline_and_no_removed_baseline_paths():
    lint_baseline = json.loads((ROOT / "ruff-baseline.json").read_text(encoding="utf-8"))
    assert lint_baseline["stage1"] == []
    assert lint_baseline["engine"] == []
    for rel in (
        "scripts/run_stage1_test_matrix.py",
        "scripts/run_engine_test_matrix.py",
        "scripts/run_phase3_acceptance.py",
        "scripts/run_phase4_acceptance.py",
        "scripts/run_phase6_acceptance.py",
        "scripts/run_phase7_acceptance.py",
        "scripts/run_phase8_acceptance.py",
    ):
        assert "baseline/" not in (ROOT / rel).read_text(encoding="utf-8"), rel
