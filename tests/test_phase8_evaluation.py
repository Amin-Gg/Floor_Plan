from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from evaluation.dataset import DatasetContractError, load_dataset
from evaluation.metrics import EvaluationConfig, compare_variants, evaluate_dataset
from evaluation.policy import apply_policy, load_policy
from services.accuracy_service import performAccuracyAnalysis

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "examples" / "phase8" / "reference_dataset" / "dataset.json"
POLICY = ROOT / "config" / "phase8_evaluation_policy.json"


def test_reference_dataset_is_valid_but_not_empirical_evidence():
    dataset = load_dataset(REFERENCE)
    assert dataset.dataset_id == "phase8-synthetic-reference-v1"
    assert dataset.empirical_claims_allowed is False
    assert dataset.prediction_variants() == ("baseline", "candidate")


def test_candidate_variant_improves_detection_geometry_scale_and_verdicts():
    dataset = load_dataset(REFERENCE)
    baseline = evaluate_dataset(dataset, variant="baseline")
    candidate = evaluate_dataset(dataset, variant="candidate")
    assert candidate["summary"]["map_50_95"] > baseline["summary"]["map_50_95"]
    assert candidate["summary"]["macro_f1"] > baseline["summary"]["macro_f1"]
    assert candidate["geometry_overall"]["center_error_px"]["mean"] < baseline["geometry_overall"]["center_error_px"]["mean"]
    assert candidate["scale"]["relative_error"]["mean"] < baseline["scale"]["relative_error"]["mean"]
    assert candidate["verdict_impact"]["exact_agreement"] > baseline["verdict_impact"]["exact_agreement"]


def test_critical_false_pass_is_detected():
    dataset = load_dataset(REFERENCE)
    baseline = evaluate_dataset(dataset, variant="baseline")
    candidate = evaluate_dataset(dataset, variant="candidate")
    assert baseline["verdict_impact"]["critical_false_pass"] == 1
    assert candidate["verdict_impact"]["critical_false_pass"] == 0


def test_synthetic_reference_can_never_pass_release_policy():
    dataset = load_dataset(REFERENCE)
    report = evaluate_dataset(dataset, variant="candidate")
    gate = apply_policy(report, load_policy(POLICY))
    assert report["summary"]["map_50_95"] == pytest.approx(1.0)
    assert gate["passed"] is False
    check = next(row for row in gate["checks"] if row["name"] == "human_verified_labels")
    assert check["passed"] is False


def test_ab_comparison_reports_all_safety_and_quality_deltas():
    dataset = load_dataset(REFERENCE)
    reports = {name: evaluate_dataset(dataset, variant=name) for name in ("baseline", "candidate")}
    comparison = compare_variants(reports, "baseline")["comparisons"]["candidate"]
    assert comparison["delta_macro_f1"] > 0
    assert comparison["delta_map_50_95"] > 0
    assert comparison["delta_ece"] < 0
    assert comparison["critical_false_pass"] == 0


def test_slice_reports_cover_declared_dimensions():
    report = evaluate_dataset(load_dataset(REFERENCE), variant="candidate")
    assert set(report["slices"]) == {"language", "plan_style", "scan_quality"}
    assert set(report["slices"]["plan_style"]) == {"office", "residential"}


def test_path_traversal_is_rejected(tmp_path: Path):
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "dataset_id": "unsafe",
        "split": "test",
        "label_status": "human_verified",
        "classes": ["wall"],
        "samples": [{
            "sample_id": "x",
            "width": 100,
            "height": 100,
            "annotations_path": "../outside.json",
            "predictions": {"baseline": "../outside.json"},
        }],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetContractError, match="escapes the dataset root"):
        load_dataset(path)


def test_duplicate_image_content_is_rejected(tmp_path: Path):
    image = tmp_path / "one.png"
    from PIL import Image
    Image.new("RGB", (10, 10), "white").save(image)
    duplicate = tmp_path / "two.png"
    duplicate.write_bytes(image.read_bytes())
    annotation = {"schema_version": "1.0", "sample_id": "placeholder", "instances": []}
    prediction = {"schema_version": "1.0", "sample_id": "placeholder", "model": {}, "instances": []}
    samples = []
    for index, name in enumerate(("one", "two")):
        sid = f"s{index}"
        annotation["sample_id"] = sid
        prediction["sample_id"] = sid
        (tmp_path / f"{sid}.ann.json").write_text(json.dumps(annotation), encoding="utf-8")
        (tmp_path / f"{sid}.pred.json").write_text(json.dumps(prediction), encoding="utf-8")
        samples.append({"sample_id": sid, "width": 10, "height": 10, "image_path": f"{name}.png", "annotations_path": f"{sid}.ann.json", "predictions": {"v": f"{sid}.pred.json"}, "label_status": "human_verified"})
    manifest = {"schema_version": "1.0", "dataset_id": "dup", "split": "test", "classes": ["wall"], "samples": samples}
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetContractError, match="Duplicate image content"):
        load_dataset(path)


