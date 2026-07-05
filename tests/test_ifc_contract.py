"""
tests/test_ifc_contract.py
==========================
IFC Interface Spec §8.1 — generate an IFC from a sample plan and assert the full
§4 acceptance checklist against the generated file:

    file-level   → valid IFC4, units declared (length + m² area), ContractVersion,
                   full Project→Site→Building→Storey hierarchy
    per element  → canonical entity, deterministic GlobalId, provenance Pset
                   (no nulls), standard Pset, OverallWidth/Height, Qto NetFloorArea,
                   real footprint, openings voided/filled
    geometry     → coordinates in the declared unit (mm) — guards the §9.1 fix

A file that fails any of these is, by definition, a failed build.
"""

import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")
import ifcopenshell.util.element as ue  # noqa: E402

from export.ifc_exporter import (  # noqa: E402
    bim_json_to_ifc, PROVENANCE_PSET, CONTRACT_PSET, CONTRACT_VERSION,
)
from validation import validate_ifc_contract  # noqa: E402

PROVENANCE_REQUIRED = ("OriginalId", "Source", "Confidence", "NeedsReview",
                       "ReviewReason")


def _sample_bim():
    """A small but complete plan: 4 walls forming a room, one door, one window."""
    return {
        "walls": [
            {"id": "w1", "start_point": [0, 0, 0], "end_point": [5000, 0, 0],
             "thickness": 200, "height": 2800, "type": "exterior", "is_exterior": True},
            {"id": "w2", "start_point": [5000, 0, 0], "end_point": [5000, 4000, 0],
             "thickness": 200, "height": 2800, "type": "exterior", "is_exterior": True},
            {"id": "w3", "start_point": [5000, 4000, 0], "end_point": [0, 4000, 0],
             "thickness": 200, "height": 2800, "type": "interior", "is_exterior": False},
            {"id": "w4", "start_point": [0, 4000, 0], "end_point": [0, 0, 0],
             "thickness": 200, "height": 2800, "type": "exterior", "is_exterior": True},
        ],
        "doors": [{"id": "d1", "host_wall_id": "w1", "insertion_point": [2500, 0, 0],
                   "width": 900, "height": 2100}],
        "windows": [{"id": "win1", "host_wall_id": "w2",
                     "insertion_point": [5000, 2000, 0], "width": 1200,
                     "height": 1000, "sill_height": 900}],
        "rooms": [{"id": "r1", "name": "Bedroom", "category": "Bedroom",
                   "polygon": [[0, 0], [5000, 0], [5000, 4000], [0, 4000], [0, 0]],
                   "area_m2": 20.0, "centroid_mm": [2500, 2000],
                   "name_source": "ocr", "needs_review": False}],
        "stairs": [], "slabs": [],
    }


@pytest.fixture(scope="module")
def ifc_path(tmp_path_factory):
    out = tmp_path_factory.mktemp("ifc") / "contract_plan.ifc"
    # If the exporter's §A7 gate fails, bim_json_to_ifc raises and the test errors.
    bim_json_to_ifc(_sample_bim(), {}, str(out))
    return str(out)


@pytest.fixture(scope="module")
def model(ifc_path):
    return ifcopenshell.open(ifc_path)


# ── file level ────────────────────────────────────────────────────────────────
def test_valid_ifc4(model):
    assert model.schema == "IFC4"


def test_units_declared(model):
    proj = model.by_type("IfcProject")[0]
    unit_types = {u.UnitType for u in proj.UnitsInContext.Units
                  if u.is_a("IfcNamedUnit")}
    assert "LENGTHUNIT" in unit_types
    assert "AREAUNIT" in unit_types     # explicit m² area unit (§9.1 decision)
    length = [u for u in proj.UnitsInContext.Units
              if getattr(u, "UnitType", None) == "LENGTHUNIT"][0]
    assert length.is_a("IfcSIUnit") and length.Name == "METRE"
    assert length.Prefix == "MILLI"     # millimetre length unit


def test_contract_version(model):
    proj = model.by_type("IfcProject")[0]
    cv = ue.get_psets(proj).get(CONTRACT_PSET, {}).get("ContractVersion")
    assert cv == CONTRACT_VERSION


def test_full_spatial_hierarchy(model):
    assert model.by_type("IfcProject")
    assert model.by_type("IfcSite")
    assert model.by_type("IfcBuilding")
    assert model.by_type("IfcBuildingStorey")


