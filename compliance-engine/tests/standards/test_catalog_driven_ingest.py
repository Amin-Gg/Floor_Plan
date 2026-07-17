from copy import deepcopy
from pathlib import Path

import yaml

from standards import catalog_api as catalog
from ingest.ifc_to_bim_data import ifc_to_bim_data


def test_custom_attribute_mapping_changes_ingest(tmp_path):
    raw = deepcopy(catalog.reload_catalog())
    # A deliberately absent attribute proves ingest follows the YAML mapping.
    raw["elements"]["door"]["properties"]["width_mm"]["ifc_mappings"] = [
        {"attribute": "NoSuchWidth", "source": "attribute"}
    ]
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    catalog.reload_catalog(str(path))
    try:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "sample_plan.ifc"
        data = ifc_to_bim_data(str(fixture))
        assert data["doors"]
        assert all(door["width"] == 0.0 for door in data["doors"])
    finally:
        catalog.reload_catalog()
