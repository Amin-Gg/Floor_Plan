"""Strict, path-safe Phase-8 evaluation dataset loader.

The loader intentionally supports precomputed predictions. Real inference is a
separate operation so metric computation remains deterministic and can be
repeated without GPUs or model downloads.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
ALLOWED_LABEL_STATUS = {"human_verified", "adjudicated", "synthetic_contract_test"}


class DatasetContractError(ValueError):
    """The evaluation dataset is unsafe, incomplete, or semantically invalid."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetContractError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetContractError(f"JSON root must be an object: {path}")
    return value


def _safe_resolve(root: Path, value: str, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DatasetContractError(f"{field} must be a non-empty relative path")
    raw = Path(value)
    if raw.is_absolute():
        raise DatasetContractError(f"{field} must be relative to the dataset manifest")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DatasetContractError(f"{field} escapes the dataset root: {value}") from exc
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_number(value: Any, field: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetContractError(f"{field} must be numeric")
    number = float(value)
    if number < 0 or (number == 0 and not allow_zero):
        raise DatasetContractError(f"{field} must be {'non-negative' if allow_zero else 'positive'}")
    if number != number or number in (float("inf"), float("-inf")):
        raise DatasetContractError(f"{field} must be finite")
    return number


def _validate_bbox(value: Any, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise DatasetContractError(f"{field} must be [x1,y1,x2,y2]")
    bbox = [_positive_number(item, f"{field}[{index}]", allow_zero=True) for index, item in enumerate(value)]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise DatasetContractError(f"{field} must have positive width and height")
    return bbox


def _validate_instance(instance: Any, field: str, classes: set[str], *, prediction: bool) -> dict[str, Any]:
    if not isinstance(instance, dict):
        raise DatasetContractError(f"{field} must be an object")
    identifier = instance.get("id")
    class_name = instance.get("class_name")
    if not isinstance(identifier, str) or not identifier.strip():
        raise DatasetContractError(f"{field}.id must be a non-empty string")
    if class_name not in classes:
        raise DatasetContractError(f"{field}.class_name is not declared: {class_name!r}")
    normalized = dict(instance)
    normalized["bbox_xyxy"] = _validate_bbox(instance.get("bbox_xyxy"), f"{field}.bbox_xyxy")
    if prediction:
        score = _positive_number(instance.get("confidence"), f"{field}.confidence", allow_zero=True)
        if score > 1:
            raise DatasetContractError(f"{field}.confidence must be in [0,1]")
        normalized["confidence"] = score
    attributes = instance.get("attributes", {})
    if not isinstance(attributes, dict):
        raise DatasetContractError(f"{field}.attributes must be an object")
    normalized["attributes"] = dict(attributes)
    mask = instance.get("mask")
    if mask is not None and not isinstance(mask, dict):
        raise DatasetContractError(f"{field}.mask must be an object")
    return normalized


@dataclass(frozen=True)
class EvaluationSample:
    sample_id: str
    width: int
    height: int
    image_path: Path | None
    annotations: dict[str, Any]
    prediction_paths: dict[str, Path]
    slices: dict[str, str]
    mm_per_pixel: float | None
    label_status: str


@dataclass(frozen=True)
class EvaluationDataset:
    manifest_path: Path
    manifest_sha256: str
    dataset_id: str
    split: str
    classes: tuple[str, ...]
    samples: tuple[EvaluationSample, ...]
    metadata: dict[str, Any]

    @property
    def empirical_claims_allowed(self) -> bool:
        return bool(self.samples) and all(sample.label_status in {"human_verified", "adjudicated"} for sample in self.samples)

    def prediction_variants(self) -> tuple[str, ...]:
        variants: set[str] = set()
        for sample in self.samples:
            variants.update(sample.prediction_paths)
        return tuple(sorted(variants))


def load_prediction(path: Path, *, sample_id: str, classes: set[str]) -> dict[str, Any]:
    payload = _read_json(path)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DatasetContractError(f"Unsupported prediction schema in {path}")
    if payload.get("sample_id") != sample_id:
        raise DatasetContractError(f"Prediction sample_id mismatch in {path}")
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise DatasetContractError(f"instances must be an array in {path}")
    normalized = dict(payload)
    normalized["instances"] = [
        _validate_instance(row, f"{path.name}.instances[{index}]", classes, prediction=True)
        for index, row in enumerate(instances)
    ]
    scale = payload.get("scale")
    if scale is not None:
        if not isinstance(scale, dict):
            raise DatasetContractError(f"scale must be an object in {path}")
        normalized["scale"] = dict(scale)
    verdicts = payload.get("verdicts", {})
    if not isinstance(verdicts, dict):
        raise DatasetContractError(f"verdicts must be an object in {path}")
    normalized["verdicts"] = {str(k): str(v) for k, v in verdicts.items()}
    normalized["_path"] = str(path)
    return normalized


def load_dataset(path: str | Path, *, verify_hashes: bool = True) -> EvaluationDataset:
    manifest_path = Path(path).resolve()
    manifest = _read_json(manifest_path)
    root = manifest_path.parent
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetContractError(f"Unsupported dataset schema: {manifest.get('schema_version')!r}")
    dataset_id = manifest.get("dataset_id")
    split = manifest.get("split")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise DatasetContractError("dataset_id must be a non-empty string")
    if split not in {"train", "validation", "test", "holdout", "synthetic"}:
        raise DatasetContractError("split must be train/validation/test/holdout/synthetic")
    classes_raw = manifest.get("classes")
    if not isinstance(classes_raw, list) or not classes_raw or not all(isinstance(v, str) and v for v in classes_raw):
        raise DatasetContractError("classes must be a non-empty array of strings")
    if len(set(classes_raw)) != len(classes_raw):
        raise DatasetContractError("classes must be unique")
    classes = set(classes_raw)
    samples_raw = manifest.get("samples")
    if not isinstance(samples_raw, list) or not samples_raw:
        raise DatasetContractError("samples must be a non-empty array")

    samples: list[EvaluationSample] = []
    seen_ids: set[str] = set()
    seen_hashes: dict[str, str] = {}
    for index, raw in enumerate(samples_raw):
        field = f"samples[{index}]"
        if not isinstance(raw, dict):
            raise DatasetContractError(f"{field} must be an object")
        sample_id = raw.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in seen_ids:
            raise DatasetContractError(f"{field}.sample_id must be unique and non-empty")
        seen_ids.add(sample_id)
        width = int(_positive_number(raw.get("width"), f"{field}.width"))
        height = int(_positive_number(raw.get("height"), f"{field}.height"))
        label_status = raw.get("label_status", manifest.get("label_status"))
        if label_status not in ALLOWED_LABEL_STATUS:
            raise DatasetContractError(f"{field}.label_status is invalid")
        image_path = None
        if raw.get("image_path"):
            image_path = _safe_resolve(root, raw["image_path"], field=f"{field}.image_path")
            if not image_path.is_file():
                raise DatasetContractError(f"Missing image: {image_path}")
            expected = raw.get("image_sha256")
            actual = _sha256(image_path)
            if verify_hashes and expected and expected != actual:
                raise DatasetContractError(f"Image SHA-256 mismatch for {sample_id}")
            if actual in seen_hashes:
                raise DatasetContractError(
                    f"Duplicate image content in split: {sample_id} and {seen_hashes[actual]}"
                )
            seen_hashes[actual] = sample_id
        annotation_path = _safe_resolve(root, raw.get("annotations_path"), field=f"{field}.annotations_path")
        if not annotation_path.is_file():
            raise DatasetContractError(f"Missing annotations: {annotation_path}")
        expected_annotation_sha = raw.get("annotations_sha256")
        if verify_hashes and expected_annotation_sha and expected_annotation_sha != _sha256(annotation_path):
            raise DatasetContractError(f"Annotation SHA-256 mismatch for {sample_id}")
        annotations = _read_json(annotation_path)
        if annotations.get("schema_version") != SCHEMA_VERSION or annotations.get("sample_id") != sample_id:
            raise DatasetContractError(f"Annotation contract mismatch: {annotation_path}")
        rows = annotations.get("instances")
        if not isinstance(rows, list):
            raise DatasetContractError(f"instances must be an array in {annotation_path}")
        annotations["instances"] = [
            _validate_instance(row, f"{annotation_path.name}.instances[{row_index}]", classes, prediction=False)
            for row_index, row in enumerate(rows)
        ]
        gt_verdicts = annotations.get("verdicts", {})
        if not isinstance(gt_verdicts, dict):
            raise DatasetContractError(f"verdicts must be an object in {annotation_path}")
        annotations["verdicts"] = {str(k): str(v) for k, v in gt_verdicts.items()}
        predictions_raw = raw.get("predictions", {})
        if not isinstance(predictions_raw, dict):
            raise DatasetContractError(f"{field}.predictions must be an object")
        prediction_paths: dict[str, Path] = {}
        for variant, rel in predictions_raw.items():
            if not isinstance(variant, str) or not variant.strip():
                raise DatasetContractError(f"{field}.predictions has an invalid variant")
            prediction_path = _safe_resolve(root, rel, field=f"{field}.predictions.{variant}")
            if not prediction_path.is_file():
                raise DatasetContractError(f"Missing predictions: {prediction_path}")
            expected_prediction_hashes = raw.get("prediction_sha256", {})
            if expected_prediction_hashes is not None and not isinstance(expected_prediction_hashes, dict):
                raise DatasetContractError(f"{field}.prediction_sha256 must be an object")
            expected_prediction_sha = (expected_prediction_hashes or {}).get(variant)
            if verify_hashes and expected_prediction_sha and expected_prediction_sha != _sha256(prediction_path):
                raise DatasetContractError(f"Prediction SHA-256 mismatch for {sample_id}/{variant}")
            prediction_paths[variant] = prediction_path
        slices = raw.get("slices", {})
        if not isinstance(slices, dict):
            raise DatasetContractError(f"{field}.slices must be an object")
        mm_per_pixel = raw.get("mm_per_pixel")
        if mm_per_pixel is not None:
            mm_per_pixel = _positive_number(mm_per_pixel, f"{field}.mm_per_pixel")
        samples.append(EvaluationSample(
            sample_id=sample_id,
            width=width,
            height=height,
            image_path=image_path,
            annotations=annotations,
            prediction_paths=prediction_paths,
            slices={str(k): str(v) for k, v in slices.items()},
            mm_per_pixel=mm_per_pixel,
            label_status=label_status,
        ))
    return EvaluationDataset(
        manifest_path=manifest_path,
        manifest_sha256=_sha256(manifest_path),
        dataset_id=dataset_id,
        split=split,
        classes=tuple(classes_raw),
        samples=tuple(samples),
        metadata={k: v for k, v in manifest.items() if k not in {"samples", "classes"}},
    )
