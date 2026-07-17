# ADR-008 — Ground-Truth ML Evaluation and Verdict-Impact Gate

## Status

Accepted for Phase 8.

## Context

The legacy `/analyze_accuracy` endpoint calculated confidence distribution, overlap counts, and heuristic reliability. It did not use ground truth and therefore could not measure accuracy, precision, recall, F1, IoU, mAP, calibration, scale error, or downstream compliance impact.

Publishing those confidence-derived values as model accuracy would be methodologically invalid. In addition, a detector can look acceptable at the object level while still causing dangerous downstream behavior, such as turning a real compliance `FAIL` into `PASS`.

## Decision

Phase 8 introduces a separate, offline, versioned evaluation boundary:

```text
sealed holdout dataset
        ↓
sealed model inference
        ↓
versioned prediction documents
        ↓
deterministic metric evaluator
        ↓
geometry + calibration + scale + slices
        ↓
compliance verdict impact
        ↓
release policy gate
```

Metric calculation is intentionally separated from inference. This provides deterministic re-evaluation of the same predictions without GPU availability or model downloads.

## Claim-safety rules

1. Synthetic data can validate evaluator mathematics, but cannot support a real model-quality claim.
2. A train or validation split cannot be presented as final test performance.
3. The holdout split must be human-verified or adjudicated.
4. The test manifest, annotations, images, predictions, model weights, and engine reports must be SHA-256 sealed.
5. `critical_false_pass` must equal zero for the evaluated safety-critical rule set.
6. Any tuning after reading holdout results invalidates that holdout; a new holdout is required.
7. Confidence diagnostics remain available for operations, but are explicitly marked `accuracy_claim=false`.

## Metrics

The evaluator reports:

- precision, recall, and F1 at a declared confidence and IoU operating point;
- AP@0.50, AP@0.75, and COCO-style mAP@0.50:0.95;
- mask IoU where both sides have masks, otherwise bbox IoU;
- center error in pixels and millimetres;
- bbox width and height relative error;
- orientation error;
- wall centerline Hausdorff error;
- scale absolute and relative error;
- expected calibration error and Brier score;
- metrics by declared dataset slice;
- exact verdict agreement, false fail, review-to-pass, and critical false pass.

## Matching

Predictions are ordered deterministically by confidence, sample ID, and prediction ID. Each prediction can match at most one ground-truth instance of the same class in the same image. Each ground-truth instance can be used once per IoU threshold.

## Release policy

`config/phase8_evaluation_policy.json` contains provisional dataset coverage and metric thresholds. These thresholds remain provisional until the first adjudicated holdout baseline is reviewed. The evaluator still enforces the non-negotiable safety rule that critical false pass must be zero.

## Dataset leakage prevention

- image hashes are unique inside a split;
- the audit command can compare multiple split manifests and fails on shared image hashes;
- annotation and prediction hashes can be embedded in the manifest;
- the holdout is stored outside the source repository and mounted read-only during evaluation.

## Consequences

### Positive

- no confidence metric can be mistaken for real accuracy;
- A/B preprocessing decisions are based on the same locked examples;
- model and pipeline changes can be connected to compliance outcomes;
- evaluation can be rerun from sealed predictions without a GPU;
- safety regressions are visible even when aggregate mAP improves.

### Cost

- a real Phase-8 score requires an adjudicated dataset and sealed external weights;
- verdict impact requires reference and predicted IFC/engine outputs;
- dataset creation and adjudication are substantial human tasks.
