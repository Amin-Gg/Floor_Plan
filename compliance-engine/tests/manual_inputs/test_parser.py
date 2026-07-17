import json
import math

import pytest

from manual_inputs import ManualInputsError, parse_manual_inputs


def test_parse_v1_nested_contract():
    parsed = parse_manual_inputs({
        "schema_version": "1.0",
        "project": {"default_storey_height_mm": 3200, "finished_floor_level_mm": 0},
        "defaults": {"window_width_mm": 1200, "window_height_mm": 1400},
        "element_overrides": {"windows": {"W-01": {"width_mm": 1500}}},
    })
    assert parsed.project.default_storey_height_mm == 3200
    assert parsed.defaults.window_width_mm == 1200
    assert parsed.element_overrides.windows["W-01"]["width_mm"] == 1500


def test_flat_contract_is_rejected():
    with pytest.raises(ManualInputsError, match="Unknown key"):
        parse_manual_inputs({"wall_height": 3000, "window_sill_height": 900})

@pytest.mark.parametrize("payload", [
    {"schema_version": "2.0"},
    {"schema_version": "1.0", "unknown": 1},
    {"schema_version": "1.0", "defaults": {"wall_height_mm": True}},
    {"schema_version": "1.0", "defaults": {"wall_height_mm": math.inf}},
    {"schema_version": "1.0", "element_overrides": {"windows": {"W": {"foo": 1}}}},
])
def test_strict_validation_rejects_invalid_payloads(payload):
    with pytest.raises(ManualInputsError):
        parse_manual_inputs(payload)


def test_rejects_invalid_json_string():
    with pytest.raises(ManualInputsError, match="not valid JSON"):
        parse_manual_inputs("{bad")


def test_cross_field_default_window_must_fit_wall():
    with pytest.raises(ManualInputsError, match="exceeds"):
        parse_manual_inputs({
            "schema_version": "1.0",
            "defaults": {
                "wall_height_mm": 2500,
                "window_sill_height_mm": 1200,
                "window_height_mm": 1500,
            },
        })


def test_wire_dict_is_json_serializable():
    parsed = parse_manual_inputs({"schema_version": "1.0", "defaults": {"wall_height_mm": 3000}})
    wire = parsed.to_wire_dict()
    json.dumps(wire)
    assert wire["defaults"]["wall_height_mm"] == 3000
