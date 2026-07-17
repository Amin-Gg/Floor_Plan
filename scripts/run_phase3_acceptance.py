#!/usr/bin/env python3
"""Phase 3 acceptance: Manual Inputs, scale evidence, provenance and defense-in-depth."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.ifc_exporter import IfcExportError, bim_json_to_ifc
from stage1_contracts import (
    ManualInputsError,
    ScaleEvidenceError,
    assess_scale_evidence,
    build_measurement_provenance,
    resolve_manual_inputs,
)
from utils.process_bridge import run_json_process
from validation import validate_ifc_contract


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _engine(operation: str, payload: Any) -> Any:
    return run_json_process(
        [sys.executable, str(ROOT / "scripts" / "engine_bridge.py"), operation],
        payload=payload,
        timeout=360,
    )


def _pset_entity(product: Any, name: str) -> Any:
    for rel in product.IsDefinedBy:
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == name:
            return definition
    raise LookupError(name)


def _qto_entity(product: Any, name: str) -> Any:
    for rel in product.IsDefinedBy:
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcElementQuantity") and definition.Name == name:
            return definition
    raise LookupError(name)


def _strict_scale() -> dict[str, Any]:
    return assess_scale_evidence({
        "schema_version": "1.0",
        "mm_per_pixel": 1.0,
        "source": "user_dimension",
        "evidence": [{
            "id": "phase3-known-1m",
            "kind": "known_dimension",
            "raw_pixel_measurement": 1000.0,
            "real_world_length_mm": 1000.0,
            "evidence_confidence": 1.0,
            "model_version": "phase3-acceptance",
            "weight_version": "fixture",
        }],
    })


def _prepare_source() -> tuple[dict[str, Any], dict[str, Any]]:
    source_path = ROOT / "compliance-engine" / "tests" / "fixtures" / "sample_plan_bim.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["scale"] = _strict_scale()
    manual = {
        "schema_version": "1.0",
        "defaults": {"wall_height_mm": 2800.0},
    }
    resolved, _ = resolve_manual_inputs(source, manual)
    resolved = build_measurement_provenance(resolved, context={
        "request_id": "phase3-acceptance",
        "model_version": "phase3-acceptance",
        "weight_version": "fixture",
        "timestamp": "2026-07-12T00:00:00+00:00",
    })
    return resolved, manual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="release/local/phase3")
    args = parser.parse_args()
    out = (ROOT / args.out).resolve()
    negatives = out / "negative"
    out.mkdir(parents=True, exist_ok=True)
    if negatives.exists():
        shutil.rmtree(negatives)
    negatives.mkdir(parents=True)

    import numpy as np
    import ifcopenshell
    import ifcopenshell.api.geometry
    import ifcopenshell.api.pset
    import ifcopenshell.util.element as ue
    import ifcopenshell.util.placement as placement
    import ifcopenshell.util.representation as representation

    resolved, manual = _prepare_source()
    valid = out / "valid_contract_1_2.ifc"
    bim_json_to_ifc(resolved, output_path=str(valid))

    # Layer 1: producer prevention before any output is published.
    layer1: dict[str, Any] = {}
    direct_target = out / "must_not_exist_direct_override.ifc"
    try:
        bim_json_to_ifc(resolved, {"wall_height": 3200}, str(direct_target))
    except IfcExportError as exc:
        layer1["direct_geometry_override"] = {
            "blocked": True, "error": str(exc), "output_exists": direct_target.exists(),
        }
    else:
        layer1["direct_geometry_override"] = {"blocked": False}

    try:
        resolve_manual_inputs(resolved, {"schema_version": "1.0", "unknown": 1})
    except ManualInputsError as exc:
        layer1["unknown_manual_key"] = {"blocked": True, "error": str(exc)}
    else:
        layer1["unknown_manual_key"] = {"blocked": False}

    try:
        assess_scale_evidence({
            "schema_version": "1.0", "mm_per_pixel": 1.0,
            "source": "guess", "evidence": [],
        })
    except ScaleEvidenceError as exc:
        layer1["unknown_scale_source"] = {"blocked": True, "error": str(exc)}
    else:
        layer1["unknown_scale_source"] = {"blocked": False}

    trusted_scale = _strict_scale()
    default_scale = assess_scale_evidence({
        "schema_version": "1.0", "mm_per_pixel": 1.0,
        "source": "default_unverified", "evidence": [],
    })
    layer1["exact_one_scale_policy"] = {
        "trusted_with_evidence": not trusted_scale["needs_review"],
        "trusted_confidence": trusted_scale["confidence"],
        "default_requires_review": default_scale["needs_review"],
        "default_confidence": default_scale["confidence"],
        "hashes_are_distinct": trusted_scale["evidence_sha256"] != default_scale["evidence_sha256"],
    }

    def mutate_copy(name: str, mutator: Callable[[Any], None]) -> Path:
        target = negatives / f"{name}.ifc"
        shutil.copy2(valid, target)
        model = ifcopenshell.open(str(target))
        mutator(model)
        model.write(str(target))
        return target

    def tiny_body(model: Any) -> None:
        door = model.by_type("IfcDoor")[0]
        context = representation.get_context(model, "Model", "Body", "MODEL_VIEW")
        tiny = ifcopenshell.api.geometry.add_door_representation(
            model, context=context, overall_height=2.1, overall_width=0.9,
        )
        door.Representation.Representations = (tiny,)

    def wrong_axis(model: Any) -> None:
        opening = model.by_type("IfcOpeningElement")[1]
        current = np.asarray(placement.get_local_placement(opening.ObjectPlacement), dtype=float)
        wrong = np.eye(4)
        wrong[:3, 3] = current[:3, 3]
        ifcopenshell.api.geometry.edit_object_placement(
            model, product=opening, matrix=wrong, is_si=False,
        )

    def count_drift(model: Any) -> None:
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ExpectedDoorCount": 99},
        )

    def qto_drift(model: Any) -> None:
        wall = model.by_type("IfcWall")[0]
        ifcopenshell.api.pset.edit_qto(
            model, qto=_qto_entity(wall, "Qto_WallBaseQuantities"),
            properties={"Width": 20.0},
        )

    def unknown_version(model: Any) -> None:
        project = model.by_type("IfcProject")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(project, "Pset_SimsysContract"),
            properties={"ContractVersion": "9.9"},
        )

    def missing_body(model: Any) -> None:
        model.by_type("IfcDoor")[0].Representation = None

    def no_void(model: Any) -> None:
        opening = model.by_type("IfcOpeningElement")[0]
        model.remove(opening.VoidsElements[0])

    def duplicate_guid(model: Any) -> None:
        model.by_type("IfcDoor")[0].GlobalId = model.by_type("IfcWall")[0].GlobalId

    def measurement_tamper(model: Any) -> None:
        door = model.by_type("IfcDoor")[0]
        prov = ue.get_psets(door)["Pset_SimsysProvenance"]
        measurements = json.loads(prov["MeasurementsJson"])
        measurements["width"]["value"] = 1234.0
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(door, "Pset_SimsysProvenance"),
            properties={"MeasurementsJson": json.dumps(measurements, sort_keys=True)},
        )

    def context_removal(model: Any) -> None:
        wall = model.by_type("IfcWall")[0]
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(wall, "Pset_SimsysProvenance"),
            properties={"ProvenanceContextJson": ""},
        )

    def scale_hash_mismatch(model: Any) -> None:
        wall = model.by_type("IfcWall")[0]
        prov = ue.get_psets(wall)["Pset_SimsysProvenance"]
        measurements = json.loads(prov["MeasurementsJson"])
        measurements["thickness"]["scale_evidence_sha256"] = "0" * 64
        ifcopenshell.api.pset.edit_pset(
            model, pset=_pset_entity(wall, "Pset_SimsysProvenance"),
            properties={"MeasurementsJson": json.dumps(measurements, sort_keys=True)},
        )

    cases: dict[str, Callable[[Any], None]] = {
        "tiny_body": tiny_body,
        "wrong_opening_axis": wrong_axis,
        "manifest_count_drift": count_drift,
        "qto_body_drift": qto_drift,
        "unknown_contract_version": unknown_version,
        "missing_body": missing_body,
        "opening_without_void": no_void,
        "duplicate_global_id": duplicate_guid,
        "measurement_value_tamper": measurement_tamper,
        "provenance_context_removed": context_removal,
        "measurement_scale_hash_mismatch": scale_hash_mismatch,
    }

    valid_stage1 = validate_ifc_contract(str(valid)).to_dict()
    negative_rows: list[tuple[str, Path, dict[str, Any]]] = []
    for name, mutator in cases.items():
        path = mutate_copy(name, mutator)
        stage1 = validate_ifc_contract(str(path)).to_dict()
        negative_rows.append((name, path, stage1))

    prechecks = _engine(
        "batch_precheck_ifc", [str(valid.resolve())] + [str(path.resolve()) for _, path, _ in negative_rows],
    )
    valid_engine = prechecks[0]
    full_valid, full_phase2 = _engine(
        "batch_full_check_ifc",
        [
            str(valid.resolve()),
            str((ROOT / "compliance-engine/tests/fixtures/sample_plan.ifc").resolve()),
        ],
    )

    layer2: dict[str, Any] = {
        "schema_version": "phase3-defense-in-depth-matrix-v1",
        "contract_version": "1.2",
        "valid": {
            "file": _display(valid),
            "sha256": _sha256(valid),
            "stage1": valid_stage1,
            "engine_precheck": valid_engine,
            "engine_full_check": full_valid,
        },
        "negative": {},
    }
    for (name, path, stage1), engine in zip(negative_rows, prechecks[1:], strict=True):
        layer2["negative"][name] = {
            "file": _display(path),
            "sha256": _sha256(path),
            "stage1_blocked": stage1["blocked"],
            "stage1_critical_codes": sorted(
                issue["code"] for issue in stage1["issues"] if issue["severity"] == "critical"
            ),
            "engine_blocked_reason": engine.get("blocked_reason"),
            "engine_schema_status": (engine.get("schema") or {}).get("status"),
            "engine_quality_status": (engine.get("quality") or {}).get("status"),
            "engine_fail_codes": sorted({
                row["code"]
                for stage in (engine.get("schema") or {}, engine.get("quality") or {})
                for row in stage.get("findings", [])
                if row.get("severity") == "fail"
            }),
        }

    matching = _engine("precheck_ifc_with_manual", {
        "path": str(valid.resolve()), "manual_inputs": manual,
    })
    conflict = {"schema_version": "1.0", "defaults": {"wall_height_mm": 3200.0}}
    try:
        _engine("precheck_ifc_with_manual", {
            "path": str(valid.resolve()), "manual_inputs": conflict,
        })
    except RuntimeError as exc:
        conflict_result = {"blocked": True, "error_contains_reexport": "Re-export" in str(exc)}
    else:
        conflict_result = {"blocked": False, "error_contains_reexport": False}
    manual_boundary = {
        "matching_external_payload": matching["manual_inputs"],
        "conflicting_external_payload": conflict_result,
    }

    live_summary = full_valid["compliance"]["summary"]
    phase2_summary = full_phase2["compliance"]["summary"]
    live_verdicts = [
        [row["article_id"], row["verdict"]]
        for row in full_valid["compliance"]["findings"]
    ]
    phase2_verdicts = [
        [row["article_id"], row["verdict"]]
        for row in full_phase2["compliance"]["findings"]
    ]
    regression = {
        "schema_version": "phase3-verdict-regression-v1",
        "live_summary": live_summary,
        "phase2_summary": phase2_summary,
        "summaries_equal": live_summary == phase2_summary,
        "verdict_sequence_equal": live_verdicts == phase2_verdicts,
        "live_verdicts": live_verdicts,
        "phase2_verdicts": phase2_verdicts,
    }

    _write_json(out / "layer1_exporter_prevention.json", layer1)
    _write_json(out / "layer2_gate_matrix.json", layer2)
    _write_json(out / "manual_input_boundary.json", manual_boundary)
    _write_json(out / "valid_stage1_contract.json", valid_stage1)
    _write_json(out / "valid_engine_precheck.json", valid_engine)
    _write_json(out / "valid_engine_full_check.json", full_valid)
    _write_json(out / "verdict_regression.json", regression)

    layer1_ok = (
        layer1["direct_geometry_override"].get("blocked")
        and not layer1["direct_geometry_override"].get("output_exists")
        and layer1["unknown_manual_key"].get("blocked")
        and layer1["unknown_scale_source"].get("blocked")
        and layer1["exact_one_scale_policy"]["trusted_with_evidence"]
        and layer1["exact_one_scale_policy"]["default_requires_review"]
    )
    failed_negatives = [
        name for name, row in layer2["negative"].items()
        if not row["stage1_blocked"] or not row["engine_blocked_reason"]
    ]
    valid_ok = (
        not valid_stage1["blocked"]
        and not valid_engine["blocked_reason"]
        and not full_valid["blocked_reason"]
    )
    manual_ok = (
        matching["manual_inputs"].get("hash_match") is True
        and matching["manual_inputs"].get("geometry_mutated_by_engine") is False
        and conflict_result["blocked"]
        and conflict_result["error_contains_reexport"]
    )
    verdict_ok = regression["summaries_equal"] and regression["verdict_sequence_equal"]
    if not layer1_ok:
        raise SystemExit("exporter prevention acceptance failed")
    if failed_negatives:
        raise SystemExit(f"negative IFCs escaped one of the independent gates: {failed_negatives}")
    if not valid_ok:
        raise SystemExit("valid Contract 1.2 IFC was blocked")
    if not manual_ok:
        raise SystemExit("manual-input engine boundary acceptance failed")
    if not verdict_ok:
        raise SystemExit("deterministic verdict regression detected")

    result = {
        "schema_version": "phase3-acceptance-result-v1",
        "output": _display(out),
        "valid_sha256": _sha256(valid),
        "layer1_prevention_cases": 4,
        "layer1_passed": True,
        "external_tamper_cases": len(cases),
        "all_external_tamper_blocked_by_stage1_and_engine": True,
        "manual_conflict_requires_reexport": True,
        "verdict_regression": "passed",
        "compliance_summary": live_summary,
    }
    _write_json(out / "acceptance_result.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
