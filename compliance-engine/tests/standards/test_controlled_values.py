import pytest

from ingest.category_normalizer import normalize_controlled_value
from standards.loaders import load_controlled_values


def test_controlled_values_are_versioned_and_multilingual():
    assert load_controlled_values(force_reload=True).version == "1.0"
    assert normalize_controlled_value("room_types", "Kitchen").canonical_value == "room_kitchen"
    assert normalize_controlled_value("room_types", "آشپزخانه").canonical_value == "room_kitchen"
    assert normalize_controlled_value("boolean", "بله").canonical_value == "true"


def test_unknown_value_is_preserved_not_guessed():
    result = normalize_controlled_value("room_types", "experimental pod")
    assert result.raw_value == "experimental pod"
    assert result.canonical_value is None
    assert result.source == "unmapped"


def test_extra_alias_target_must_exist():
    with pytest.raises(ValueError, match="unknown room_types value"):
        normalize_controlled_value(
            "room_types", "x", extra_aliases={"x": "room_not_real"}
        )


def test_duplicate_alias_in_controlled_values_fails_loud(tmp_path):
    import yaml
    from standards.loaders import load_controlled_values

    raw = {
        "version": "x",
        "vocabularies": {
            "demo": {
                "values": {
                    "a": {"aliases": ["same"]},
                    "b": {"aliases": ["same"]},
                }
            }
        },
    }
    path = tmp_path / "controlled.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="maps to both"):
        load_controlled_values(str(path), force_reload=True)
