"""Deterministic, ground-truth based model and downstream metrics."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

import numpy as np

from .dataset import EvaluationDataset, load_prediction
from .geometry import (
    center_distance,
    circular_angle_error,
    instance_iou,
    polyline_hausdorff,
    relative_size_error,
)


@dataclass(frozen=True)
class EvaluationConfig:
    confidence_threshold: float = 0.5
    operating_iou: float = 0.5
    ap_iou_thresholds: tuple[float, ...] = tuple(round(0.5 + 0.05 * index, 2) for index in range(10))
    calibration_bins: int = 10


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _average_precision(tp: list[int], fp: list[int], total_gt: int) -> float:
    if total_gt <= 0:
        return 0.0
    tp_cum = np.cumsum(tp, dtype=float)
    fp_cum = np.cumsum(fp, dtype=float)
    recalls = tp_cum / total_gt
    precisions = tp_cum / np.maximum(tp_cum + fp_cum, 1e-12)
    values = []
    for recall_level in np.linspace(0.0, 1.0, 101):
        eligible = precisions[recalls >= recall_level]
        values.append(float(np.max(eligible)) if eligible.size else 0.0)
    return float(np.mean(values))


def _prediction_records(dataset: EvaluationDataset, variant: str) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    payloads: dict[str, dict[str, Any]] = {}
    bases: dict[str, Path] = {}
    classes = set(dataset.classes)
    for sample in dataset.samples:
        path = sample.prediction_paths.get(variant)
        if path is None:
            raise ValueError(f"Prediction variant {variant!r} is missing for sample {sample.sample_id}")
        payloads[sample.sample_id] = load_prediction(path, sample_id=sample.sample_id, classes=classes)
        bases[sample.sample_id] = path.parent
    return payloads, bases


def _class_ap(dataset: EvaluationDataset, predictions: dict[str, dict[str, Any]], pred_bases: dict[str, Path], class_name: str, iou_threshold: float) -> tuple[float, list[dict[str, Any]]]:
    ground_truth: dict[str, list[dict[str, Any]]] = {}
    total_gt = 0
    for sample in dataset.samples:
        rows = [row for row in sample.annotations["instances"] if row["class_name"] == class_name]
        ground_truth[sample.sample_id] = rows
        total_gt += len(rows)
    records: list[tuple[float, str, dict[str, Any]]] = []
    for sample in dataset.samples:
        for prediction in predictions[sample.sample_id]["instances"]:
            if prediction["class_name"] == class_name:
                records.append((prediction["confidence"], sample.sample_id, prediction))
    records.sort(key=lambda item: (-item[0], item[1], item[2]["id"]))
    matched = {sample_id: set() for sample_id in ground_truth}
    tp: list[int] = []
    fp: list[int] = []
    diagnostics: list[dict[str, Any]] = []
    samples_by_id = {sample.sample_id: sample for sample in dataset.samples}
    for score, sample_id, prediction in records:
        sample = samples_by_id[sample_id]
        best_index = None
        best_iou = -1.0
        best_kind = "bbox"
        gt_base = dataset.manifest_path.parent
        for index, truth in enumerate(ground_truth[sample_id]):
            if index in matched[sample_id]:
                continue
            iou, kind = instance_iou(
                truth,
                prediction,
                width=sample.width,
                height=sample.height,
                gt_base=gt_base,
                pred_base=pred_bases[sample_id],
            )
            if iou > best_iou:
                best_iou, best_index, best_kind = iou, index, kind
        correct = best_index is not None and best_iou >= iou_threshold
        if correct:
            matched[sample_id].add(best_index)
        tp.append(1 if correct else 0)
        fp.append(0 if correct else 1)
        diagnostics.append({
            "sample_id": sample_id,
            "prediction_id": prediction["id"],
            "confidence": score,
            "correct": correct,
            "matched_iou": max(0.0, best_iou),
            "iou_kind": best_kind,
        })
    return _average_precision(tp, fp, total_gt), diagnostics


def _operating_matches(dataset: EvaluationDataset, predictions: dict[str, dict[str, Any]], pred_bases: dict[str, Path], class_name: str, config: EvaluationConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tp = fp = fn = 0
    matches: list[dict[str, Any]] = []
    for sample in dataset.samples:
        truths = [row for row in sample.annotations["instances"] if row["class_name"] == class_name]
        preds = [row for row in predictions[sample.sample_id]["instances"] if row["class_name"] == class_name and row["confidence"] >= config.confidence_threshold]
        preds.sort(key=lambda row: (-row["confidence"], row["id"]))
        used: set[int] = set()
        for prediction in preds:
            candidates: list[tuple[float, int, str]] = []
            for index, truth in enumerate(truths):
                if index in used:
                    continue
                iou, kind = instance_iou(
                    truth,
                    prediction,
                    width=sample.width,
                    height=sample.height,
                    gt_base=dataset.manifest_path.parent,
                    pred_base=pred_bases[sample.sample_id],
                )
                candidates.append((iou, index, kind))
            best = max(candidates, default=(-1.0, -1, "bbox"), key=lambda item: (item[0], -item[1]))
            if best[0] >= config.operating_iou:
                used.add(best[1])
                truth = truths[best[1]]
                matches.append({
                    "sample_id": sample.sample_id,
                    "class_name": class_name,
                    "ground_truth": truth,
                    "prediction": prediction,
                    "iou": best[0],
                    "iou_kind": best[2],
                    "mm_per_pixel": sample.mm_per_pixel,
                })
                tp += 1
            else:
                fp += 1
        fn += len(truths) - len(used)
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}, matches


def _summary(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not rows:
        return {"count": 0, "mean": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(rows),
        "mean": mean(rows),
        "median": median(rows),
        "p95": float(np.percentile(rows, 95)),
        "max": max(rows),
    }


def _geometry_metrics(matches: list[dict[str, Any]]) -> dict[str, Any]:
    center_px: list[float] = []
    center_mm: list[float] = []
    width_rel: list[float] = []
    height_rel: list[float] = []
    angles: list[float] = []
    hausdorff_px: list[float] = []
    hausdorff_mm: list[float] = []
    matched_ious: list[float] = []
    mask_matches = 0
    for match in matches:
        truth = match["ground_truth"]
        prediction = match["prediction"]
        distance = center_distance(truth["bbox_xyxy"], prediction["bbox_xyxy"])
        center_px.append(distance)
        mmpp = match.get("mm_per_pixel")
        if mmpp:
            center_mm.append(distance * mmpp)
        width_error, height_error = relative_size_error(truth["bbox_xyxy"], prediction["bbox_xyxy"])
        width_rel.append(width_error)
        height_rel.append(height_error)
        matched_ious.append(match["iou"])
        if match["iou_kind"] == "mask":
            mask_matches += 1
        gt_attrs, pred_attrs = truth.get("attributes", {}), prediction.get("attributes", {})
        if isinstance(gt_attrs.get("orientation_deg"), (int, float)) and isinstance(pred_attrs.get("orientation_deg"), (int, float)):
            angles.append(circular_angle_error(float(gt_attrs["orientation_deg"]), float(pred_attrs["orientation_deg"])))
        gt_line, pred_line = gt_attrs.get("centerline"), pred_attrs.get("centerline")
        if isinstance(gt_line, list) and isinstance(pred_line, list):
            value = polyline_hausdorff(gt_line, pred_line)
            if value is not None:
                hausdorff_px.append(value)
                if mmpp:
                    hausdorff_mm.append(value * mmpp)
    return {
        "matched_iou": _summary(matched_ious),
        "mask_based_matches": mask_matches,
        "center_error_px": _summary(center_px),
        "center_error_mm": _summary(center_mm),
        "bbox_width_relative_error": _summary(width_rel),
        "bbox_height_relative_error": _summary(height_rel),
        "orientation_error_deg": _summary(angles),
        "centerline_hausdorff_px": _summary(hausdorff_px),
        "centerline_hausdorff_mm": _summary(hausdorff_mm),
    }


def _calibration(records: list[dict[str, Any]], bins: int) -> dict[str, Any]:
    if not records:
        return {"count": 0, "ece": None, "brier": None, "bins": []}
    confidences = np.array([row["confidence"] for row in records], dtype=float)
    outcomes = np.array([1.0 if row["correct"] else 0.0 for row in records], dtype=float)
    bin_rows = []
    ece = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        mask = (confidences >= lower) & (confidences <= upper if index == bins - 1 else confidences < upper)
        count = int(mask.sum())
        if count:
            confidence = float(confidences[mask].mean())
            accuracy = float(outcomes[mask].mean())
            ece += count / len(records) * abs(confidence - accuracy)
        else:
            confidence = accuracy = None
        bin_rows.append({"lower": lower, "upper": upper, "count": count, "mean_confidence": confidence, "accuracy": accuracy})
    return {"count": len(records), "ece": float(ece), "brier": float(np.mean((confidences - outcomes) ** 2)), "bins": bin_rows}


def _scale_metrics(dataset: EvaluationDataset, predictions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    absolute: list[float] = []
    relative: list[float] = []
    rows: list[dict[str, Any]] = []
    for sample in dataset.samples:
        gt = sample.mm_per_pixel
        scale = predictions[sample.sample_id].get("scale") or {}
        pred = scale.get("mm_per_pixel")
        if gt is None or not isinstance(pred, (int, float)) or isinstance(pred, bool):
            continue
        error = abs(float(pred) - gt)
        rel = error / gt
        absolute.append(error)
        relative.append(rel)
        rows.append({"sample_id": sample.sample_id, "ground_truth": gt, "predicted": float(pred), "absolute_error": error, "relative_error": rel})
    return {"count": len(rows), "absolute_error_mm_per_pixel": _summary(absolute), "relative_error": _summary(relative), "within_1_percent": sum(row["relative_error"] <= 0.01 for row in rows), "within_2_percent": sum(row["relative_error"] <= 0.02 for row in rows), "samples": rows}


def _verdict_metrics(dataset: EvaluationDataset, predictions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    statuses = {"PASS", "FAIL", "NEEDS_REVIEW", "NOT_EVALUATED"}
    matrix = {truth: {pred: 0 for pred in statuses} for truth in statuses}
    compared = exact = false_pass = false_fail = review_miss = 0
    for sample in dataset.samples:
        truth_rows = sample.annotations.get("verdicts", {})
        pred_rows = predictions[sample.sample_id].get("verdicts", {})
        for rule_id in sorted(set(truth_rows) & set(pred_rows)):
            truth, pred = truth_rows[rule_id], pred_rows[rule_id]
            if truth not in statuses or pred not in statuses:
                continue
            compared += 1
            matrix[truth][pred] += 1
            exact += truth == pred
            false_pass += truth == "FAIL" and pred == "PASS"
            false_fail += truth == "PASS" and pred == "FAIL"
            review_miss += truth == "NEEDS_REVIEW" and pred == "PASS"
    return {
        "compared_rules": compared,
        "exact_agreement": _safe_ratio(exact, compared),
        "critical_false_pass": false_pass,
        "false_fail": false_fail,
        "needs_review_to_pass": review_miss,
        "confusion_matrix": matrix,
    }


def _slice_reports(dataset: EvaluationDataset, variant: str, config: EvaluationConfig) -> dict[str, Any]:
    dimensions: dict[str, set[str]] = {}
    for sample in dataset.samples:
        for key, value in sample.slices.items():
            dimensions.setdefault(key, set()).add(value)
    reports: dict[str, Any] = {}
    for dimension, values in sorted(dimensions.items()):
        reports[dimension] = {}
        for value in sorted(values):
            subset = EvaluationDataset(
                manifest_path=dataset.manifest_path,
                manifest_sha256=dataset.manifest_sha256,
                dataset_id=f"{dataset.dataset_id}:{dimension}={value}",
                split=dataset.split,
                classes=dataset.classes,
                samples=tuple(sample for sample in dataset.samples if sample.slices.get(dimension) == value),
                metadata=dataset.metadata,
            )
            if subset.samples:
                report = evaluate_dataset(subset, variant=variant, config=config, include_slices=False)
                reports[dimension][value] = {
                    "samples": len(subset.samples),
                    "macro_f1": report["summary"]["macro_f1"],
                    "map_50_95": report["summary"]["map_50_95"],
                    "verdict_exact_agreement": report["verdict_impact"]["exact_agreement"],
                }
    return reports


def evaluate_dataset(dataset: EvaluationDataset, *, variant: str, config: EvaluationConfig | None = None, include_slices: bool = True) -> dict[str, Any]:
    config = config or EvaluationConfig()
    predictions, pred_bases = _prediction_records(dataset, variant)
    class_reports: dict[str, Any] = {}
    all_calibration: list[dict[str, Any]] = []
    all_matches: list[dict[str, Any]] = []
    for class_name in dataset.classes:
        aps: dict[str, float] = {}
        operating, matches = _operating_matches(dataset, predictions, pred_bases, class_name, config)
        all_matches.extend(matches)
        diagnostics_at_50: list[dict[str, Any]] = []
        for threshold in config.ap_iou_thresholds:
            ap, diagnostics = _class_ap(dataset, predictions, pred_bases, class_name, threshold)
            aps[f"{threshold:.2f}"] = ap
            if threshold == 0.5:
                diagnostics_at_50 = diagnostics
                all_calibration.extend(diagnostics)
        support = sum(1 for sample in dataset.samples for row in sample.annotations["instances"] if row["class_name"] == class_name)
        class_reports[class_name] = {
            "support": support,
            **operating,
            "ap50": aps.get("0.50", 0.0),
            "ap75": aps.get("0.75", 0.0),
            "map_50_95": mean(aps.values()) if aps else 0.0,
            "ap_by_iou": aps,
            "calibration": _calibration(diagnostics_at_50, config.calibration_bins),
            "geometry": _geometry_metrics(matches),
        }
    supported = [row for row in class_reports.values() if row["support"] > 0]
    summary = {
        "samples": len(dataset.samples),
        "instances": sum(row["support"] for row in class_reports.values()),
        "macro_precision": mean(row["precision"] for row in supported) if supported else 0.0,
        "macro_recall": mean(row["recall"] for row in supported) if supported else 0.0,
        "macro_f1": mean(row["f1"] for row in supported) if supported else 0.0,
        "map_50": mean(row["ap50"] for row in supported) if supported else 0.0,
        "map_75": mean(row["ap75"] for row in supported) if supported else 0.0,
        "map_50_95": mean(row["map_50_95"] for row in supported) if supported else 0.0,
    }
    result = {
        "schema_version": "1.0",
        "dataset": {
            "id": dataset.dataset_id,
            "split": dataset.split,
            "manifest": str(dataset.manifest_path),
            "manifest_sha256": dataset.manifest_sha256,
            "label_statuses": sorted({sample.label_status for sample in dataset.samples}),
            "prediction_sha256": {
                sample.sample_id: __import__("hashlib").sha256(sample.prediction_paths[variant].read_bytes()).hexdigest()
                for sample in dataset.samples
            },
        },
        "variant": variant,
        "config": {"confidence_threshold": config.confidence_threshold, "operating_iou": config.operating_iou, "ap_iou_thresholds": list(config.ap_iou_thresholds), "calibration_bins": config.calibration_bins},
        "empirical_claims_allowed": dataset.empirical_claims_allowed,
        "summary": summary,
        "classes": class_reports,
        "geometry_overall": _geometry_metrics(all_matches),
        "calibration_overall": _calibration(all_calibration, config.calibration_bins),
        "scale": _scale_metrics(dataset, predictions),
        "verdict_impact": _verdict_metrics(dataset, predictions),
    }
    if include_slices:
        result["slices"] = _slice_reports(dataset, variant, config)
    return result


def compare_variants(reports: dict[str, dict[str, Any]], baseline: str) -> dict[str, Any]:
    if baseline not in reports:
        raise ValueError(f"Unknown baseline variant: {baseline}")
    base = reports[baseline]
    comparisons = {}
    for name, report in sorted(reports.items()):
        if name == baseline:
            continue
        comparisons[name] = {
            "delta_macro_f1": report["summary"]["macro_f1"] - base["summary"]["macro_f1"],
            "delta_map_50_95": report["summary"]["map_50_95"] - base["summary"]["map_50_95"],
            "delta_ece": (report["calibration_overall"]["ece"] or 0.0) - (base["calibration_overall"]["ece"] or 0.0),
            "delta_scale_relative_error_mean": (report["scale"]["relative_error"]["mean"] or 0.0) - (base["scale"]["relative_error"]["mean"] or 0.0),
            "delta_verdict_exact_agreement": report["verdict_impact"]["exact_agreement"] - base["verdict_impact"]["exact_agreement"],
            "critical_false_pass": report["verdict_impact"]["critical_false_pass"],
        }
    return {"baseline": baseline, "comparisons": comparisons}
