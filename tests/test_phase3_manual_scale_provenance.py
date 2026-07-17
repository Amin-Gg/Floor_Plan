"""Phase 3: strict Manual Inputs, Scale Evidence and two-layer IFC trust boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from export.ifc_exporter import IfcExportError, bim_json_to_ifc
from stage1_contracts import (
    ManualInputsError,
    ScaleEvidenceError,
    assess_scale_evidence,
    build_measurement_provenance,
    canonical_json_sha256,
    parse_manual_inputs,
    resolve_manual_inputs,
)
from validation import validate_ifc_contract


def _plan() -> dict:
    return {
        "walls": [{
            "id": "w1", "start_point": [0, 0, 0], "end_point": [5000, 0, 0],
            "thickness": 200, "height": 2800,
        }],
        "doors": [{
            "id": "d1", "host_wall_id": "w1", "insertion_point": [1200, 0, 0],
            "width": 900, "height": 2100,
        }],
        "windows": [{
            "id": "win1", "host_wall_id": "w1", "insertion_point": [3500, 0, 900],
            "width": 1200, "height": 1200, "sill_height": 900,
        }],
        "rooms": [], "stairs": [], "slabs": [],
        "scale": assess_scale_evidence({
            "schema_version": "1.0", "mm_per_pixel": 1.0,
            "source": "user_dimension",
            "evidence": [{
                "id": "known-1m", "kind": "known_dimension",
                "raw_pixel_measurement": 1000, "real_world_length_mm": 1000,
                "evidence_confidence": 1.0,
            }],
        }),
    }


def _resolved(manual=None) -> dict:
    out, _ = resolve_manual_inputs(_plan(), manual)
    return build_measurement_provenance(out, context={
        "request_id": "phase3-test", "model_version": "test-model",
        "weight_version": "test-weight", "timestamp": "2026-07-12T00:00:00+00:00",
    })


@pytest.mark.parametrize("payload, message", [
    ({"schema_version": "1.0", "unknown": 1}, "Unknown key"),
    ({"schema_version": "1.0", "defaults": {"wall_height_mm": True}}, "finite number"),
    ({"schema_version": "1.0", "defaults": {"wall_height_mm": float("nan")}}, "finite"),
    ({"schema_version": "1.0", "defaults": {"wall_height_mm": 300}}, "between"),
    ({"schema_version": "1.0", "defaults": {
        "wall_height_mm": 2400, "window_sill_height_mm": 1500,
        "window_height_mm": 1200,
    }}, "exceeds"),
    ({"schema_version": "9.9"}, "Unsupported"),
])
def test_manual_inputs_invalid_matrix(payload, message):
    with pytest.raises(ManualInputsError, match=message):
        parse_manual_inputs(payload)


def test_manual_inputs_malformed_json_and_unmatched_override_are_rejected():
    with pytest.raises(ManualInputsError, match="not valid JSON"):
        parse_manual_inputs('{"schema_version":')
    with pytest.raises(ManualInputsError, match="does not match"):
        resolve_manual_inputs(_plan(), {
            "schema_version": "1.0",
            "element_overrides": {"windows": {"missing": {"width_mm": 1400}}},
        })


def test_manual_input_hash_is_canonical_and_key_order_independent():
    a = parse_manual_inputs({
        "defaults": {"door_height_mm": "2100", "wall_height_mm": 2800},
        "schema_version": "1.0",
    })
    b = parse_manual_inputs({
        "schema_version": "1.0",
        "defaults": {"wall_height_mm": 2800.0, "door_height_mm": 2100},
    })
    assert canonical_json_sha256(a) == canonical_json_sha256(b)


def test_exact_one_mm_per_pixel_is_trusted_only_with_evidence():
    trusted = assess_scale_evidence({
        "schema_version": "1.0", "mm_per_pixel": 1.0,
        "source": "user_dimension",
        "evidence": [{
            "id": "one", "kind": "known_dimension",
            "raw_pixel_measurement": 1000, "real_world_length_mm": 1000,
        }],
    })
    default = assess_scale_evidence({
        "schema_version": "1.0", "mm_per_pixel": 1.0,
        "source": "default_unverified", "evidence": [],
    })
    assert trusted["confidence"] >= 0.9 and not trusted["needs_review"]
    assert default["confidence"] < 0.75 and default["needs_review"]
    assert trusted["evidence_sha256"] != default["evidence_sha256"]


@pytest.mark.parametrize("payload, message", [
    ({"schema_version": "1.0", "mm_per_pixel": 1, "source": "guess", "evidence": []}, "Unknown"),
    ({"schema_version": "1.0", "mm_per_pixel": 1, "source": "user_dimension", "evidence": []}, "requires"),
    ({"schema_version": "1.0", "mm_per_pixel": 2, "source": "user_dimension", "evidence": [{
        "id": "bad", "kind": "known", "raw_pixel_measurement": 1000,
        "real_world_length_mm": 1000,
    }]}, "conflicts"),
])
def test_scale_evidence_invalid_matrix(payload, message):
    with pytest.raises(ScaleEvidenceError, match=message):
        assess_scale_evidence(payload)


def test_element_override_resolves_geometry_and_provenance():
    resolved = _resolved({
        "schema_version": "1.0",
        "element_overrides": {
            "windows": {"win1": {"width_mm": 1450, "height_mm": 1000, "sill_height_mm": 800}},
            "doors": {}, "walls": {},
        },
    })
    window = resolved["windows"][0]
    assert (window["width"], window["height"], window["sill_height"]) == (1450, 1000, 800)
    assert window["_manual_input_resolution"]["width"]["source"] == "element_override"
    assert window["_measurement_provenance"]["width"]["value"] == 1450
    assert window["_measurement_provenance"]["width"]["override_history"]


def test_layer_one_exporter_rejects_direct_geometry_override(tmp_path):
    with pytest.raises(IfcExportError, match="resolve Manual Inputs v1"):
        bim_json_to_ifc(
            _plan(), {"wall_height": 3200}, str(tmp_path / "must_not_exist.ifc")
        )
    assert not (tmp_path / "must_not_exist.ifc").exists()


def test_contract_1_2_roundtrip_and_trace_fields(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.util.element as ue

    out = tmp_path / "phase3.ifc"
    bim_json_to_ifc(_resolved({
        "schema_version": "1.0", "defaults": {"wall_height_mm": 3000},
    }), output_path=str(out))
    report = validate_ifc_contract(str(out))
    assert not report.blocked, report.to_dict()
    model = ifcopenshell.open(str(out))
    contract = ue.get_psets(model.by_type("IfcProject")[0])["Pset_SimsysContract"]
    assert contract["ContractVersion"] == "1.2"
    for field in (
        "ManualInputsSha256", "ManualInputsResolvedSha256",
        "ScaleEvidenceSha256", "SourcePayloadSha256",
    ):
        assert len(contract[field]) == 64
    assert contract["ScaleSource"] == "user_dimension"
    door_prov = ue.get_psets(model.by_type("IfcDoor")[0])["Pset_SimsysProvenance"]
    measurements = json.loads(door_prov["MeasurementsJson"])
    context = json.loads(door_prov["ProvenanceContextJson"])
    assert measurements["width"]["value"] == 900
    assert context["request_id"] == "phase3-test"


def test_layer_two_rejects_externally_tampered_provenance(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.pset

    out = tmp_path / "valid.ifc"
    bim_json_to_ifc(_resolved(), output_path=str(out))
    model = ifcopenshell.open(str(out))
    door = model.by_type("IfcDoor")[0]
    pset = next(
        rel.RelatingPropertyDefinition for rel in door.IsDefinedBy
        if rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
        and rel.RelatingPropertyDefinition.Name == "Pset_SimsysProvenance"
    )
    ifcopenshell.api.pset.edit_pset(
        model, pset=pset, properties={"MeasurementsJson": "{broken-json"},
    )
    tampered = tmp_path / "tampered.ifc"
    model.write(str(tampered))
    report = validate_ifc_contract(str(tampered))
    assert report.blocked
    assert any(i.code == "CONTRACT.V12.PROVENANCE_JSON_INVALID" for i in report.issues)


def test_layer_two_rejects_semantically_tampered_measurement(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.pset
    import ifcopenshell.util.element as ue

    out = tmp_path / "valid_semantic.ifc"
    bim_json_to_ifc(_resolved(), output_path=str(out))
    model = ifcopenshell.open(str(out))
    door = model.by_type("IfcDoor")[0]
    pset = next(
        rel.RelatingPropertyDefinition for rel in door.IsDefinedBy
        if rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
        and rel.RelatingPropertyDefinition.Name == "Pset_SimsysProvenance"
    )
    provenance = ue.get_psets(door)["Pset_SimsysProvenance"]
    measurements = json.loads(provenance["MeasurementsJson"])
    measurements["width"]["value"] = 1234.0
    ifcopenshell.api.pset.edit_pset(
        model, pset=pset,
        properties={"MeasurementsJson": json.dumps(measurements, sort_keys=True)},
    )
    tampered = tmp_path / "semantic_tampered.ifc"
    model.write(str(tampered))

    report = validate_ifc_contract(str(tampered))
    assert report.blocked
    assert any(i.code == "CONTRACT.V12.MEASUREMENT_MISMATCH" for i in report.issues)


def test_layer_two_rejects_measurement_scale_commitment_mismatch(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api.pset
    import ifcopenshell.util.element as ue

    out = tmp_path / "valid_scale_hash.ifc"
    bim_json_to_ifc(_resolved(), output_path=str(out))
    model = ifcopenshell.open(str(out))
    wall = model.by_type("IfcWall")[0]
    pset = next(
        rel.RelatingPropertyDefinition for rel in wall.IsDefinedBy
        if rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
        and rel.RelatingPropertyDefinition.Name == "Pset_SimsysProvenance"
    )
    provenance = ue.get_psets(wall)["Pset_SimsysProvenance"]
    measurements = json.loads(provenance["MeasurementsJson"])
    measurements["thickness"]["scale_evidence_sha256"] = "0" * 64
    ifcopenshell.api.pset.edit_pset(
        model, pset=pset,
        properties={"MeasurementsJson": json.dumps(measurements, sort_keys=True)},
    )
    tampered = tmp_path / "scale_hash_tampered.ifc"
    model.write(str(tampered))

    report = validate_ifc_contract(str(tampered))
    assert report.blocked
    assert any(
        i.code == "CONTRACT.V12.MEASUREMENT_SCALE_HASH_MISMATCH"
        for i in report.issues
    )
