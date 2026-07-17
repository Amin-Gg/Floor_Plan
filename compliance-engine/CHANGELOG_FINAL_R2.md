# Final R2 Changelog

This release closes the independent post-Phase-9 review findings and replaces
`stage8-phase9-final` as the release candidate.

## Blocking fixes closed

### 1. Removed direct compliance bypass

- Deleted obsolete `services/orchestrator.py`.
- Moved the deterministic prepared-input runner to
  `validation/compliance/runner.py`.
- Renamed the callable to private `_run_compliance_core()`.
- Removed the `building_params` argument and all low-level parameter merging.
- The only supported production entry point is now
  `services.validation_pipeline.run_validation_pipeline()`.
- Manual Inputs v1 validation and provenance resolution always run before the
  deterministic engine.

### 2. Clarified enriched `bim_data` contract

- Value-bearing enriched `bim_data` is now documented as an internal,
  output-only deterministic-agent seam.
- Public callers must submit original raw `bim_data` plus Manual Inputs v1, or
  reuse a typed `BuildingModel` in-process.
- Empty or `_provided`-only legacy blocks remain tolerated because they carry no
  verdict-driving values.
- Value-bearing `building_params` blocks are rejected at HTTP and unified
  pipeline boundaries.

### 3. Enforced trustworthy BCF anchors

- Removed internal-ID-only / markup-only BCF topic support.
- Every exported BCF topic now has a real IFC `GlobalId` component selection.
- Findings without a trustworthy IFC GUID remain in JSON/HTML/PDF.
- Every excluded finding is recorded in the BCF manifest with an explicit skip
  reason.

## Additional hardening

- Acceptance artifact checks now verify that JSON, HTML, PDF and BCF paths
  exist and are non-empty, not merely truthy.
- Updated architecture, migration, Manual Inputs and BCF documentation.
- Added regression tests for removed public orchestrator entry point,
  output-only enriched seams and strict BCF skip policy.
- Bumped package version to `1.0.1` and report engine version to
  `stage8-remediation-phase9-final-r2`.

## Dead/obsolete files removed

- `services/orchestrator.py` — obsolete public bypass and flat parameter merge.
- `docs/IFC_INGESTION.md` — stale pre-remediation implementation document that
  referenced removed APIs and mutable alias behavior.
- Runtime caches (`__pycache__`, `.pytest_cache`, `*.pyc`) are excluded from
  the final source and delivery trees. Historical phase evidence remains for traceability;
  authoritative R2 evidence is under `_delivery/`.

## Verdict policy

No deterministic compliance verdict was intentionally changed by this release.
The reference IFC + full corpus remains:

```text
PASS:            15
FAIL:             9
NEEDS_REVIEW:   309
NOT_EVALUATED:    8
```
