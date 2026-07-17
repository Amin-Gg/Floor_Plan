"""
tests/test_building_params.py
=============================
Manual 3D-modeling / building parameters: the values an operator asserts
because they cannot be measured from a 2D plan (wall/ceiling height, window
height, window sill height, door height, floor thickness).

Covers the full engine-side contract:

  * BimAdapter provenance — the ``_provided`` list written by Step 1 (or the
    IFC ingest) decides what counts as "user supplied"; recorded defaults do
    NOT. The wall_height / wall_height_mm spellings alias to the engine's
    ceiling_height_mm.
  * Honest verdict tagging — a room-height verdict driven by an engine
    default must say ENGINE DEFAULT, never "user building parameter".
  * IFC contract Pset — WallHeightMm etc. + BuildingParamsProvided written
    by Step 1's exporter are read back into bim_data["building_params"].
  * Pipeline threading — building_params given to run_ifc_compliance reach
    the checker and flip real verdicts.
"""

import pytest

from services.numeric_checker import NumericChecker, BimAdapter, Verdict


def _rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


BIM = {
    "walls": [], "doors": [], "windows": [], "stairs": [], "slabs": [],
    "rooms": [{"id": "R1", "category": "room_bedroom", "area_m2": 12.0,
               "polygon": _rect(0, 0, 4000, 3000),
               "dimensions": {"width_mm": 3000, "length_mm": 4000}}],
}


def _clause(article, obj, prop, comp, value, unit):
    # Mirrors the ingested corpus schema (see test_numeric_property_routing):
    # the measurable quantities live under "entities".
    return {"article_id": article, "rule_type": "numeric", "text_en": article,
            "entities": {"object": obj, "property": prop, "comparator": comp,
                         "value": value, "unit": unit, "condition": None}}


ROOM_H = _clause("ROOM-H", "dwelling_space", "ceiling height", ">=", 2.4, "m")


def _only(findings):
    assert len(findings) == 1, findings
    return findings[0]


# ── BimAdapter provenance ─────────────────────────────────────────────────────

def test_provided_list_decides_user_supplied():
    """Step 1 records ALL parameter values but marks only the asserted ones."""
    bim = {**BIM, "building_params": {
        "wall_height": 3000.0, "door_height": 2100.0,
        "_provided": ["wall_height"]}}
    a = BimAdapter(bim)
    assert a.param_is_user_supplied("ceiling_height_mm")   # via wall_height
    assert not a.param_is_user_supplied("door_height")     # recorded default
    assert a.ceiling_height_m() == 3.0


def test_recorded_defaults_are_not_user_supplied():
    bim = {**BIM, "building_params": {
        "wall_height": 2800.0, "_provided": []}}
    a = BimAdapter(bim)
    assert not a.param_is_user_supplied("ceiling_height_mm")
    # value is still used for the measurement (deliberate design: the
    # default produces a verdict, honestly tagged) …
    assert a.ceiling_height_m() == 2.8


def test_legacy_flat_dict_keeps_old_semantics():
    """Pre-contract bim_data without _provided: presence == supplied."""
    bim = {**BIM, "building_params": {"ceiling_height_mm": 3000}}
    a = BimAdapter(bim)
    assert a.param_is_user_supplied("ceiling_height_mm")
    assert a.ceiling_height_m() == 3.0


def test_wall_height_mm_spelling_aliases_to_ceiling():
    """The engine API's canonical _mm spelling drives the ceiling too."""
    a = BimAdapter(dict(BIM), building_params={"wall_height_mm": 3200.0})
    assert a.ceiling_height_m() == 3.2
    assert a.param_is_user_supplied("ceiling_height_mm")


def test_explicit_ceiling_beats_wall_height_alias():
    bim = {**BIM, "building_params": {
        "wall_height": 3000.0, "ceiling_height_mm": 2600.0,
        "_provided": ["wall_height", "ceiling_height_mm"]}}
    assert BimAdapter(bim).ceiling_height_m() == 2.6


