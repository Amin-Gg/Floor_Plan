# Compliance Engine Final R2 — Repair and Release Report

**Final Git tag:** `stage8-phase9-final-r2`  
**Final commit:** `2fd6476351e1460d688e787eed2a71042004b54d`  
**Package version:** `1.0.1`  
**Report engine version:** `stage8-remediation-phase9-final-r2`

## Release decision

All blocking findings from the independent review of
`compliance-engine-final-reviewfixes.zip` are closed. The resulting code was
committed, tagged, exported through a complete Git bundle, cloned into a clean
directory, tested again, and used to generate the final acceptance artifacts.

## 1. Direct `run_compliance` bypass — closed

### Previous defect

The old public `services.orchestrator.run_compliance()` accepted either an
embedded flat `building_params` block or a `building_params=` argument. This
allowed verdict-driving values to bypass Manual Inputs v1 validation,
provenance resolution, stage ordering, and Quality validation.

### Final correction

- Removed `services/orchestrator.py`.
- Moved deterministic execution to the private module
  `validation/compliance/runner.py`.
- Renamed the callable to `_run_compliance_core()`.
- Removed the `building_params` argument and low-level merge logic.
- `services.validation_pipeline.run_validation_pipeline()` is the only
  supported production entry point.
- Added regression tests proving the old module and public function no longer
  exist and the private runner has no `building_params` parameter.

## 2. Enriched `bim_data` seam ambiguity — closed

### Final contract

- Raw public `bim_data` is accepted only at the unified pipeline boundary.
- Operator values are accepted only through Manual Inputs Schema v1.0.
- Value-bearing enriched `bim_data` produced after merge is an internal,
  output-only deterministic-agent seam.
- Trusted in-process reuse uses `BuildingModel`.
- External reruns submit the original raw mapping plus the Manual Inputs v1
  document again.
- Empty or `_provided`-only legacy blocks remain tolerated because they carry
  no verdict-driving values.
- Value-bearing legacy blocks are rejected consistently by HTTP and pipeline
  boundaries.

## 3. BCF trustworthy-anchor policy — closed

- Removed support for internal-ID-only markup topics.
- Every exported BCF topic now has a real IFC `GlobalId` and viewpoint component
  selection.
- Findings without a trustworthy IFC GUID remain in JSON, HTML, and PDF.
- Every excluded finding receives an explicit skip reason in the BCF manifest.
- `QC-IDENT-001` never fabricates an IFC GUID or creates an unselectable topic.

## 4. Acceptance artifact gate — hardened

The release acceptance command now verifies that JSON, HTML, PDF, and BCF
outputs:

- have a returned path;
- exist as files;
- have non-zero size.

A missing PDF reports the WeasyPrint/pango/cairo installation remedy instead of
failing with an unexplained assertion.

## 5. Dead and obsolete files removed

### Runtime code

- `services/orchestrator.py` — obsolete bypass and flat parameter merger.

### Documentation

- `docs/IFC_INGESTION.md` — stale pre-remediation guide referencing removed
  APIs, mutable aliases, and superseded pipeline architecture.

### Generated debris excluded

- `__pycache__/`
- `.pytest_cache/`
- `*.pyc`
- `.env`
- local `build/` and temporary wheel directories

Historical phase evidence that remains useful for traceability was retained.

## 6. Independent verification

### Full suite from clean Git-bundle clone

```text
548 tests
548 passed
0 failures
0 errors
0 skipped
0 PytestUnraisableExceptionWarning
```

### Reference verdict parity

```text
PASS:            15
FAIL:             9
NEEDS_REVIEW:   309
NOT_EVALUATED:    8
TOTAL:           341
```

The IFC and raw `bim_data` public entry points produced identical compliance
signatures for the reference model.

### Final acceptance

```text
ok: true
Schema: passed
Quality: intentionally failed by injected acceptance defects
JSON: generated and non-empty
HTML: generated and non-empty
PDF: generated and non-empty
BCF: generated and non-empty
BCF topics/viewpoints: 18 / 18
```

Manual overrides remained distinct:

```text
Wb.width_mm = 1400
W-ACCEPT-02.width_mm = 900
```

Every BCF component GUID was cross-checked against the source IFC and found in
that model.

## 7. Final package contents

- source tree from `stage8-phase9-final-r2`;
- complete Git bundle with Phase 0–9 history and R2 tag;
- clean-clone JUnit XML;
- final acceptance JSON/HTML/PDF/BCF and manifest;
- verdict and entry-point comparison;
- wheel `mabhas_compliance_engine-1.0.1-py3-none-any.whl`;
- Git evidence;
- file manifest;
- SHA-256 checksums;
- original independent audit and R2 closure documentation.

## Remaining non-blockers

- Desktop GUI BCF import remains an external manual verification because no
  independent desktop viewer is installed in the execution environment.
- The large `NEEDS_REVIEW` population is a product/rule-automation frontier,
  not an unresolved architecture defect.
