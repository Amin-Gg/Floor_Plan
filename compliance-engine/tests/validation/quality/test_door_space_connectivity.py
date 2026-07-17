from .phase4_helpers import codes, model, room, wall


def _adjacent_rooms():
    return [
        room("R1", polygon=[[0, 0], [2000, 0], [2000, 3000], [0, 3000], [0, 0]], area=6),
        room("R2", polygon=[[2000, 0], [4000, 0], [4000, 3000], [2000, 3000], [2000, 0]], area=6),
    ]


def test_door_on_shared_boundary_derives_two_spaces():
    payload = model(
        rooms=_adjacent_rooms(),
        walls=[wall("W1", start=(2000, 0, 0), end=(2000, 3000, 0))],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [2000, 1500, 0],
                "storey_id": "S1"}],
    )
    assert "QC-SPACE-009" not in codes(payload)


def test_door_without_space_connectivity_is_space_009():
    payload = model(
        rooms=[room()],
        walls=[wall("W1", start=(5000, 0, 0), end=(5000, 3000, 0))],
        doors=[{"id": "D1", "host_wall_id": "W1", "width": 900,
                "height": 2100, "insertion_point": [5000, 1500, 0],
                "storey_id": "S1"}],
    )
    assert "QC-SPACE-009" in codes(payload)
