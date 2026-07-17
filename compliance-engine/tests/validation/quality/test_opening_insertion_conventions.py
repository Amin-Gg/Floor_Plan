from .phase4_helpers import codes, model, wall


def test_start_offset_is_normalised_to_center_before_endpoint_check():
    payload = model(
        walls=[wall()],
        doors=[{
            "id": "D1", "host_wall_id": "W1", "width": 900,
            "height": 2100, "insertion_point": [0, 0, 0],
            "insertion_offset_mm": 3600,
            "insertion_convention": "start",
            "storey_id": "S1",
        }],
    )
    assert "QC-PLACE-007" in codes(payload, "QC-PLACE")


def test_end_offset_at_wall_end_normalises_to_valid_center():
    payload = model(
        walls=[wall()],
        doors=[{
            "id": "D1", "host_wall_id": "W1", "width": 900,
            "height": 2100, "insertion_point": [0, 0, 0],
            "insertion_offset_mm": 4000,
            "insertion_convention": "end",
            "storey_id": "S1",
        }],
    )
    assert "QC-PLACE-007" not in codes(payload, "QC-PLACE")
