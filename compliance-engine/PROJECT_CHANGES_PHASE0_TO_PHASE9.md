# Complete Project Change History — Phase 0 through Phase 9

## Document purpose

This document explains what changed during the complete Stage 8 remediation
program, why each change was necessary, and what the final system can now do.
It is intended for the development team, thesis supervisor, future maintainers,
and anyone auditing the implementation.

## Starting point

The original Stage 8 project already contained a working deterministic
compliance engine, IFC ingestion, a preliminary Schema/Quality pipeline, RAG
retrieval, HTML/PDF reporting, and a minimal BCF archive. Its strongest property
was the deterministic verdict spine: numeric and spatial `PASS`/`FAIL` decisions
were produced by Python rather than an LLM.

The audit found that continued development would become unsafe because:

- different entry points executed different stages;
- IFC, Quality, Compliance, and reporting exchanged loosely structured dicts;
- finding contracts were split;
- Quality checks lived in one growing file;
- manual inputs were flat and could not address individual windows;
- Room/Space checks were incomplete;
- semantic mappings and aliases were duplicated in Python;
- IFC hierarchy/GUID checks were incomplete;
- reports were not based on one versioned model;
- BCF topics could not reliably select IFC elements;
- transitional compatibility paths remained after the new architecture existed.

The remediation kept the validated deterministic agents and rebuilt the
surrounding architecture in controlled phases.

---

# Phase 0 — Baseline and Safety

## Goal

Create a reproducible, auditable baseline before changing architecture.

## What changed

- Created the remediation Git branch and tagged the sanitized Stage 8 baseline.
- Recorded the exact commit, branch, and Git bundle evidence.
- Captured the full dependency environment with `pip freeze`.
- Saved before/after JUnit test reports.
- Added three Architecture Decision Records:
  - canonical `BuildingModel`;
  - shared `Finding` and `ValidationResult`;
  - one authoritative pipeline.
- Investigated the single failing verdict-regression test.
- Corrected the test without deleting, skipping, or weakening it.
- Replaced process-randomized `hash()` usage with deterministic SHA-256.
- Explicitly tested that unsupported findings do not receive AI advisory text,
  while eligible interpretive findings may receive it.
- Corrected the baseline API documentation.
- Documented that per-window overrides did not yet exist.
- Restored comprehensive `.gitignore` protection and documented the intentional
  exclusion of `.env` from the sanitized baseline.

## Result

- Reproducible baseline established.
- Full baseline suite became green.
- No production runtime behavior changed.
- The architectural rules for later phases were frozen before implementation.

---

# Phase 1 — Canonical Domain and Shared Result Contracts

## Goal

Stop using ad hoc dictionaries as the internal architectural contract while
preserving the proven deterministic agents.

## What changed

### Canonical model

Added the `domain/` package with typed models for:

- `BuildingModel`;
- `Storey`;
- `Wall`;
- `Door`;
- `Window`;
- `Space`;
- geometry and units;
- model and element provenance.

### Identity separation

Each element now retains separate:

```text
internal_id
ifc_guid
source_id
model_name
```

Stable internal IDs use deterministic UUID5 generation. IFC `GlobalId` remains
available independently for BCF and external coordination.

### Compatibility seam

Added a bidirectional canonical model ↔ engine mapping adapter so the existing
agents could continue operating without a risky rewrite.

### Shared findings

Created one `Finding` contract and common enums for:

- stage;
- severity;
- verdict;
- element identity;
- expected/actual values;
- clause references;
- stable finding IDs.

Created one `ValidationResult` stage contract with centralized status policy.

## Result

- Internal architecture gained a typed domain model.
- IFC GUIDs and engine IDs stopped being overloaded.
- Schema, Quality, and Compliance could emit compatible findings.
- Existing deterministic verdicts remained unchanged.

---

# Phase 2 — Unified Pipeline and Manual Inputs v1.0

## Goal

Make every public entry point execute the same validation stages and add proper
operator input for dimensions unavailable from a 2D plan.

## What changed

### One orchestrator

