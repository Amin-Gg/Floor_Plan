from .phase4_helpers import codes, model, wall


def test_opening_past_wall_endpoint_is_place_007():
    payload = model(
        walls=[wall()],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [3900, 0, 0],
                "storey_id": "S1"}],
    )
    assert "QC-PLACE-007" in codes(payload, "QC-PLACE")


def test_centered_opening_within_wall_has_no_place_007():
    payload = model(
        walls=[wall()],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [2000, 0, 0],
                "storey_id": "S1"}],
    )
    assert "QC-PLACE-007" not in codes(payload, "QC-PLACE")
