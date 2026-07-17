from domain.identifiers import build_element_identity, fingerprint_data
from validation.compliance.adapter import building_model_from_bim_data, building_model_to_bim_data


def test_ifc_guid_source_and_internal_ids_are_distinct_and_preserved():
    data = {"walls": [{"id": "detector-W1", "ifc_guid": "IFC-W1",
                       "start_point": [0, 0, 0], "end_point": [1000, 0, 0]}]}
    model = building_model_from_bim_data(data, model_fingerprint="f" * 64,
                                       source_type="ifc")
    identity = model.walls[0].identity
    assert identity.ifc_guid == "IFC-W1"
    assert identity.source_id == "detector-W1"
    assert identity.internal_id not in {identity.ifc_guid, identity.source_id}
    out = building_model_to_bim_data(model)["walls"][0]
    assert out["ifc_guid"] == "IFC-W1"
    assert out["source_id"] == "detector-W1"
    assert out["internal_id"] == identity.internal_id


def test_internal_id_is_deterministic_for_same_input():
    a = build_element_identity(model_fingerprint="a" * 64, element_type="door",
                               source_type="ifc", ifc_guid="GUID")
    b = build_element_identity(model_fingerprint="a" * 64, element_type="door",
                               source_type="ifc", ifc_guid="GUID")
    c = build_element_identity(model_fingerprint="b" * 64, element_type="door",
                               source_type="ifc", ifc_guid="GUID")
    assert a.internal_id == b.internal_id
    assert a.internal_id != c.internal_id


def test_geometry_fallback_is_explicit():
    identity = build_element_identity(model_fingerprint="a" * 64,
                                      element_type="space",
                                      source_type="bim_data",
                                      geometry_key="space|L1|p:10,20")
    assert identity.used_geometry_fallback is True


def test_real_ifc_ingest_preserves_globalid_and_internal_id():
    import os
    import pytest
    pytest.importorskip("ifcopenshell")
    from ingest.ifc_to_bim_data import ifc_to_bim_data, ifc_to_building_model

    fixture = os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_plan.ifc")
    fixture = os.path.abspath(fixture)
    model = ifc_to_building_model(fixture)
    assert model.walls and model.spaces
    for element in [*model.walls, *model.doors, *model.windows, *model.spaces]:
        assert element.identity.ifc_guid
        assert element.identity.internal_id
        assert element.identity.internal_id != element.identity.ifc_guid

    legacy = ifc_to_bim_data(fixture)
    assert all(row["ifc_guid"] and row["internal_id"] for row in legacy["walls"])
    assert legacy["_model"]["model_fingerprint"] == model.provenance.model_fingerprint


def test_ifc_only_identity_does_not_duplicate_guid_into_source_id():
    data = {"walls": [{"id": "IFC-W1", "ifc_guid": "IFC-W1", "source_id": None,
                       "start_point": [0, 0, 0], "end_point": [1000, 0, 0]}]}
    model = building_model_from_bim_data(data, model_fingerprint="f" * 64,
                                       source_type="ifc")
    assert model.walls[0].identity.ifc_guid == "IFC-W1"
    assert model.walls[0].identity.source_id is None
