# Final R2 Independent-Review Closure

## Decision

All blocking findings from `FINAL_REVIEW_FIXES_INDEPENDENT_AUDIT.md` are closed
in this release.

## R2-001 — Direct orchestrator bypass

**Closed.** `services/orchestrator.py` was deleted. The deterministic runner is
private implementation code at `validation/compliance/runner.py` and has no
`building_params` argument. Public IFC, `bim_data` and `BuildingModel` requests
enter through `run_validation_pipeline()`.

## R2-002 — Enriched seam contract

**Closed by contract clarification.** Value-bearing enriched `bim_data` is an
internal output-only agent seam. It is deliberately rejected if resubmitted as
public raw `bim_data`. Trusted in-process reuse uses `BuildingModel`; external
reruns use the original raw mapping plus Manual Inputs v1.

## R2-003 — Strict BCF anchor policy

**Closed.** The exporter creates topics only for findings with a real IFC
`GlobalId` found in the canonical model. Internal-ID-only findings are skipped
with an explicit manifest reason and remain visible in JSON/HTML/PDF.

## Release evidence

The release package contains:

- complete source;
- Git history and `stage8-phase9-final-r2` tag;
- transferable Git bundle;
- full JUnit result;
- acceptance outputs;
- verdict comparison;
- wheel `1.0.1`;
- file manifest and SHA-256 checksums.
