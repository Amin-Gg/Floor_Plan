from .phase4_helpers import codes, model, wall


def test_degenerate_host_geometry_is_place_008():
    payload = model(
        walls=[wall(start=(0, 0, 0), end=(0, 0, 0))],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [0, 0, 0],
                "storey_id": "S1"}],
    )
    assert "QC-PLACE-008" in codes(payload, "QC-PLACE")
