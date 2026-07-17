# Stage 8 Remediation — Phase 2 Implementation

## Scope

Phase 2 implements:

- REM-ARCH-003 — one validation orchestrator;
- REM-TEST-002 — pipeline contract tests;
- REM-INPUT-001 — Manual Inputs Schema v1.0;
- REM-INPUT-002 — per-window, per-door and per-wall overrides;
- removal of the IfcOpenShell malformed-file destructor warning.

## Authoritative pipeline

The single orchestrator is:

```python
services.validation_pipeline.run_validation_pipeline(PipelineRequest(...))
```

Supported sources:

```text
ifc
building_model
legacy_bim_data
```

Supported modes:

```text
precheck     schema where applicable + quality
full_check   schema where applicable + quality + compliance + reporting
```

Stage order:

```text
parse source
→ schema validation for IFC
→ canonical BuildingModel
→ manual-input merge
→ category/confidence preparation
→ Quality validation
→ deterministic compliance
→ optional RAG advisory
→ reporting
```

`PipelineExecution.stage_trace` records the actual order.

## Compatibility wrappers

The following functions remain available but delegate to the authoritative
orchestrator:

```text
api.pipeline.run_pipeline
api.pipeline.run_pipeline_from_ifc
ingest.ifc_pipeline.run_ifc_compliance
```

The raw-data path can no longer bypass Quality.

## Schema blocking

Direct use of the new orchestrator returns a structured blocked execution with:

- schema findings;
- skipped-stage reasons;
- no Quality or compliance execution.

Legacy wrappers continue to raise `IfcSchemaError`, preserving API/task
behavior while carrying the full structured schema result.

## Manual-input merge

The merge operates on `BuildingModel`, not raw IFC or arbitrary dictionaries.
Every final value records source and confidence. Per-element overrides may use
internal ID, IFC GUID or source ID.

## Async/API wiring

Manual Inputs v1.0 is supported by:

- JSON `POST /analyze` through `manual_inputs`;
- multipart `POST /analyze-ifc` through the `manual_inputs` form field;
- in-process thread jobs;
- Celery jobs.

The deprecated `building_params` form remains for one transition release.

## Non-goals

Phase 2 does not implement:

- Quality plugin migration — Phase 3;
- full Room/Space checks — Phase 4;
- catalog single-source migration — Phase 5;
- Report Schema v1.0 — Phase 7;
- Full BCF interoperability — Phase 8.
