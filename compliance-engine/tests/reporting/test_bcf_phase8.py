from __future__ import annotations

import json
import shutil
from dataclasses import replace
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from domain.elements import Door, Space, Storey, Wall, Window
from domain.geometry import Point2D, Polygon2D
from domain.identifiers import ElementIdentity
from domain.model import BuildingModel, BuildingParameters, ModelProvenance
from reporting.bcf_exporter import (
    BcfValidationError,
    export_bcf,
    topic_guid_for_finding,
    validate_bcf_archive,
    viewpoint_guid_for_finding,
)
from reporting.report_model import build_validation_report

PROJECT_GUID = "2w3eKGABvOuXObW6FJDOjs"
STOREY_GUID = "1lTt3YPMv9qO2p1Uh$abcD"
WALL_GUID = "0AAAAAAAAAAAAAAAAAAAAA"
DOOR_GUID = "1BBBBBBBBBBBBBBBBBBBBB"
WINDOW_GUID = "2CCCCCCCCCCCCCCCCCCCCC"
SPACE_GUID = "3DDDDDDDDDDDDDDDDDDDDD"


def _identity(internal: str, guid: str) -> ElementIdentity:
    return ElementIdentity(
        internal_id=internal,
        ifc_guid=guid,
        source_id=internal,
        model_name="phase8.ifc",
    )


def _model() -> BuildingModel:
    storey = Storey(
        identity=_identity("storey-1", STOREY_GUID),
        name="Storey 1",
        elevation_mm=0,
    )
    wall = Wall(
        identity=_identity("wall-1", WALL_GUID),
        storey_id="storey-1",
        start=Point2D(0, 0),
        end=Point2D(6000, 0),
        thickness_mm=200,
        height_mm=3200,
    )
    door = Door(
        identity=_identity("door-1", DOOR_GUID),
        storey_id="storey-1",
        width_mm=900,
        height_mm=2100,
        host_wall_id="wall-1",
        insertion_point=Point2D(1500, 0),
    )
    window = Window(
        identity=_identity("window-1", WINDOW_GUID),
        storey_id="storey-1",
        width_mm=1200,
        height_mm=1400,
        sill_height_mm=900,
        host_wall_id="wall-1",
        insertion_point=Point2D(4000, 0),
    )
    space = Space(
        identity=_identity("space-1", SPACE_GUID),
        storey_id="storey-1",
        name="Bedroom 1",
        canonical_type="bedroom",
        area_m2=24,
        boundary=Polygon2D((
            Point2D(0, 0), Point2D(6000, 0), Point2D(6000, 4000),
            Point2D(0, 4000), Point2D(0, 0),
        )),
        centroid=Point2D(3000, 2000),
    )
    return BuildingModel(
        provenance=ModelProvenance(
            source_type="ifc",
            model_fingerprint="f" * 64,
            model_name="phase8.ifc",
            ifc_schema="IFC4",
        ),
        project_id=PROJECT_GUID,
        storeys=[storey],
        walls=[wall],
        doors=[door],
        windows=[window],
        spaces=[space],
        parameters=BuildingParameters(),
    )


def _finding(*, guid: str | None, internal: str | None, verdict: str, code: str, stage: str = "quality"):
    return {
        "stage": stage,
        "category": stage,
        "code": code,
        "article_id": code,
        "clause_id": code if stage == "compliance" else None,
        "severity": "fail" if verdict == "FAIL" else "alert",
        "verdict": verdict,
        "message": f"Issue {code}",
        "requirement": "The element shall comply.",
        "expected": 1200,
        "actual": 900,
        "unit": "mm",
        "element_ifc_guid": guid,
        "element_internal_id": internal,
        "element_id": internal,
        "element_type": "Door",
    }


