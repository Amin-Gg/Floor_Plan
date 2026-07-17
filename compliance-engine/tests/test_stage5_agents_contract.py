"""
Stage 5 — agent NOT_EVALUATED migration + contract-read hardening tests.

Invariants locked here:
  * Topology and opening agents emit verdict-level NOT_EVALUATED for data
    absence (no rooms of the categories a clause applies to; unmeasurable
    glazing) — never a message-sniffed NEEDS_REVIEW.
  * The safety stair case remains NEEDS_REVIEW: a missing stair on a possibly
    single-storey plan is an APPLICABILITY judgment, not missing data, and it
    no longer inflates blocked_by_missing_data.
  * Coverage's message-sniffed BLOCKED path is retired (verdict-level only).
  * A Pset_SimsysContract read failure is loud: logged, recorded in
    bim_data["_contract_read_error"], surfaced as QC-CONTRACT-001 — never a
    silent empty building_params (the Stage-4 shadowing bug class).
"""
from __future__ import annotations

import pytest

from numeric_checker import Verdict
from services.coverage import (BLOCKED, NEEDS_REVIEW, _BLOCKED_MARKERS,
                               classify_finding)
from tests.helpers import run_quality_checks


# ── agent migration ──────────────────────────────────────────────────────────

def _bim_no_rooms():
    return {"rooms": [], "walls": [], "doors": [], "windows": []}


def test_topology_no_rooms_is_not_evaluated():
    from spatial_graph import SpatialGraph
    from topology_agent import TopologyAgent
    clause = {"article_id": "T1", "rule_type": "spatial",
              "text_en": "kitchen must not open directly into WC",
              "entities": {"subject": "kitchen", "object": "toilet",
                           "relation": "must_not_connect_to"}}
    findings = TopologyAgent(SpatialGraph(_bim_no_rooms())).check_all([clause])
    f = [x for x in findings if x.article_id == "T1"][0]
    assert f.verdict == Verdict.NOT_EVALUATED
    assert "not evaluated" in f.message.lower()


def test_opening_no_habitable_rooms_is_not_evaluated():
    from spatial_graph import SpatialGraph
    from opening_agent import OpeningAgent
    clause = {"article_id": "O1", "rule_type": "numeric",
              "text_en": "glazing at least 1/8 of floor area",
              "entities": {"object": "window_area",
                           "property": "ratio_to_floor_area",
                           "comparator": ">=", "value": 0.125,
                           "unit": "ratio"}}
    findings = OpeningAgent(SpatialGraph(_bim_no_rooms())).check_all([clause])
    f = [x for x in findings if x.article_id == "O1"][0]
    assert f.verdict == Verdict.NOT_EVALUATED


def test_safety_missing_stair_stays_needs_review():
    """Applicability judgment, not data absence — Stage 5 semantic fix."""
    from spatial_graph import SpatialGraph
    from safety_agent import SafetyAgent
    clause = {"article_id": "S1", "rule_type": "numeric",
              "text_en": "stair minimum width 900 mm",
              "entities": {"object": "stair", "property": "width",
                           "comparator": ">=", "value": 900, "unit": "mm"}}
    bim = _bim_no_rooms()
    findings = SafetyAgent(SpatialGraph(bim), bim).check_all([clause])
    f = [x for x in findings if x.article_id == "S1"][0]
    assert f.verdict == Verdict.NEEDS_REVIEW
    # and coverage must NOT count it as blocked-by-missing-data any more
    assert classify_finding(f.verdict.value, f.message) == NEEDS_REVIEW


def test_blocked_markers_retired():
    assert _BLOCKED_MARKERS == ()
    assert classify_finding("NEEDS_REVIEW",
                            "Nothing measurable for 'x'") == NEEDS_REVIEW
    assert classify_finding("NOT_EVALUATED", "any wording") == BLOCKED


# ── contract-read hardening ──────────────────────────────────────────────────

def _param_ifc(tmp_path, break_it=False):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.root
    import ifcopenshell.api.unit
    import ifcopenshell.api.project
    import ifcopenshell.api.pset
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcProject", name="t")
    ifcopenshell.api.unit.assign_unit(
        model, length={"is_metric": True, "raw": "MILLIMETRE"})
    pset = ifcopenshell.api.pset.add_pset(
        model, product=project, name="Pset_SimsysContract")
    props = {"ContractVersion": "bim-canonical-v1", "WallHeightMm": 3200.0,
             "BuildingParamsProvided": "wall_height"}
    if break_it:
        # a non-numeric value where a float is required → float() raises
        props["WallHeightMm"] = "not-a-number"
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=props)
    p = tmp_path / "plan.ifc"
    model.write(str(p))
    return str(p)


def test_clean_contract_read_has_no_error(tmp_path):
    from ingest.ifc_to_bim_data import ifc_to_bim_data
    bim = ifc_to_bim_data(_param_ifc(tmp_path))
    assert bim["_contract_read_error"] is None
    assert bim["building_params"]["wall_height"] == 3200.0
    stage = run_quality_checks(bim)
    codes = [f["code"] for f in stage["findings"]]
    assert "QC-CONTRACT-001" not in codes


def test_broken_contract_read_is_loud_and_quality_visible(tmp_path, caplog):
    import logging
    from ingest.ifc_to_bim_data import ifc_to_bim_data
    with caplog.at_level(logging.WARNING):
        bim = ifc_to_bim_data(_param_ifc(tmp_path, break_it=True))
    assert bim["_contract_read_error"]           # recorded, not swallowed
    assert bim["building_params"] == {}          # never half-applied
    assert any("Pset_SimsysContract read failed" in r.message
               for r in caplog.records)          # logged
    stage = run_quality_checks(bim)
    codes = [f["code"] for f in stage["findings"]]
    assert "QC-CONTRACT-001" in codes            # operator-visible
