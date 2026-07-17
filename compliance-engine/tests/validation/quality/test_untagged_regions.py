from .phase4_helpers import codes, model, room


def test_uncovered_enclosed_region_is_space_008():
    payload = model(
        rooms=[room()],
        enclosed_regions=[{
            "id": "region-2",
            "polygon": [[4000, 0], [5000, 0], [5000, 1000], [4000, 1000], [4000, 0]],
        }],
    )
    assert "QC-SPACE-008" in codes(payload)


def test_covered_enclosed_region_is_not_reported():
    payload = model(
        rooms=[room()],
        enclosed_regions=[{
            "id": "region-1",
            "polygon": [[0, 0], [3000, 0], [3000, 3000], [0, 3000], [0, 0]],
        }],
    )
    assert "QC-SPACE-008" not in codes(payload)
