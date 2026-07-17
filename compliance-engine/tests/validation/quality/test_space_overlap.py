from .phase4_helpers import codes, model, room


def test_overlapping_spaces_are_space_010():
    second = room(
        "R2",
        polygon=[[2000, 0], [5000, 0], [5000, 3000], [2000, 3000], [2000, 0]],
        area=9,
    )
    assert "QC-SPACE-010" in codes(model(rooms=[room(), second]))


def test_touching_spaces_do_not_count_as_overlap():
    second = room(
        "R2",
        polygon=[[3000, 0], [6000, 0], [6000, 3000], [3000, 3000], [3000, 0]],
        area=9,
    )
    assert "QC-SPACE-010" not in codes(model(rooms=[room(), second]))