Added:

```text
PipelineRequest
PipelineExecution
PipelineSourceType
PipelineMode
run_validation_pipeline
```

All source types now follow the same order:

```text
parse
→ schema when IFC
→ BuildingModel
→ manual merge
→ quality
→ compliance
→ advisory
→ report
```

Raw `bim_data` can no longer bypass Quality checks.

### Manual Inputs v1.0

Added a strict versioned schema supporting:

- project defaults;
- finished floor level;
- storey and wall height;
- default door/window dimensions;
- per-window overrides;
- per-door overrides;
- per-wall overrides;
- unmatched-override policy;
- range/type/cross-field validation;
- resolved-value provenance.

Merge order was defined and tested. Manual input is merged before Quality, so
Quality sees the same resolved model later used by Compliance.

### IfcOpenShell warning fix

The malformed-IFC read path was corrected at the wrapper boundary. A failed
parse no longer leaves a partially initialized object whose destructor raises a
`KeyError`. The warning was removed at its source rather than hidden with a
warning filter.

## Result

- One pipeline contract across CLI, API, tasks, IFC, and in-memory data.
- Independent dimensions can be supplied for individual windows.
- Missing operator inputs are explicit and traceable.
- The historical IfcOpenShell unraisable warning was eliminated.

---

# Phase 3 — Plugin-Based Quality Layer

## Goal

Prevent the Quality checker from becoming a new monolith.

## What changed

Added `validation/quality/` with:

- `QualityCheck` protocol;
- immutable per-run `QualityContext`;
- explicit ordered registry;
- plugin executor;
- common quality-finding helpers;
- plugin error/isolation policy.

Existing checks were separated into plugins for:

- contract read;
- space tagging;
- element confidence;
- scale confidence;
- manual parameters;
- opening placement.

Plugin failures produce a visible `QC-INTERNAL-001` finding according to policy.
A non-blocking plugin failure does not suppress the rest of the stage.

## Result

- New Quality checks can be added independently.
- Plugin order and execution metadata are explicit.
- Validators work on `BuildingModel`, not raw dictionaries.
- Compliance verdict behavior remained unchanged.

---

# Phase 4 — Complete Model Quality Checks

## Goal

Validate whether the model contains trustworthy information before attempting
regulatory compliance.

## What changed

### Room/Space checks

Implemented checks for:

- missing Space representation;
- incomplete identity and IFC GUID;
- missing human-readable tag;
- missing or invalid area;
- missing boundary;
- open, non-finite, zero-area, or self-intersecting boundary;
- missing or invalid storey;
- enclosed regions without Space tags;
- Door-to-Space connectivity;
- overlapping Spaces.

### Required properties and units

Added:

- required semantic property validation;
- wrong mapping evidence;
- missing/unsupported unit findings;
- normalization to millimetres and square metres at the boundary.

### Storey checks

Added missing storey, unknown storey reference, and FFL consistency checks.
IFC ingest now preserves actual spatial containment.

### Opening placement

Completed Door/Window/Wall placement with:

- host existence;
- host geometry;
- axis distance;
- opening width versus wall length;
- endpoint span overflow;
- internal-window warnings;
- Door connectivity consistency;
- vertical fit against wall height.

### Geometry API

Added deterministic helpers for:

- polygon area;
- closure;
- centroid;
- validity;
- overlap;
- coverage;
- containment;
- boundary distance.

## Result

- Model-quality deficiencies are distinguished from regulatory failures.
- Only dependent checks become `NOT_EVALUATED`; one bad Space does not stop the
  entire project.
- Quality findings became useful corrective actions rather than generic alerts.

---

# Phase 5 — Semantic Catalog and Controlled Values

## Goal

Make configuration the single source of truth instead of duplicating mappings
and aliases across Python modules.

## What changed

Added:

```text
standards/semantic_property_catalog.yaml
standards/controlled_values.yaml
standards/loaders.py
standards/models.py
```

The semantic catalog now drives:

