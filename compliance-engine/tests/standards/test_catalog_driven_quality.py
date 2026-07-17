from copy import deepcopy

import yaml

from domain.identifiers import ElementIdentity
from domain.model import BuildingModel, ModelProvenance
from domain.elements import Door
from standards import catalog_api as catalog
from validation.quality import QualityContext, run_model_quality_checks
from validation.quality.checks.required_properties import RequiredPropertiesCheck


def _model():
    return BuildingModel(
        provenance=ModelProvenance(source_type="test", model_fingerprint="f" * 64),
        doors=[Door(identity=ElementIdentity(internal_id="D1"), width_mm=900, height_mm=None)],
    )


def test_yaml_required_for_changes_quality_dependency(tmp_path):
    raw = deepcopy(catalog.reload_catalog())
    raw["elements"]["door"]["properties"]["height_mm"]["required_for"] = ["custom.capability"]
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    catalog.reload_catalog(str(path))
    try:
        model = _model()
        result = run_model_quality_checks(
            model,
            context=QualityContext.from_model(model),
            checks=(RequiredPropertiesCheck(),),
        )
        finding = next(f for f in result.findings if f.code == "QC-PROP-001")
        assert finding.details["required_for"] == ["custom.capability"]
        assert finding.details["blocks_capabilities"] == ["custom.capability"]
    finally:
        catalog.reload_catalog()
