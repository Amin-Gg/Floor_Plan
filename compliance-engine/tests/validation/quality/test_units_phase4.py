from validation.compliance.adapter import building_model_from_bim_data

from .phase4_helpers import codes, model, room, wall


def test_supported_metres_are_normalised_once_to_mm():
    payload = model(
        walls=[wall(start=(0, 0, 0), end=(4, 0, 0), height=3, thickness=.2)],
        rooms=[room(
            polygon=[[0, 0], [3, 0], [3, 3], [0, 3], [0, 0]],
            area=9,
        )],
        units={"length": "m", "area": "m2"},
    )
    typed = building_model_from_bim_data(payload)
    assert typed.walls[0].end.x == 4000
    assert typed.walls[0].height_mm == 3000
    assert typed.units == {"length": "mm", "area": "m2"}


def test_missing_units_emit_unit_001():
    payload = model(walls=[wall()])
    payload.pop("units")
    assert "QC-UNIT-001" in codes(payload)


def test_unsupported_units_emit_unit_002():
    payload = model(walls=[wall()], units={"length": "feet", "area": "acre"})
    assert "QC-UNIT-002" in codes(payload)
