from copy import deepcopy

import yaml

from standards import catalog_api as catalog
from services import numeric_checker


def test_clause_property_vocabulary_and_units_come_from_catalog(tmp_path):
    raw = deepcopy(catalog.reload_catalog())
    raw["compliance"]["clause_properties"]["width"]["aliases"].append("clear span")
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    catalog.reload_catalog(str(path))
    try:
        assert numeric_checker._quantity_of("clear span") == "width"
        assert numeric_checker.NumericChecker._convert_scalar(
            900, "mm", "clear span"
        ) == 0.9
    finally:
        catalog.reload_catalog()
