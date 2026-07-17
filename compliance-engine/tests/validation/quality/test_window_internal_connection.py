from .phase4_helpers import codes, model, room, wall


def test_window_connecting_two_spaces_is_place_009():
    rooms = [
        room("R1", polygon=[[0, 0], [2000, 0], [2000, 3000], [0, 3000], [0, 0]], area=6),
        room("R2", polygon=[[2000, 0], [4000, 0], [4000, 3000], [2000, 3000], [2000, 0]], area=6),
    ]
    payload = model(
        rooms=rooms,
        walls=[wall("W1", start=(2000, 0, 0), end=(2000, 3000, 0))],
        windows=[{"id": "N1", "host_wall_id": "W1", "width": 1000,
                  "height": 1200, "sill_height": 900,
                  "insertion_point": [2000, 1500, 900],
                  "storey_id": "S1", "is_exterior": False}],
    )
    assert "QC-PLACE-009" in codes(payload, "QC-PLACE")


def test_explicitly_allowed_internal_window_is_not_reported():
    rooms = [
        room("R1", polygon=[[0, 0], [2000, 0], [2000, 3000], [0, 3000], [0, 0]], area=6),
        room("R2", polygon=[[2000, 0], [4000, 0], [4000, 3000], [2000, 3000], [2000, 0]], area=6),
    ]
    payload = model(
        rooms=rooms,
        walls=[wall("W1", start=(2000, 0, 0), end=(2000, 3000, 0))],
        windows=[{"id": "N1", "host_wall_id": "W1", "width": 1000,
                  "height": 1200, "sill_height": 900,
                  "insertion_point": [2000, 1500, 900],
                  "storey_id": "S1", "is_exterior": False,
                  "allow_internal": True}],
    )
    assert "QC-PLACE-009" not in codes(payload, "QC-PLACE")