# ── per element ───────────────────────────────────────────────────────────────
def test_canonical_entities_present(model):
    assert len(model.by_type("IfcWall")) == 4
    assert len(model.by_type("IfcDoor")) == 1
    assert len(model.by_type("IfcWindow")) == 1
    assert len(model.by_type("IfcSpace")) == 1


@pytest.mark.parametrize("ifc_class", ["IfcWall", "IfcDoor", "IfcWindow", "IfcSpace"])
def test_every_element_has_guid_and_provenance(model, ifc_class):
    for el in model.by_type(ifc_class):
        assert el.GlobalId, f"{ifc_class} missing GlobalId"
        prov = ue.get_psets(el).get(PROVENANCE_PSET)
        assert prov is not None, f"{ifc_class} missing {PROVENANCE_PSET}"
        for field in PROVENANCE_REQUIRED:
            assert prov.get(field) is not None, f"{ifc_class}.{field} is null"


def test_standard_psets_present(model):
    assert ue.get_psets(model.by_type("IfcWall")[0]).get("Pset_WallCommon")
    assert ue.get_psets(model.by_type("IfcDoor")[0]).get("Pset_DoorCommon")
    assert ue.get_psets(model.by_type("IfcWindow")[0]).get("Pset_WindowCommon")
    assert ue.get_psets(model.by_type("IfcSpace")[0]).get("Pset_SpaceCommon")


def test_door_window_overall_dimensions(model):
    d = model.by_type("IfcDoor")[0]
    assert d.OverallWidth == 900 and d.OverallHeight == 2100
    w = model.by_type("IfcWindow")[0]
    assert w.OverallWidth == 1200 and w.OverallHeight == 1000


def test_openings_voided_and_filled(model):
    # every door/window fills an opening that voids a wall (where the C4 bug hid)
    for cls in ("IfcDoor", "IfcWindow"):
        for el in model.by_type(cls):
            fills = el.FillsVoids or []
            assert fills, f"{cls} {el.Name} fills no opening"
            opening = fills[0].RelatingOpeningElement
            assert opening.VoidsElements, "opening voids no wall"


def test_space_qto_area_and_footprint(model):
    sp = model.by_type("IfcSpace")[0]
    qto = ue.get_psets(sp, qtos_only=True).get("Qto_SpaceBaseQuantities", {})
    assert qto.get("NetFloorArea") == pytest.approx(20.0, abs=0.1)   # m²
    assert sp.Representation is not None                              # real footprint


def test_window_is_external_mirrored_from_host(model):
    # window win1 hosts on w2 (is_exterior True) → IsExternal True
    w = model.by_type("IfcWindow")[0]
    assert ue.get_psets(w).get("Pset_WindowCommon", {}).get("IsExternal") is True


# ── geometry fidelity (guards the §9.1 ×1000 fix) ─────────────────────────────
def test_geometry_is_millimetres(model):
    """Wall solids and placements must be in mm, not ×1000 (a 5 km building)."""
    import ifcopenshell.util.placement as P
    for w in model.by_type("IfcWall"):
        for r in (w.Representation.Representations if w.Representation else []):
            for it in r.Items:
                if it.is_a("IfcExtrudedAreaSolid"):
                    # wall extrude depth == storey height (2800 mm), not 2.8e6
                    assert 2000 < it.Depth < 4000, f"wall depth {it.Depth} not mm"
    # wall w2 starts at x=5000 mm exactly (not 5_000_000)
    w2 = [w for w in model.by_type("IfcWall") if w.Name == "w2"][0]
    origin = P.get_local_placement(w2.ObjectPlacement)[0][3]
    assert origin == pytest.approx(5000, abs=1)


def test_deterministic_guids(ifc_path, tmp_path):
    """Re-exporting the same bim_data yields identical GlobalIds (§A2)."""
    out2 = tmp_path / "again.ifc"
    bim_json_to_ifc(_sample_bim(), {}, str(out2))
    m1 = ifcopenshell.open(ifc_path)
    m2 = ifcopenshell.open(str(out2))
    g1 = {w.Name: w.GlobalId for w in m1.by_type("IfcWall")}
    g2 = {w.Name: w.GlobalId for w in m2.by_type("IfcWall")}
    assert g1 == g2 and len(g1) == 4


# ── the gate itself ───────────────────────────────────────────────────────────
def test_validate_ifc_contract_passes(ifc_path):
    report = validate_ifc_contract(ifc_path)
    assert not report.blocked, [i.code for i in report.issues
                                if i.severity.value == "critical"]
    assert report.n_critical == 0
