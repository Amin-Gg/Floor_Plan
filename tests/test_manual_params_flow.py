"""
tests/test_manual_params_flow.py
================================
Manual 3D-modeling parameters, end to end on the Section-1 side:

    validate (utils/validators + schemas)
      -> BimDataBuilder embeds building_params (+ _provided) in bim_data
        -> ifc_exporter writes them into Pset_SimsysContract
          -> the ENGINE's production loader reads them back
             (via tests/_engine_modules — the single source of truth)

The parameters are the values an operator must assert because a 2D plan
cannot yield them: wall_height (FFL -> underside of slab above == clear
ceiling height), window_height, window_sill_height, door_height,
floor_thickness.
"""

import pytest

from schemas import BuildingParams
from services.bim_builder import BimDataBuilder
from utils.error_handlers import ValidationError
from utils.validators import validate_building_params


# ── validation ────────────────────────────────────────────────────────────────

def test_partial_params_pass_validation():
    out = validate_building_params({"wall_height": 3000})
    assert out == {"wall_height": 3000.0}


def test_out_of_range_rejected():
    with pytest.raises(ValidationError):
        validate_building_params({"window_sill_height": 5000})


def test_cross_field_window_head_above_ceiling_rejected():
    # sill 1500 + height 1400 = 2900 > wall 2400
    with pytest.raises(ValidationError):
        validate_building_params({"wall_height": 2400,
                                  "window_sill_height": 1500,
                                  "window_height": 1400})


def test_cross_field_uses_exporter_defaults_for_missing_keys():
    # sill 1900 + default window_height 1200 = 3100 > default wall 2800
    with pytest.raises(ValidationError):
        validate_building_params({"window_sill_height": 1900})


def test_cross_field_door_taller_than_wall_rejected():
    with pytest.raises(ValidationError):
        validate_building_params({"wall_height": 2000, "door_height": 2100})


def test_schema_model_enforces_same_cross_field_rules():
    with pytest.raises(ValueError):
        BuildingParams(wall_height=2400, window_sill_height=1500,
                       window_height=1400)
    ok = BuildingParams(wall_height=3200, window_sill_height=950,
                        window_height=1400)
    assert ok.wall_height == 3200


# ── BimDataBuilder embeds the block ──────────────────────────────────────────

def _build_bim(params):
    builder = BimDataBuilder(params)
    return builder.build(
        wall_parameters=[{"wall_id": 1,
                          "centerline": [[0, 0], [5000, 0]],
                          "thickness": {"average": 200}}],
        detailed_doors=[],
        detailed_windows=[{
            "window_id": 1, "host_wall_id": 1,
            "location": {"center": {"x": 2500, "y": 0}},
            "dimensions": {"width": 1200}, "window_type": "single",
        }],
        room_polygons=[], bim_stairs=[], bim_slabs=[],
        exterior_walls=[{"wall_id": 1}],
        scale={"mm_per_pixel": 25.0, "source": "user"},
    )


def test_bim_data_carries_params_and_provenance():
    bim = _build_bim({"wall_height": 3200, "window_sill_height": 950})
    bp = bim["building_params"]
    assert bp["wall_height"] == 3200.0
    assert bp["window_sill_height"] == 950.0
    assert bp["window_height"] == 1200.0            # recorded default
    assert bp["_provided"] == ["wall_height", "window_sill_height"]


def test_bim_data_marks_nothing_provided_on_defaults():
    bim = _build_bim(None)
    assert bim["building_params"]["_provided"] == []
    assert bim["building_params"]["wall_height"] == 2800.0


def test_window_elements_carry_param_heights():
    """Window height/sill in bim_data ARE the manual parameters — the engine
    measures window rules against these element fields."""
    bim = _build_bim({"window_height": 1400, "window_sill_height": 950})
    w = bim["windows"][0]
    assert w["height"] == 1400.0 and w["sill_height"] == 950.0


# ── exporter Pset + production-engine roundtrip ───────────────────────────────

