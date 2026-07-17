from domain.model import BuildingModel
from validation.compliance.adapter import building_model_from_bim_data


def test_building_model_created_from_legacy_contract():
    bim = {
        "units": {"length": "mm", "area": "m2"},
        "walls": [{"id": "W1", "start_point": [0, 0, 0],
                   "end_point": [4000, 0, 0], "thickness": 200,
                   "height": 3000}],
        "doors": [], "windows": [], "rooms": [],
        "building_params": {"wall_height": 3000, "_provided": ["wall_height"]},
    }
    model = building_model_from_bim_data(bim, model_name="plan")
    assert isinstance(model, BuildingModel)
    assert model.provenance.model_name == "plan"
    assert len(model.walls) == 1
    assert model.walls[0].start.x == 0
    assert model.walls[0].end.x == 4000
    assert model.parameters.values["wall_height"] == 3000
    assert "wall_height" in model.parameters.provided