- IFC entity/property extraction;
- property aliases;
- Pset mappings;
- data types;
- units;
- required properties;
- min/max values;
- compliance dependencies;
- numeric clause semantics.

Controlled values now define multilingual aliases for:

- room types;
- booleans;
- occupancy types;
- door types.

Startup validates both files and fails fast on malformed configuration.
Request-specific aliases are isolated; no request mutates process-global state.
Unknown values preserve the original raw value instead of being guessed.

## Result

- YAML changes affect ingest and Quality consistently.
- Python mapping duplication was removed.
- Persian and English normalization became versioned and testable.
- Parallel requests cannot leak aliases into one another.

---

# Phase 6 — Complete IFC Schema Gate

## Goal

Ensure that model structure is valid before Quality or Compliance runs.

## What changed

Added `validation/schema/` with:

- explicit immutable `SchemaValidationPolicy`;
- `ParsedIfcSource` shared between validation and ingest;
- supported-version policy;
- relationship-aware spatial hierarchy validation;
- duplicate `GlobalId` detection;
- mandatory-attribute checks using IfcOpenShell schema metadata;
- single-parse tests;
- mode-independent blocking tests.

The gate validates:

```text
IfcProject
  → IfcSite
    → IfcBuilding
      → IfcBuildingStorey
```

Entity existence alone is no longer sufficient.

Default accepted families are explicit. IFC2X3 requires compatibility opt-in
instead of being accepted accidentally by a prefix match.

## Result

- Invalid IFC structure cannot proceed into downstream stages.
- Duplicate GUIDs and missing mandatory attributes are visible blocking errors.
- The file is parsed once and reused by ingest.

---

# Phase 7 — ValidationReport v1.0

## Goal

Generate every output format from one versioned and validated report model.

## What changed

Added:

- authoritative `ValidationReport v1.0`;
- central overall-status policy;
- deterministic flattened finding order;
- model metadata and fingerprints;
- checker/catalog version metadata;
- skipped-stage reasons;
- precheck and schema-rejection reports;
- dedicated JSON, HTML, and PDF renderers;
- Draft 2020-12 JSON Schema;
- automatic JSON Schema validation;
- secret and absolute-path sanitization.

Overall status now distinguishes:

- schema rejection;
- non-compliance;
- incomplete evaluation;
- human review;
- compliance with quality alerts;
- clean compliance;
- precheck-only states.

## Result

- JSON, HTML, and PDF cannot disagree about stage state or overall status.
- Machine output has a published versioned contract.
- Reports do not falsely claim compliance when data is missing.
- Finding identities and ordering are reproducible.

---

# Phase 8 — Full BCF 2.1

## Goal

Produce useful model-coordination issues rather than a markup-only archive.

## What changed

Added a Full BCF exporter that produces:

- `bcf.version`;
- project metadata;
- stable issue topics;
- `markup.bcf`;
- `viewpoint.bcfv`;
- component selection using real IFC `GlobalId`;
- geometry-derived camera and snapshot when trustworthy;
- deterministic UUIDs;
- export manifest;
- BCF structure/XML/XSD validation;
- corruption tests;
- source-IFC GUID cross-checking;
- BCF validation and acceptance CLIs.

Only findings with a trustworthy model anchor become BCF topics. Global and
unanchored findings remain in JSON/HTML/PDF instead of creating unusable topics.

## Result

- BCF topics can select real IFC elements.
- Viewpoints and snapshots support model review.
- Topic identity is stable between identical runs.
- Automated interoperability verification is complete.
- Desktop GUI viewer verification remains honestly documented as a manual
  external step where no viewer is installed.

---

# Phase 9 — Final Cleanup, Regression, and Release

## Goal

Remove transition-only paths, prove the entire project in one acceptance run,
and deliver a maintainable final repository.

## What changed

### Removed compatibility production modules

Deleted:

```text
services/report_generator.py
services/quality_checker.py
ingest/ifc_pipeline.py
ingest/schema_validator.py
ingest/semantic_catalog.py
```

Canonical replacements are now the only production paths.

### Consolidated production APIs

