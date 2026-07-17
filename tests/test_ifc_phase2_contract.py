"""Phase 2: Contract 1.2, Body-aware gates, and live engine boundary tests."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")
import ifcopenshell.api.geometry  # noqa: E402
import ifcopenshell.api.pset  # noqa: E402
import ifcopenshell.util.element as ue  # noqa: E402
import ifcopenshell.util.placement as placement  # noqa: E402
import ifcopenshell.util.representation as representation  # noqa: E402

from export.ifc_exporter import CONTRACT_VERSION, EXPORTER_VERSION, bim_json_to_ifc  # noqa: E402
from stage1_contracts import assess_scale_evidence, build_measurement_provenance, resolve_manual_inputs  # noqa: E402
from validation import validate_ifc_contract  # noqa: E402
from tests._engine_modules import precheck_ifcs  # noqa: E402


def _plan() -> dict:
    return {
        "walls": [
            {
                "id": "w1", "start_point": [0, 0, 0],
                "end_point": [5000, 0, 0], "thickness": 200,
                "height": 2800, "is_exterior": True,
            },
            {
                "id": "diag", "start_point": [7000, 1000, 0],
                "end_point": [11000, 4000, 0], "thickness": 240,
                "height": 3000, "is_exterior": False,
            },
        ],
        "doors": [
            {
                "id": "d1", "host_wall_id": "w1",
                "insertion_point": [2500, 0, 0], "width": 900,
                "height": 2100,
            },
            {
                "id": "d2", "host_wall_id": "diag",
                "insertion_point": [9000, 2500, 0], "width": 1000,
                "height": 2200,
            },
        ],
        "windows": [],
        "rooms": [
            {
                "id": "r1", "name": "Bedroom", "category": "room_bedroom",
                "area_m2": 12.0,
                "polygon": [[0, 1000], [4000, 1000], [4000, 4000], [0, 4000], [0, 1000]],
                "dimensions": {"width_mm": 3000, "length_mm": 4000},
                "centroid_mm": [2000, 2500],
            }
        ],
        "stairs": [], "slabs": [],
        "scale": {"mm_per_pixel": 10.0, "source": "reference", "confidence": 0.9},
    }


def _export(plan: dict, path: Path, *, wall_height: float = 2800) -> Path:
    source = json.loads(json.dumps(plan))
    legacy_scale = dict(source.get("scale") or {})
    mmpp = float(legacy_scale.get("mm_per_pixel", 1.0))
    confidence = float(legacy_scale.get("confidence", 1.0) or 1.0)
    source["scale"] = assess_scale_evidence({
        "schema_version": "1.0",
        "mm_per_pixel": mmpp,
        "source": "user_dimension",
        "evidence": [{
            "id": "phase2-fixture-scale",
            "kind": "known_dimension",
            "raw_pixel_measurement": 1000.0,
            "real_world_length_mm": 1000.0 * mmpp,
            "evidence_confidence": confidence,
        }],
    })
    resolved, _ = resolve_manual_inputs(source, {
        "schema_version": "1.0",
        "defaults": {"wall_height_mm": wall_height},
    })
    resolved = build_measurement_provenance(
        resolved,
        context={
            "request_id": "phase2-regression",
            "model_version": "fixture",
            "weight_version": "fixture",
            "timestamp": "2026-07-12T00:00:00+00:00",
        },
    )
    bim_json_to_ifc(resolved, output_path=str(path))
    return path


@pytest.fixture()
def valid_ifc(tmp_path: Path) -> Path:
    path = tmp_path / "valid_contract_1_2.ifc"
    return _export(_plan(), path, wall_height=2800)


def _contract_pset(model):
    return ue.get_psets(model.by_type("IfcProject")[0])["Pset_SimsysContract"]


def _pset_entity(product, name: str):
    for rel in product.IsDefinedBy:
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == name:
            return definition
    raise AssertionError(f"missing pset {name}")


def _qto_entity(product, name: str):
    for rel in product.IsDefinedBy:
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcElementQuantity") and definition.Name == name:
            return definition
    raise AssertionError(f"missing qto {name}")


def _copy_mutate(valid_ifc: Path, tmp_path: Path, name: str, mutate) -> Path:
    target = tmp_path / name
    shutil.copy2(valid_ifc, target)
    model = ifcopenshell.open(str(target))
    mutate(model)
    model.write(str(target))
    return target


def _critical_codes(path: Path) -> set[str]:
    report = validate_ifc_contract(str(path))
    return {issue.code for issue in report.issues if issue.severity.value == "critical"}


def test_contract_1_2_manifest_is_complete_and_deterministic(tmp_path):
    first = tmp_path / "a.ifc"
    second = tmp_path / "b.ifc"
    _export(_plan(), first, wall_height=2800)
    _export(_plan(), second, wall_height=2800)
    a = _contract_pset(ifcopenshell.open(str(first)))
    b = _contract_pset(ifcopenshell.open(str(second)))

    assert a["ContractVersion"] == CONTRACT_VERSION == "1.2"
    assert a["ExporterVersion"] == EXPORTER_VERSION
    assert a["InsertionPointSemantics"] == "CENTER_ON_HOST_CENTERLINE"
    assert a["OrientationConvention"] == "LOCAL_X_WALL_DIRECTION_LOCAL_Y_THICKNESS_LOCAL_Z_UP"
    assert a["LengthUnit"] == "MILLIMETRE"
    assert a["ExpectedWallCount"] == 2
    assert a["ExpectedDoorCount"] == 2
    assert a["ExpectedSpaceCount"] == 1
    assert len(a["SourcePayloadSha256"]) == 64
    assert len(a["ManualInputManifestSha256"]) == 64
    assert a["SourcePayloadSha256"] == b["SourcePayloadSha256"]
    assert a["ManualInputManifestSha256"] == b["ManualInputManifestSha256"]


def test_source_and_manual_hashes_change_only_with_their_inputs(tmp_path):
    source_changed = _plan()
    source_changed["scale"]["confidence"] = 0.8
    paths = [tmp_path / f"{i}.ifc" for i in range(3)]
    _export(_plan(), paths[0], wall_height=2800)
    _export(source_changed, paths[1], wall_height=2800)
    _export(_plan(), paths[2], wall_height=3000)
    psets = [_contract_pset(ifcopenshell.open(str(path))) for path in paths]

    assert psets[0]["SourcePayloadSha256"] != psets[1]["SourcePayloadSha256"]
    assert psets[0]["ManualInputManifestSha256"] == psets[1]["ManualInputManifestSha256"]
    assert psets[0]["SourcePayloadSha256"] == psets[2]["SourcePayloadSha256"]
    assert psets[0]["ManualInputManifestSha256"] != psets[2]["ManualInputManifestSha256"]


def test_stage1_and_engine_reject_body_1000x_too_small(valid_ifc, tmp_path):
    def mutate(model):
        door = model.by_type("IfcDoor")[0]
        context = representation.get_context(model, "Model", "Body", "MODEL_VIEW")
        tiny = ifcopenshell.api.geometry.add_door_representation(
            model, context=context, overall_height=2.1, overall_width=0.9,
        )
        door.Representation.Representations = (tiny,)

    broken = _copy_mutate(valid_ifc, tmp_path, "tiny_body.ifc", mutate)
    assert "CONTRACT.GEOM.FILLING.BODY_ATTRIBUTE_MISMATCH" in _critical_codes(broken)


def test_stage1_and_engine_reject_wrong_opening_axis(valid_ifc, tmp_path):
    def mutate(model):
        opening = model.by_type("IfcOpeningElement")[1]
        current = np.asarray(placement.get_local_placement(opening.ObjectPlacement), dtype=float)
        wrong = np.eye(4)
        wrong[:3, 3] = current[:3, 3]
        ifcopenshell.api.geometry.edit_object_placement(
            model, product=opening, matrix=wrong, is_si=False,
        )

    broken = _copy_mutate(valid_ifc, tmp_path, "wrong_axis.ifc", mutate)
    assert "CONTRACT.GEOM.OPENING.ORIENTATION_MISMATCH" in _critical_codes(broken)


def test_stage1_and_engine_reject_manifest_count_drift(valid_ifc, tmp_path):
    def mutate(model):
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ExpectedDoorCount": 99},
        )

    broken = _copy_mutate(valid_ifc, tmp_path, "count_drift.ifc", mutate)
    assert "CONTRACT.GEOM.MANIFEST.COUNT_MISMATCH" in _critical_codes(broken)


def test_stage1_and_engine_reject_qto_body_contradiction(valid_ifc, tmp_path):
    def mutate(model):
        wall = model.by_type("IfcWall")[0]
        ifcopenshell.api.pset.edit_qto(
            model, qto=_qto_entity(wall, "Qto_WallBaseQuantities"),
            properties={"Width": 20.0},
        )

    broken = _copy_mutate(valid_ifc, tmp_path, "qto_drift.ifc", mutate)
    assert "CONTRACT.GEOM.WALL.BODY_QTO_MISMATCH" in _critical_codes(broken)


def test_unknown_contract_version_is_blocked_before_ingest(valid_ifc, tmp_path):
    def mutate(model):
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ContractVersion": "9.9"},
        )

    broken = _copy_mutate(valid_ifc, tmp_path, "unknown_version.ifc", mutate)
    assert "CONTRACT.VERSION.UNSUPPORTED" in _critical_codes(broken)


def test_valid_contract_1_2_passes_both_geometry_gates(valid_ifc):
    report = validate_ifc_contract(str(valid_ifc))
    assert not report.blocked, report.to_dict()


def test_live_stage1_export_reaches_engine_compliance_without_verdict_drift(tmp_path):
    """The public Stage-1 exporter must feed the real engine, not a mock seam."""
    import json

    source_path = Path("compliance-engine/tests/fixtures/sample_plan_bim.json")
    live_ifc = tmp_path / "live_stage1_to_engine.ifc"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    _export(source, live_ifc, wall_height=2800)

    from tests._engine_modules import full_check_ifc

    live = full_check_ifc(str(live_ifc))
    assert live["blocked_reason"] is None
    assert live["schema"]["status"] == "passed"
    assert live["quality"]["status"] in {"passed", "passed_with_alerts"}
    assert live["stage_trace"][-1] == "compliance"
    assert live["compliance"] is not None
    assert live["compliance"]["summary"] == {
        "PASS": 1, "FAIL": 3, "NEEDS_REVIEW": 1, "NOT_EVALUATED": 0,
    }
    assert [
        (row["article_id"], row["verdict"])
        for row in live["compliance"]["findings"]
    ] == [
        ("PH2-N1", "PASS"),
        ("PH2-O1", "NEEDS_REVIEW"),
        ("PH2-S1", "FAIL"),
        ("PH2-O1", "FAIL"),
        ("natural_light_presence", "FAIL"),
    ]


def test_engine_rejects_opening_without_host_relation(valid_ifc, tmp_path):
    def mutate(model):
        opening = model.by_type("IfcOpeningElement")[0]
        model.remove(opening.VoidsElements[0])

    broken = _copy_mutate(valid_ifc, tmp_path, "opening_without_host.ifc", mutate)
    assert _critical_codes(broken)


def test_engine_batch_accepts_valid_and_rejects_contract_negatives(valid_ifc, tmp_path):
    def tiny(model):
        door = model.by_type("IfcDoor")[0]
        context = representation.get_context(model, "Model", "Body", "MODEL_VIEW")
        rep = ifcopenshell.api.geometry.add_door_representation(
            model, context=context, overall_height=2.1, overall_width=0.9,
        )
        door.Representation.Representations = (rep,)

    def wrong_axis(model):
        opening = model.by_type("IfcOpeningElement")[1]
        current = np.asarray(placement.get_local_placement(opening.ObjectPlacement), dtype=float)
        wrong = np.eye(4)
        wrong[:3, 3] = current[:3, 3]
        ifcopenshell.api.geometry.edit_object_placement(
            model, product=opening, matrix=wrong, is_si=False,
        )

    def count_drift(model):
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ExpectedDoorCount": 99},
        )

    def qto_drift(model):
        wall = model.by_type("IfcWall")[0]
        ifcopenshell.api.pset.edit_qto(
            model, qto=_qto_entity(wall, "Qto_WallBaseQuantities"),
            properties={"Width": 20.0},
        )

    def unknown_version(model):
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ContractVersion": "9.9"},
        )

    def missing_body(model):
        model.by_type("IfcDoor")[0].Representation = None

    def no_void(model):
        opening = model.by_type("IfcOpeningElement")[0]
        model.remove(opening.VoidsElements[0])

    def duplicate_guid(model):
        model.by_type("IfcDoor")[0].GlobalId = model.by_type("IfcWall")[0].GlobalId

    cases = [
        ("tiny", tiny),
        ("axis", wrong_axis),
        ("count", count_drift),
        ("qto", qto_drift),
        ("version", unknown_version),
        ("body", missing_body),
        ("void", no_void),
        ("guid", duplicate_guid),
    ]
    paths = [valid_ifc]
    for name, mutator in cases:
        paths.append(_copy_mutate(valid_ifc, tmp_path, f"batch_{name}.ifc", mutator))

    results = precheck_ifcs([str(path) for path in paths])
    assert len(results) == 9
    assert results[0]["blocked_reason"] is None
    assert all(result["blocked_reason"] for result in results[1:])
    assert results[5]["blocked_reason"] == "IFC schema validation failed"
    assert results[8]["blocked_reason"] == "IFC schema validation failed"
    assert any(
        row["details"].get("geometry_code") == "GEOM.OPENING.HOST_RELATION_MISSING"
        for row in results[7]["quality"]["findings"]
        if row["code"] == "QC-IFC-GEOM-004"
    )
