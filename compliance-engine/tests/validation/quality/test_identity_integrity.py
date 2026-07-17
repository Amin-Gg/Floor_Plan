"""Tests for the identity-integrity quality plugin (QC-IDENT-001).

Closes the ADR-001 obligation tracked since the Phase 1 review: using the
deterministic geometry-fallback identity must surface as a Quality alert,
because such elements cannot be targeted by manual-input element overrides
or BCF component selection.
"""
from __future__ import annotations

from validation.compliance.adapter import building_model_from_bim_data
from validation.quality.checker import run_model_quality_checks
from validation.quality.checks import IdentityIntegrityCheck
from validation.quality.context import QualityContext
from validation.quality.registry import DEFAULT_QUALITY_CHECKS

ROOM = {"id": "R1", "category": "bedroom", "area_m2": 10,
        "polygon": [[0, 0], [3, 0], [3, 4], [0, 4]]}
WALL = {"id": "W1", "start_point": [0, 0, 0], "end_point": [4000, 0, 0],
        "thickness": 200, "height": 3000}


def _model(windows):
    return building_model_from_bim_data(
        {"rooms": [ROOM], "walls": [WALL], "windows": windows})


def test_fallback_identity_emits_qc_ident_001():
    # window with neither id nor ifc_guid -> geometry-fallback identity
    model = _model([{"host_wall_id": "W1", "insertion_point": [500, 0, 1000],
                     "width": 1000, "height": 1500}])
    assert model.windows[0].identity.used_geometry_fallback is True

    result = run_model_quality_checks(model)
    hits = [f for f in result.findings if f.code == "QC-IDENT-001"]
    assert len(hits) == 1
    finding = hits[0]
    assert finding.severity.value == "alert"
    assert finding.element_internal_id == model.windows[0].identity.internal_id
    assert finding.element_ifc_guid is None
    assert "fallback" in finding.message
    assert finding.details.get("used_geometry_fallback") is True


def test_identified_elements_do_not_alert():
    model = _model([{"id": "Win1", "host_wall_id": "W1",
                     "insertion_point": [500, 0, 1000],
                     "width": 1000, "height": 1500}])
    assert model.windows[0].identity.used_geometry_fallback is False

    check = IdentityIntegrityCheck()
    context = QualityContext.from_model(model)
    assert check.applies_to(model, context) is False
    assert check.run(model, context) == []


def test_plugin_is_registered_and_non_blocking():
    names = {check.name for check in DEFAULT_QUALITY_CHECKS}
    assert "identity_integrity" in names
    plugin = next(c for c in DEFAULT_QUALITY_CHECKS
                  if c.name == "identity_integrity")
    assert plugin.blocking is False
    assert plugin.codes == ("QC-IDENT-001",)


def test_fallback_alert_alone_does_not_fail_the_stage():
    """Non-blocking by design: addressability degrades, measurements don't."""
    model = _model([{"host_wall_id": "W1", "insertion_point": [500, 0, 1000],
                     "width": 1000, "height": 1500}])
    check = IdentityIntegrityCheck()
    context = QualityContext.from_model(model)
    result = run_model_quality_checks(model, context=context, checks=[check])
    assert result.status in {"passed_with_alerts", "passed"}
    assert any(f.code == "QC-IDENT-001" for f in result.findings)


def test_bcf_export_skips_fallback_identity_without_ifc_guid(tmp_path):
    """BCF topics require a trustworthy IFC GlobalId.

    QC-IDENT-001 remains in JSON/HTML/PDF, while the BCF manifest records an
    explicit skip. No markup-only internal-ID topic or fabricated component is
    allowed.
    """
    import re
    import zipfile

    from reporting.bcf_exporter import export_bcf
    from reporting.report_model import build_validation_report

    model = _model([{"host_wall_id": "W1", "insertion_point": [500, 0, 1000],
                     "width": 1000, "height": 1500}])
    quality = run_model_quality_checks(model)
    ident_findings = [f for f in quality.findings if f.code == "QC-IDENT-001"]
    assert ident_findings

    report = build_validation_report(
        compliance={"summary": {}, "findings": [], "duration_s": 0.0},
        schema={"stage": "schema", "status": "passed", "findings": []},
        quality=quality,
        model=model,
        metadata={"plan_name": "fallback-test"},
        generated_at="2026-07-10T12:00:00Z",
        run_id="22222222-2222-4222-8222-222222222222",
    )
    exported = export_bcf(report, model, tmp_path / "issues.bcf")

    with zipfile.ZipFile(exported.path) as archive:
        guids = set()
        markups = [name for name in archive.namelist() if name.endswith("/markup.bcf")]
        for name in archive.namelist():
            if name.endswith(".bcfv"):
                text = archive.read(name).decode("utf-8", "replace")
                guids |= set(re.findall(r'IfcGuid="([^"]+)"', text))

    ident_ids = {finding.finding_id for finding in ident_findings}
    assert not any(topic.finding_id in ident_ids for topic in exported.topics)
    skipped = [row for row in exported.skipped if row.get("finding_id") in ident_ids]
    assert len(skipped) == len(ident_ids)
    assert all("no trustworthy IFC GlobalId" in row["reason"] for row in skipped)
    assert guids == set()
    assert markups == []
    assert exported.to_dict()["component_selection_topics"] == 0