def test_explicit_arg_overrides_bim_block():
    """API-supplied params (already validated) beat the IFC-carried ones."""
    bim = {**BIM, "building_params": {
        "wall_height": 2800.0, "_provided": ["wall_height"]}}
    a = BimAdapter(bim, building_params={"ceiling_height_mm": 3100.0})
    assert a.ceiling_height_m() == 3.1
    assert a.param_is_user_supplied("ceiling_height_mm")


# ── Honest verdict tagging ────────────────────────────────────────────────────

def test_default_driven_check_forces_needs_review():
    """Policy: unasserted ceiling -> NOT_EVALUATED (data absent), never a
    default-driven verdict. Stage 1: was NEEDS_REVIEW; missing required input
    is now a model-data problem, not a judgment call."""
    f = _only(NumericChecker(dict(BIM)).check_clause(ROOM_H))
    assert f.verdict == Verdict.NOT_EVALUATED
    assert "building_params.wall_height" in f.message
    assert f.element_id == "R1"


def test_user_param_verdict_tagged_user_supplied():
    bim = {**BIM, "building_params": {
        "wall_height": 3000.0, "_provided": ["wall_height"]}}
    f = _only(NumericChecker(bim).check_clause(ROOM_H))
    assert f.verdict == Verdict.PASS and f.measured == 3.0
    assert "user building parameter" in f.message
    assert "ENGINE DEFAULT" not in f.message


def test_user_param_can_fail_the_check():
    f = _only(NumericChecker(
        dict(BIM), building_params={"wall_height_mm": 2200.0}
    ).check_clause(ROOM_H))
    assert f.verdict == Verdict.FAIL and f.measured == 2.2
    assert "user building parameter" in f.message


def test_recorded_default_from_step1_block_forces_review():
    """Step 1 always embeds the value; if the operator never touched the
    stepper the check must degrade to NOT_EVALUATED, not verdict on it."""
    bim = {**BIM, "building_params": {
        "wall_height": 2800.0, "door_height": 2100.0, "_provided": []}}
    f = _only(NumericChecker(bim).check_clause(ROOM_H))
    assert f.verdict == Verdict.NOT_EVALUATED
    assert "building_params.wall_height" in f.message


# ── IFC contract Pset roundtrip (engine side) ─────────────────────────────────

def _make_param_ifc(tmp_path, provided="wall_height,window_sill_height"):
    """Minimal IFC with just the project + Pset_SimsysContract, mirroring
    exactly what Step 1's exporter writes (property names ARE the contract)."""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.root
    import ifcopenshell.api.unit
    import ifcopenshell.api.project
    import ifcopenshell.api.pset

    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(
        model, ifc_class="IfcProject", name="param test")
    ifcopenshell.api.unit.assign_unit(
        model, length={"is_metric": True, "raw": "MILLIMETRE"})
    pset = ifcopenshell.api.pset.add_pset(
        model, product=project, name="Pset_SimsysContract")
    ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={
        "ContractVersion": "bim-canonical-v1",
        "WallHeightMm": 3200.0,
        "DoorHeightMm": 2100.0,
        "WindowHeightMm": 1400.0,
        "WindowSillHeightMm": 950.0,
        "FloorThicknessMm": 200.0,
        "BuildingParamsProvided": provided,
    })
    out = tmp_path / "params.ifc"
    model.write(str(out))
    return str(out)


def test_ingest_reads_params_from_contract_pset(tmp_path):
    from ingest.ifc_to_bim_data import ifc_to_bim_data
    bim = ifc_to_bim_data(_make_param_ifc(tmp_path))
    bp = bim["building_params"]
    assert bp["wall_height"] == 3200.0
    assert bp["ceiling_height_mm"] == 3200.0          # contract alias
    assert bp["window_height"] == 1400.0
    assert bp["window_sill_height"] == 950.0
    assert set(bp["_provided"]) == {"wall_height", "window_sill_height",
                                    "ceiling_height_mm"}


