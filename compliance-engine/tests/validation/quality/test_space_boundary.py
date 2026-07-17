from .phase4_helpers import codes, model, room


def test_missing_boundary_is_space_005():
    assert "QC-SPACE-005" in codes(model(rooms=[room(polygon=[])]))


def test_open_boundary_is_space_006():
    polygon = [[0, 0], [3000, 0], [3000, 3000], [0, 3000]]
    assert "QC-SPACE-006" in codes(model(rooms=[room(polygon=polygon)]))


def test_self_intersecting_boundary_is_space_006():
    polygon = [[0, 0], [3000, 3000], [0, 3000], [3000, 0], [0, 0]]
    assert "QC-SPACE-006" in codes(model(rooms=[room(polygon=polygon)]))


def test_valid_boundary_has_no_005_or_006():
    result = set(codes(model(rooms=[room()])))
    assert not {"QC-SPACE-005", "QC-SPACE-006"} & result


def test_invalid_boundary_marks_quality_stage_failed():
    from tests.helpers import run_quality_checks
    polygon = [[0, 0], [3000, 3000], [0, 3000], [3000, 0], [0, 0]]
    stage = run_quality_checks(model(rooms=[room(polygon=polygon)]))
    assert stage["status"] == "failed"
