# Phase 2 Change Log

## Added

- `services/validation_pipeline.py`
  - `PipelineRequest`
  - `PipelineExecution`
  - `PipelineSourceType`
  - `PipelineMode`
  - authoritative `run_validation_pipeline`
- `manual_inputs/`
  - schema dataclasses;
  - strict parser;
  - deterministic merger;
  - resolved-value provenance;
  - backward-compatible flat adapter.
- `ingest/ifc_io.py`
  - safe low-level parse-status check preventing the IfcOpenShell destructor warning.
- Phase 2 pipeline, manual-input and API tests.
- Manual-input and IfcOpenShell technical documentation.

## Changed

- raw and IFC public entry points now delegate to one orchestrator;
- raw `bim_data` now runs Quality before compliance;
- manual values merge before Quality;
- IFC schema and ingest reuse one successful parse;
- Quality checker accepts additional precomputed manual-input findings;
- `/analyze` accepts a versioned `manual_inputs` object;
- `/analyze-ifc` accepts a `manual_inputs` JSON form field;
- Celery and in-process tasks carry manual inputs;
- package metadata includes `manual_inputs`.

## Deprecated

- flat `building_params` payloads;
- direct use of `run_pipeline`, `run_pipeline_from_ifc` and
  `run_ifc_compliance` as architectural entry points. They remain compatibility
  wrappers for the transition release.

## Removed behavior

- raw-data Quality bypass;
- malformed-IFC `PytestUnraisableExceptionWarning` from the IfcOpenShell Python wrapper.

## Deterministic verdict impact

No deterministic verdict regression was observed on the full clause corpus and
reference IFC fixture. The compared 341 rows remained identical.
