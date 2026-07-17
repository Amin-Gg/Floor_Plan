from .phase4_helpers import findings, model, room


def test_missing_area_reports_qc_space_004_and_dependency():
    row = next(item for item in findings(model(rooms=[room(area=None)]))
               if item["code"] == "QC-SPACE-004")
    assert "room_area" in row["details"]["blocks_capabilities"]


def test_declared_and_derived_area_mismatch_is_reported():
    rows = findings(model(rooms=[room(area=4.0)]))
    match = next(item for item in rows if item["code"] == "QC-SPACE-004")
    assert match["details"]["derived_area_m2"] == 9.0


def test_matching_area_is_clean_for_area_code():
    rows = findings(model(rooms=[room(area=9.0)]))
    assert "QC-SPACE-004" not in {item["code"] for item in rows}