def test_exporter_writes_contract_pset_and_engine_reads_it(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from export.ifc_exporter import bim_json_to_ifc
    from _engine_modules import ifc_to_bim_data  # production engine loader

    bim = _build_bim({"wall_height": 3200, "window_sill_height": 950,
                      "window_height": 1400})
    out = tmp_path / "plan.ifc"
    bim_json_to_ifc(bim, output_path=str(out))

    # 1. Raw contract properties present on the project.
    model = ifcopenshell.open(str(out))
    import ifcopenshell.util.element as ue
    psets = ue.get_psets(model.by_type("IfcProject")[0])
    contract = psets["Pset_SimsysContract"]
    assert contract["WallHeightMm"] == 3200.0
    assert contract["WindowSillHeightMm"] == 950.0
    assert contract["WindowHeightMm"] == 1400.0
    assert contract["DoorHeightMm"] == 2100.0       # recorded default
    assert (contract["BuildingParamsProvided"]
            == "wall_height,window_height,window_sill_height")

    # 2. The engine's production loader recovers them with provenance.
    recon = ifc_to_bim_data(str(out))
    bp = recon["building_params"]
    assert bp["wall_height"] == 3200.0
    assert bp["ceiling_height_mm"] == 3200.0        # contract alias
    assert set(bp["_provided"]) == {"wall_height", "window_height",
                                    "window_sill_height", "ceiling_height_mm"}

    # 3. And the window ELEMENT geometry itself carries the manual values.
    win = recon["windows"][0]
    assert win["height"] == pytest.approx(1400.0, abs=1.0)
    assert win["sill_height"] == pytest.approx(950.0, abs=1.0)


def test_direct_geometry_override_is_rejected_and_manual_inputs_reexport_works(tmp_path):
    pytest.importorskip("ifcopenshell")
    import ifcopenshell
    import ifcopenshell.util.element as ue
    from export.ifc_exporter import IfcExportError, bim_json_to_ifc
    from stage1_contracts import build_measurement_provenance, resolve_manual_inputs

    bim = _build_bim({"wall_height": 3000})
    with pytest.raises(IfcExportError, match="Manual Inputs v1"):
        bim_json_to_ifc(
            bim, building_params={"wall_height": 3400},
            output_path=str(tmp_path / "blocked.ifc"),
        )

    resolved, _ = resolve_manual_inputs(bim, {
        "schema_version": "1.0",
        "defaults": {"wall_height_mm": 3400},
    })
    resolved = build_measurement_provenance(
        resolved, context={"model_version": "test", "weight_version": "test"}
    )
    out = tmp_path / "plan.ifc"
    bim_json_to_ifc(resolved, output_path=str(out))
    contract = ue.get_psets(
        ifcopenshell.open(str(out)).by_type("IfcProject")[0]
    )["Pset_SimsysContract"]
    assert contract["WallHeightMm"] == 3400.0


def test_legacy_bim_without_block_exports_defaults_unprovided(tmp_path):
    """Old bim_data (no building_params key) must still export, with defaults
    recorded and NOTHING marked as operator-provided."""
    pytest.importorskip("ifcopenshell")
    import ifcopenshell
    import ifcopenshell.util.element as ue
    from export.ifc_exporter import bim_json_to_ifc

    legacy = {"walls": [{"id": "w1", "start_point": [0, 0, 0],
                         "end_point": [4000, 0, 0], "thickness": 200,
                         "height": 2800}],
              "doors": [], "windows": [], "rooms": [], "stairs": [], "slabs": []}
    out = tmp_path / "legacy.ifc"
    bim_json_to_ifc(legacy, output_path=str(out))
    contract = ue.get_psets(
        ifcopenshell.open(str(out)).by_type("IfcProject")[0]
    )["Pset_SimsysContract"]
    assert contract["WallHeightMm"] == 2800.0
    assert contract["BuildingParamsProvided"] == ""


# ── Per-window width overrides (decision 2026-07: windows not standardized) ──

from utils.error_handlers import ValidationError as _VE
from services.bim_builder import apply_window_overrides


def test_window_override_applies_and_stamps_provenance():
    bim = _build_bim(None)
    wid = bim["windows"][0]["id"]
    assert bim["windows"][0]["width_source"] == "measured"
    apply_window_overrides(bim, {wid: {"width": 1450}})
    assert bim["windows"][0]["width"] == 1450.0
    assert bim["windows"][0]["width_source"] == "user"
    assert bim["window_overrides_applied"] == [wid]


def test_window_override_unknown_id_rejected():
    bim = _build_bim(None)
    with pytest.raises(_VE):
        apply_window_overrides(bim, {"Window_999": {"width": 1400}})


def test_window_override_out_of_range_rejected():
    bim = _build_bim(None)
    wid = bim["windows"][0]["id"]
    with pytest.raises(_VE):
        apply_window_overrides(bim, {wid: {"width": 9000}})


def test_window_override_unknown_field_rejected():
    """Heights/sills are global building_params by design — a per-window
    'height' must be rejected loudly, not silently dropped."""
    bim = _build_bim(None)
    wid = bim["windows"][0]["id"]
    with pytest.raises(_VE):
        apply_window_overrides(bim, {wid: {"height": 1600}})


def test_window_override_noop_on_empty():
    bim = _build_bim(None)
    apply_window_overrides(bim, None)
    apply_window_overrides(bim, {})
    assert bim["windows"][0]["width_source"] == "measured"
    assert "window_overrides_applied" not in bim


def test_window_override_rides_ifc_provenance_roundtrip(tmp_path):
    """Overridden width AND its 'user' provenance survive the IFC leg into
    the production engine loader."""
    pytest.importorskip("ifcopenshell")
    from export.ifc_exporter import bim_json_to_ifc
    from _engine_modules import ifc_to_bim_data

    bim = _build_bim(None)
    wid = bim["windows"][0]["id"]
    apply_window_overrides(bim, {wid: {"width": 1450}})
    out = tmp_path / "plan.ifc"
    bim_json_to_ifc(bim, output_path=str(out))

    recon = ifc_to_bim_data(str(out))
    win = recon["windows"][0]
    assert win["width"] == pytest.approx(1450.0, abs=1.0)
    assert win["width_source"] == "user"


def test_measured_windows_roundtrip_as_measured(tmp_path):
    pytest.importorskip("ifcopenshell")
    from export.ifc_exporter import bim_json_to_ifc
    from _engine_modules import ifc_to_bim_data

    bim = _build_bim(None)
    out = tmp_path / "plan.ifc"
    bim_json_to_ifc(bim, output_path=str(out))
    assert ifc_to_bim_data(str(out))["windows"][0]["width_source"] == "measured"