def test_ingest_marks_nothing_provided_when_all_defaults(tmp_path):
    from ingest.ifc_to_bim_data import ifc_to_bim_data
    bim = ifc_to_bim_data(_make_param_ifc(tmp_path, provided=""))
    assert bim["building_params"]["_provided"] == []
    # …and the checker consequently tags a room-height verdict as default.
    bim.update(rooms=BIM["rooms"])
    f = _only(NumericChecker(bim).check_clause(ROOM_H))
    assert f.verdict == Verdict.NOT_EVALUATED
    assert "building_params.wall_height" in f.message


def test_ingest_pset_absent_yields_empty_params(tmp_path):
    """Old Step-1 IFCs (pre-params contract) must keep working."""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.root
    import ifcopenshell.api.unit
    import ifcopenshell.api.project
    model = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="old")
    ifcopenshell.api.unit.assign_unit(
        model, length={"is_metric": True, "raw": "MILLIMETRE"})
    out = tmp_path / "old.ifc"
    model.write(str(out))

    from ingest.ifc_to_bim_data import ifc_to_bim_data
    bp = ifc_to_bim_data(str(out))["building_params"]
    assert bp.get("wall_height") is None
    assert bp.get("_provided", []) == []


def test_end_to_end_params_flip_verdict_through_pset(tmp_path):
    """The full engine-side path: contract Pset -> ingest -> adapter ->
    a real verdict that changes with the asserted wall height."""
    from ingest.ifc_to_bim_data import ifc_to_bim_data

    def _run(ifc):
        bim = ifc_to_bim_data(ifc)
        bim.update(rooms=BIM["rooms"])
        return _only(NumericChecker(bim).check_clause(
            _clause("ROOM-H", "dwelling_space", "ceiling height",
                    ">=", 3.0, "m")))

    tall = _make_param_ifc(tmp_path / "a" if False else tmp_path)
    f = _run(tall)                       # WallHeightMm = 3200 → PASS
    assert f.verdict == Verdict.PASS and f.measured == 3.2
    assert "user building parameter" in f.message


# ── Unified-pipeline merge point (provenance regression) ─────────────────────

def test_pipeline_manual_inputs_preserve_operator_provenance():
    """Manual Inputs v1 is the only supported operator-input path.

    The resolved value must reach the deterministic checker with user
    provenance; no low-level compliance function may merge flat parameters.
    """
    from services.validation_pipeline import PipelineRequest, run_validation_pipeline

    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data={**BIM, "rooms": [dict(BIM["rooms"][0])]},
        clauses=[ROOM_H],
        generate_reports=False,
        manual_inputs={
            "schema_version": "1.0",
            "defaults": {"wall_height_mm": 2250.0},
        },
    ))
    room_h = [f for f in execution.compliance.findings if f.article_id == "ROOM-H"]
    assert room_h, execution.compliance.findings
    finding = room_h[0]
    assert finding.verdict == Verdict.FAIL and finding.measured == 2.25, finding.message
    assert "user building parameter" in finding.message, finding.message
    assert "ENGINE DEFAULT" not in finding.message


def test_pipeline_without_manual_input_keeps_height_not_evaluated():
    from services.validation_pipeline import PipelineRequest, run_validation_pipeline

    execution = run_validation_pipeline(PipelineRequest(
        source_type="bim_data",
        bim_data={**BIM, "rooms": [dict(BIM["rooms"][0])]},
        clauses=[ROOM_H],
        generate_reports=False,
    ))
    finding = [f for f in execution.compliance.findings if f.article_id == "ROOM-H"][0]
    assert finding.verdict == Verdict.NOT_EVALUATED, finding.message
    assert "building_params.wall_height" in finding.message
