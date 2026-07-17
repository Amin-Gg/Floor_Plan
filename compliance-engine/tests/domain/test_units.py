from domain.units import area_to_m2, length_to_mm


def test_explicit_unit_conversions():
    assert length_to_mm(2.1, "m") == 2100.0
    assert length_to_mm(90, "cm") == 900.0
    assert area_to_m2(12000000, "mm2") == 12.0


def test_unknown_or_missing_units_are_not_guessed():
    assert length_to_mm(2.1, None) is None
    assert length_to_mm(2.1, "feet") is None
    assert area_to_m2(None, "m2") is None
