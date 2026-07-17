from .phase4_helpers import codes, model, room, wall


def test_declared_and_geometric_door_spaces_mismatch_is_place_010():
    rooms = [
        room("R1", polygon=[[0, 0], [2000, 0], [2000, 3000], [0, 3000], [0, 0]], area=6),
        room("R2", polygon=[[2000, 0], [4000, 0], [4000, 3000], [2000, 3000], [2000, 0]], area=6),
    ]
    payload = model(
        rooms=rooms,
        walls=[wall("W1", start=(2000, 0, 0), end=(2000, 3000, 0))],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [2000, 1500, 0],
                "connected_space_ids": ["R1"], "storey_id": "S1"}],
    )
    assert "QC-PLACE-010" in codes(payload, "QC-PLACE")
