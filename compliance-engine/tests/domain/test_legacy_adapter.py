from validation.compliance.adapter import building_model_from_bim_data, building_model_to_bim_data


def _legacy():
    return {
        "schema_version": "bim-canonical-v1",
        "units": {"length": "mm", "area": "m2"},
        "scale": {"mm_per_pixel": None, "source": "ifc", "confidence": None},
        "building_params": {},
        "walls": [{
            "id": "wall-source-1", "ifc_guid": "2WALLGUID",
            "start_point": [0.0, 0.0, 0.0], "end_point": [3000.0, 0.0, 0.0],
            "thickness": 200.0, "height": 2800.0,
            "_provenance": {"id": "wall-source-1", "confidence": 0.91},
        }],
        "doors": [], "windows": [],
        "rooms": [{
            "id": "R1", "ifc_guid": "2ROOMGUID", "name": "Bedroom",
            "category": "room_bedroom", "category_raw": "اتاق خواب",
            "area_m2": None, "polygon": [], "dimensions": {},
            "_provenance": {"id": "R1", "source": "model", "confidence": 0.8},
        }],
        "stairs": [], "slabs": [],
        "custom_top_level": {"keep": True},
    }


def test_round_trip_preserves_verdict_driving_semantics_and_provenance():
    model = building_model_from_bim_data(_legacy(), model_name="sample.ifc")
    out = building_model_to_bim_data(model)
    assert out["rooms"][0]["area_m2"] is None
    assert out["rooms"][0]["category"] == "room_bedroom"
    assert out["rooms"][0]["category_raw"] == "اتاق خواب"
    assert out["rooms"][0]["_provenance"]["confidence"] == 0.8
    assert out["walls"][0]["ifc_guid"] == "2WALLGUID"
    assert out["custom_top_level"] == {"keep": True}


def test_empty_building_params_remain_empty_not_synthetic_defaults():
    out = building_model_to_bim_data(building_model_from_bim_data(_legacy()))
    assert out["building_params"] == {}


def test_legacy_flat_building_params_keep_asserted_semantics():
    data = _legacy()
    data["building_params"] = {"ceiling_height_mm": 3000.0}
    model = building_model_from_bim_data(data)
    assert model.parameters.provided == {"ceiling_height_mm"}
    out = building_model_to_bim_data(model)
    assert out["building_params"] == {"ceiling_height_mm": 3000.0}
