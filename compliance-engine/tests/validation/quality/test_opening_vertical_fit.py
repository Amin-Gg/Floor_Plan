from .phase4_helpers import codes, model, wall


def test_door_taller_than_wall_is_place_011():
    payload = model(
        walls=[wall(height=2000)],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [2000, 0, 0],
                "storey_id": "S1"}],
    )
    assert "QC-PLACE-011" in codes(payload, "QC-PLACE")


def test_window_top_above_wall_is_place_011():
    payload = model(
        walls=[wall(height=2500)],
        windows=[{"id": "N1", "host_wall_id": "W1", "width": 1000,
                  "height": 1500, "sill_height": 1200,
                  "insertion_point": [2000, 0, 1200],
                  "storey_id": "S1", "is_exterior": True}],
    )
    assert "QC-PLACE-011" in codes(payload, "QC-PLACE")