- CLI, FastAPI, Celery, and tests use `PipelineRequest` and
  `run_validation_pipeline`.
- Source terminology changed from `legacy_bim_data` to `bim_data`.
- Response helper renamed to `to_api_response`.
- Catalog access moved to `standards.catalog_api`.
- Reporting uses `reporting.generator.generate_report_bundle`.
- Quality uses the plugin registry directly.

### Removed flat Manual Inputs

- The public flat `building_params` parser was removed.
- Manual Inputs v1.0 is the only supported public input.
- The API explicitly rejects the removed field with a migration message.

### Final acceptance scenario

Added a one-command acceptance run that starts from a real IFC and verifies:

- successful Schema validation;
- two windows with different per-element manual dimensions;
- one malformed Space;
- one opening beyond its host Wall;
- missing semantic data;
- real `FAIL`;
- real `NOT_EVALUATED`;
- JSON/HTML/PDF/BCF output;
- source-IFC BCF GUID validation.

### Documentation and delivery

Added final architecture, migration, acceptance, and complete history documents.
Created a transferable Git bundle, final tag, JUnit evidence, acceptance
artifacts, file manifest, and checksums.

## Result

- The project has one clear implementation path for every architectural
  responsibility.
- Transitional files no longer confuse future development.
- The full unchanged reference scenario preserves all Phase 8 deterministic
  verdicts.
- The final repository is easier to extend than the original Stage 8 codebase.

---

# Final system behavior

## Reference regression result

The unchanged reference IFC and complete regulation corpus produce 341
compliance findings:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

The Phase 9 behavioral rows are identical to Phase 8 for the compared fields.

## Final outputs

```text
compliance_result.json
compliance_report.html
compliance_report.pdf
compliance_issues.bcf
compliance_issues.bcf.manifest.json
```

## Final extension model

To add a property:

1. update the semantic catalog;
2. add/adjust tests;
3. avoid embedding a duplicate mapping in Python.

To add a Quality check:

1. implement the `QualityCheck` protocol;
2. register the plugin explicitly;
3. declare its codes and blocking policy;
4. add isolated and pipeline tests.

To add a deterministic rule evaluator:

1. use canonical or adapter-normalized data;
2. return `NOT_EVALUATED` for missing trustworthy measurements;
3. never delegate `PASS`/`FAIL` to an LLM;
4. add verdict-regression coverage.

## Known limitations

- The full Mabhas corpus is not fully automatable. Interpretive and unsupported
  clauses remain `NEEDS_REVIEW`.
- Missing model data remains `NOT_EVALUATED`; the engine does not invent values.
- BCF desktop GUI viewer import remains a documented external manual check when
  no compatible viewer is installed in the execution environment.
- The deterministic agents still consume one explicit mapping adapter. This is
  intentional: rewriting validated agents solely to remove the seam would add
  risk without architectural benefit.

## Final conclusion

The remediation did not replace the working compliance engine. It preserved the
deterministic core and built a reliable architecture around it:

```text
valid model
→ trustworthy semantic data
→ deterministic checking
→ explainable, versioned, model-addressable results
```

---

# Final R2 — Post-Phase-9 Independent Review Closure

After the Phase 0–9 delivery, an independent review found three remaining
architecture gaps. Final R2 closes them without changing the reference
compliance verdicts:

1. The direct `run_compliance(..., building_params=...)` bypass was removed by
   deleting the obsolete public orchestrator module and moving the deterministic
   runner behind the unified pipeline as private prepared-input code.
2. The enriched `bim_data` seam is now explicitly output-only; public reruns use
   raw `bim_data` plus Manual Inputs v1, while trusted internal reuse uses
   `BuildingModel`.
3. BCF now exports only real IFC-GUID-addressable topics. Internal-only findings
   are explicitly skipped in the manifest and retained in JSON/HTML/PDF.

Acceptance artifact existence checks were hardened, package/report versions were
bumped to R2, stale generated evidence was removed from the source tree, and a
new final delivery package was generated from a clean Git tag.
