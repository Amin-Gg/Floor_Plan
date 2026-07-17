"""
Stage 1 — L1 IFC Schema Validator tests.

Invariants locked here:
  * A broken/garbage/missing IFC produces a clean, structured L1 result —
    never a downstream crash and never a compliance verdict.
  * Missing spatial structure (project/site/building/storey) blocks.
  * The gate (require_valid_ifc / run_ifc_compliance) raises IfcSchemaError
    with the full structured findings BEFORE any ingest code runs.
  * A valid model passes L1, and the pipeline attaches the schema stage to
    bim_data["_schema"].
"""
from __future__ import annotations

import os

import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from validation.schema import (BLOCKING_CODES, IfcSchemaError,
                                     require_valid_ifc, validate_ifc_schema)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_plan.ifc")


def _write_minimal_ifc(path, *, project=True, site=True, building=True,
                       storey=True, schema="IFC4"):
    """A structurally minimal IFC with configurable spatial structure."""
    import ifcopenshell.guid as guid
    f = ifcopenshell.file(schema=schema)
    p = f.create_entity("IfcProject", GlobalId=guid.new(), Name="P") if project else None
    s = f.create_entity("IfcSite", GlobalId=guid.new(), Name="S") if site else None
    b = f.create_entity("IfcBuilding", GlobalId=guid.new(), Name="B") if building else None
    level = f.create_entity("IfcBuildingStorey", GlobalId=guid.new(), Name="L1") if storey else None
    for parent, child in ((p, s), (s, b), (b, level)):
        if parent is not None and child is not None:
            f.create_entity(
                "IfcRelAggregates", GlobalId=guid.new(),
                RelatingObject=parent, RelatedObjects=[child],
            )
    f.write(str(path))
    return str(path)


# ── 001: unreadable input ────────────────────────────────────────────────────

def test_missing_file_fails_001():
    result, model = validate_ifc_schema("/nonexistent/plan.ifc")
    assert result.status == "failed" and model is None
    assert result.findings[0].code == "IFC-SCHEMA-001"


def test_garbage_file_fails_001(tmp_path):
    p = tmp_path / "garbage.ifc"
    p.write_text("this is not an ifc file at all")
    result, model = validate_ifc_schema(str(p))
    assert result.status == "failed" and model is None
    assert result.findings[0].code == "IFC-SCHEMA-001"


# ── 003–006: spatial structure ───────────────────────────────────────────────

def test_missing_project_fails_003(tmp_path):
    p = _write_minimal_ifc(tmp_path / "no_proj.ifc", project=False)
    result, _ = validate_ifc_schema(p)
    assert result.status == "failed"
    assert any(f.code == "IFC-SCHEMA-003" for f in result.findings)


def test_missing_storey_fails_006(tmp_path):
    p = _write_minimal_ifc(tmp_path / "no_storey.ifc", storey=False)
    result, _ = validate_ifc_schema(p)
    assert result.status == "failed"
    assert any(f.code == "IFC-SCHEMA-006" for f in result.findings)


def test_minimal_valid_model_passes_with_empty_model_alert(tmp_path):
    p = _write_minimal_ifc(tmp_path / "ok.ifc")
    result, model = validate_ifc_schema(p)
    assert result.status == "passed_with_alerts"      # 009: no products
    assert model is not None
    assert all(f.severity != "fail" for f in result.findings)
    assert any(f.code == "IFC-SCHEMA-009" for f in result.findings)


# ── the gate ─────────────────────────────────────────────────────────────────

def test_gate_raises_with_structured_result(tmp_path):
    p = _write_minimal_ifc(tmp_path / "no_storey.ifc", storey=False)
    with pytest.raises(IfcSchemaError) as exc:
        require_valid_ifc(p)
    d = exc.value.result.to_dict()
    assert d["stage"] == "schema" and d["status"] == "failed"
    assert any(f["code"] == "IFC-SCHEMA-006" for f in d["findings"])


def test_run_ifc_compliance_stops_before_ingest_on_garbage(tmp_path):
    from tests.helpers import run_ifc_compliance
    p = tmp_path / "garbage.ifc"
    p.write_text("STEP; not really")
    with pytest.raises(IfcSchemaError):
        run_ifc_compliance(str(p), clauses=[])


def test_run_ifc_compliance_attaches_schema_stage_on_valid_fixture():
    from tests.helpers import run_ifc_compliance
    result, bim = run_ifc_compliance(FIXTURE, clauses=[])
    schema = bim.get("_schema")
    assert schema and schema["stage"] == "schema"
    assert schema["status"] in ("passed", "passed_with_alerts")


# ── contract sanity ──────────────────────────────────────────────────────────

def test_blocking_codes_are_the_documented_set():
    assert BLOCKING_CODES == {
        "IFC-SCHEMA-001", "IFC-SCHEMA-002", "IFC-SCHEMA-003",
        "IFC-SCHEMA-004", "IFC-SCHEMA-005", "IFC-SCHEMA-006",
        "IFC-SCHEMA-007", "IFC-SCHEMA-008", "IFC-SCHEMA-010",
        "IFC-SCHEMA-011"}