def _report():
    model = _model()
    quality = {
        "stage": "quality",
        "status": "passed_with_alerts",
        "findings": [
            _finding(guid=DOOR_GUID, internal="door-1", verdict="FAIL", code="QC-DOOR-001"),
            _finding(guid=WINDOW_GUID, internal="window-1", verdict="NOT_EVALUATED", code="QC-WIN-001"),
            _finding(guid=None, internal="legacy-1", verdict="NEEDS_REVIEW", code="QC-LEGACY-001"),
            _finding(guid=None, internal=None, verdict="FAIL", code="QC-GLOBAL-001"),
        ],
    }
    compliance = {
        "summary": {"PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 0, "NOT_EVALUATED": 0},
        "findings": [
            _finding(
                guid=SPACE_GUID,
                internal="space-1",
                verdict="FAIL",
                code="4-6-1",
                stage="compliance",
            )
        ],
        "duration_s": 0.1,
    }
    report = build_validation_report(
        compliance=compliance,
        schema={"stage": "schema", "status": "passed", "findings": []},
        quality=quality,
        model=model,
        metadata={"plan_name": "Phase 8"},
        generated_at="2026-07-10T12:00:00Z",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    return report, model


def test_bcf_archive_has_version_project_topics_viewpoints_and_snapshots(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    result = validate_bcf_archive(exported.path)
    assert result["version"] == "2.1"
    assert result["topics"] == 3  # only IFC-backed findings are eligible
    assert result["viewpoints"] == 3
    assert set(result["selected_ifc_guids"]) == {DOOR_GUID, WINDOW_GUID, SPACE_GUID}

    with zipfile.ZipFile(exported.path) as archive:
        names = set(archive.namelist())
        assert "bcf.version" in names
        assert "project.bcfp" in names
        assert sum(name.endswith("/markup.bcf") for name in names) == 3
        assert sum(name.endswith("/viewpoint.bcfv") for name in names) == 3
        assert sum(name.endswith("/snapshot.png") for name in names) == 3


def test_markup_and_viewpoint_use_real_ifc_guid_and_bcf21_shape(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    door_finding = next(
        finding for finding in report.findings if finding.get("element_ifc_guid") == DOOR_GUID
    )
    topic_guid = topic_guid_for_finding(door_finding["finding_id"])
    view_guid = viewpoint_guid_for_finding(door_finding["finding_id"])
    with zipfile.ZipFile(exported.path) as archive:
        markup = etree.fromstring(archive.read(f"{topic_guid}/markup.bcf"))
        assert markup.find("Topic").get("Guid") == topic_guid
        assert markup.find("Topic").get("TopicStatus") == "Open"
        labels = [node.text for node in markup.findall("./Topic/Labels")]
        assert "quality" in labels and "FAIL" in labels and "QC-DOOR-001" in labels
        link = markup.find("Viewpoints")
        assert link is not None and link.get("Guid") == view_guid
        assert link.findtext("Viewpoint") == "viewpoint.bcfv"
        assert link.findtext("Snapshot") == "snapshot.png"

        viewpoint = etree.fromstring(archive.read(f"{topic_guid}/viewpoint.bcfv"))
        selected = viewpoint.find("./Components/Selection/Component")
        assert selected is not None and selected.get("IfcGuid") == DOOR_GUID
        assert selected.findtext("AuthoringToolId") == "door-1"
        assert viewpoint.find("OrthogonalCamera") is not None
        assert viewpoint.find("./Components/Visibility").get("DefaultVisibility") == "true"


def test_topic_and_viewpoint_guids_are_stable_across_runs(tmp_path):
    report, model = _report()
    first = export_bcf(report, model, tmp_path / "a.bcf")
    second = export_bcf(report, model, tmp_path / "b.bcf")
    assert [(t.topic_guid, t.viewpoint_guid) for t in first.topics] == [
        (t.topic_guid, t.viewpoint_guid) for t in second.topics
    ]
    assert Path(first.path).read_bytes() == Path(second.path).read_bytes()


def test_untrusted_internal_reference_and_global_finding_are_skipped(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    manifest = json.loads(Path(exported.manifest_path).read_text(encoding="utf-8"))
    assert manifest["skipped_total"] == 2
    reasons = {row["finding_id"]: row["reason"] for row in manifest["skipped"]}
    legacy_finding = next(
        finding for finding in report.findings
        if finding.get("element_internal_id") == "legacy-1"
    )
    global_finding = next(
        finding for finding in report.findings
        if finding.get("code") == "QC-GLOBAL-001"
    )
    assert "no trustworthy IFC GlobalId" in reasons[legacy_finding["finding_id"]]
    assert reasons[global_finding["finding_id"]] == "global or unanchored finding"
    assert not any(topic.element_internal_id == "legacy-1" for topic in exported.topics)


def test_bcf_topic_count_matches_manifest_eligibility(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    manifest = json.loads(Path(exported.manifest_path).read_text(encoding="utf-8"))
    with zipfile.ZipFile(exported.path) as archive:
        markups = [name for name in archive.namelist() if name.endswith("/markup.bcf")]
    assert len(markups) == manifest["topics_total"]
    assert manifest["component_selection_topics"] == 3
    assert manifest["viewpoints_total"] == 3
    assert manifest["snapshot_topics"] == 3


def test_validator_rejects_missing_referenced_viewpoint(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "valid.bcf")
    broken = tmp_path / "broken.bcf"
    with zipfile.ZipFile(exported.path) as source, zipfile.ZipFile(broken, "w") as target:
        removed = False
        for info in source.infolist():
            if info.filename.endswith("/viewpoint.bcfv") and not removed:
                removed = True
                continue
            target.writestr(info, source.read(info.filename))
    with pytest.raises(BcfValidationError, match="missing viewpoint"):
        validate_bcf_archive(broken)


def test_snapshot_is_real_png(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    with zipfile.ZipFile(exported.path) as archive:
        snapshots = [name for name in archive.namelist() if name.endswith("snapshot.png")]
        assert snapshots
        for name in snapshots:
            payload = archive.read(name)
            assert payload.startswith(b"\x89PNG\r\n\x1a\n")
            assert len(payload) > 1000


def test_export_skips_ifc_guid_not_present_in_canonical_model(tmp_path):
    report, model = _report()
    missing_guid = "9ZZZZZZZZZZZZZZZZZZZZZ"
    report = replace(
        report,
        findings=report.findings + (
            {
                **_finding(
                    guid=missing_guid,
                    internal="missing-1",
                    verdict="FAIL",
                    code="QC-MISSING-GUID",
                ),
                "finding_id": "55555555-5555-4555-8555-555555555555",
            },
        ),
    )
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    manifest = json.loads(Path(exported.manifest_path).read_text(encoding="utf-8"))
    assert any(
        item["reason"] == "IFC GUID not found in canonical model"
        for item in manifest["skipped"]
    )
    assert missing_guid not in validate_bcf_archive(exported.path)["selected_ifc_guids"]


def test_validator_can_cross_check_selected_guids_against_source_model(tmp_path):
    report, model = _report()
    exported = export_bcf(report, model, tmp_path / "issues.bcf")
    allowed = {DOOR_GUID, WINDOW_GUID, SPACE_GUID}
    assert validate_bcf_archive(
        exported.path, allowed_ifc_guids=allowed
    )["viewpoints"] == 3
    with pytest.raises(BcfValidationError, match="not present in source model"):
        validate_bcf_archive(
            exported.path, allowed_ifc_guids={DOOR_GUID, WINDOW_GUID}
        )
