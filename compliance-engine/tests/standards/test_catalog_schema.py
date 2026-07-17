from copy import deepcopy

import pytest
import yaml

from standards.loaders import load_semantic_catalog


def test_default_catalog_is_versioned_and_typed():
    catalog = load_semantic_catalog(force_reload=True)
    assert catalog.version == "1.0"
    assert catalog.property("door", "width_mm").unit == "mm"
    assert "compliance.numeric.door_width" in catalog.property("door", "width_mm").required_for


def test_invalid_catalog_fails_at_load(tmp_path):
    raw = deepcopy(dict(load_semantic_catalog().raw))
    del raw["elements"]["door"]["properties"]["width_mm"]["unit"]
    # Missing optional unit is structurally allowed; make a real broken mapping.
    raw["elements"]["door"]["properties"]["width_mm"]["ifc_mappings"] = [
        {"pset": "does_not_exist", "property": "width"}
    ]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown pset key"):
        load_semantic_catalog(str(path), force_reload=True)


def test_missing_catalog_fails_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_semantic_catalog(str(tmp_path / "missing.yaml"), force_reload=True)