def test_confidence_diagnostics_do_not_claim_accuracy():
    model_results = {
        "rois": np.array([[0, 0, 10, 10]]),
        "class_ids": np.array([1]),
        "scores": np.array([0.9]),
        "masks": np.zeros((20, 20, 1), dtype=bool),
    }
    report = performAccuracyAnalysis(model_results, 20, 20)
    assert report["report_kind"] == "prediction_confidence_diagnostics"
    assert report["ground_truth_used"] is False
    assert report["accuracy_claim"] is False
    assert "not accuracy metrics" in report["metric_warning"]


def test_operating_threshold_is_configurable():
    dataset = load_dataset(REFERENCE)
    default = evaluate_dataset(dataset, variant="baseline")
    strict = evaluate_dataset(dataset, variant="baseline", config=EvaluationConfig(confidence_threshold=0.9))
    assert strict["summary"]["macro_recall"] < default["summary"]["macro_recall"]


def test_dataset_without_predictions_can_be_audited_and_used_for_inference(tmp_path: Path):
    annotation = tmp_path / "ann.json"
    annotation.write_text(json.dumps({"schema_version": "1.0", "sample_id": "s1", "instances": []}), encoding="utf-8")
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "dataset_id": "unscored",
        "split": "test",
        "classes": ["wall"],
        "samples": [{
            "sample_id": "s1",
            "width": 10,
            "height": 10,
            "annotations_path": "ann.json",
            "label_status": "human_verified"
        }]
    }), encoding="utf-8")
    dataset = load_dataset(manifest)
    assert dataset.prediction_variants() == ()
    with pytest.raises(ValueError, match="missing"):
        evaluate_dataset(dataset, variant="baseline")


def _minimal_dataset_with_hashes(tmp_path: Path) -> Path:
    import hashlib
    annotation = tmp_path / "ann.json"
    prediction = tmp_path / "pred.json"
    annotation.write_text(json.dumps({"schema_version": "1.0", "sample_id": "s1", "instances": []}), encoding="utf-8")
    prediction.write_text(json.dumps({"schema_version": "1.0", "sample_id": "s1", "model": {}, "instances": []}), encoding="utf-8")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "dataset_id": "hashed",
        "split": "test",
        "classes": ["wall"],
        "samples": [{
            "sample_id": "s1", "width": 10, "height": 10,
            "annotations_path": "ann.json", "annotations_sha256": digest(annotation),
            "predictions": {"v": "pred.json"}, "prediction_sha256": {"v": digest(prediction)},
            "label_status": "human_verified"
        }]
    }), encoding="utf-8")
    return manifest


def test_annotation_hash_mismatch_is_rejected(tmp_path: Path):
    manifest = _minimal_dataset_with_hashes(tmp_path)
    (tmp_path / "ann.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DatasetContractError, match="Annotation SHA-256 mismatch"):
        load_dataset(manifest)


def test_prediction_hash_mismatch_is_rejected(tmp_path: Path):
    manifest = _minimal_dataset_with_hashes(tmp_path)
    (tmp_path / "pred.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DatasetContractError, match="Prediction SHA-256 mismatch"):
        load_dataset(manifest)


def test_engine_verdict_attachment_keeps_worst_verdict_per_clause():
    from scripts.attach_phase8_verdicts import extract
    payload = {"compliance": {"findings": [
        {"clause_id": "A", "verdict": "PASS"},
        {"clause_id": "A", "verdict": "FAIL"},
        {"article_id": "B", "verdict": "NEEDS_REVIEW"},
    ]}}
    assert extract(payload) == {"A": "FAIL", "B": "NEEDS_REVIEW"}


def test_preferred_reliability_route_is_registered(monkeypatch):
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("FLOORPLAN_SKIP_MODEL_INIT", "1")
    from application import create_app
    from config.settings import TestingConfig
    app = create_app(TestingConfig)
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/analyze_reliability" in rules
    assert "/analyze_accuracy" in rules
