# Changelog — Phase 9 Final Regression, Cleanup, and Release

## Purpose

Phase 9 closes the remediation program. It removes one-release compatibility
entry points, consolidates production APIs, adds the final acceptance scenario,
updates all current documentation, and creates the final transferable release.

## Removed production compatibility modules

- `services/report_generator.py`
- `services/quality_checker.py`
- `ingest/ifc_pipeline.py`
- `ingest/schema_validator.py`
- `ingest/semantic_catalog.py`

Their production responsibilities now belong to:

- `reporting/generator.py`
- `validation/quality/`
- `services/validation_pipeline.py`
- `validation/schema/`
- `standards/catalog_api.py`

## Public pipeline cleanup

- Removed deprecated public wrappers such as `run_pipeline_from_ifc` and
  `run_ifc_compliance` from production code.
- All CLI, API, Celery, and internal calls now construct `PipelineRequest` and
  call `run_validation_pipeline`.
- Renamed the source contract from `legacy_bim_data` to `bim_data`.
- Renamed adapter functions to `building_model_from_bim_data` and
  `building_model_to_bim_data`.
- Renamed `PipelineExecution.to_legacy_response()` to `to_api_response()`.

## Manual Inputs cleanup

- Removed the old flat `building_params` parser from the production pipeline.
- Removed flat-input compatibility metadata and deprecation paths.
- `/analyze-ifc` explicitly rejects `building_params` and points clients to
  Manual Inputs Schema v1.0.
- The only public input contract is now the versioned nested schema.

## Standards cleanup

- Moved the typed semantic-catalog query API to `standards/catalog_api.py`.
- Removed the ingest-layer semantic-catalog facade.
- Updated production and test imports to the canonical standards package.
- Corrected catalog compatibility validation to continue reading the YAML
  `compatibility.required_pset_properties` section.

## Quality and reporting cleanup

- Removed the old Quality wrapper; production code runs the plugin registry.
- Removed the old report facade; production code uses
  `reporting.generator.generate_report_bundle`.
- Updated tests to exercise canonical modules rather than deleted wrappers.

## Final acceptance scenario

Added:

- `scripts/run_validation_acceptance.py`
- `tests/fixtures/remediation_manual_inputs.json`
- `tests/pipeline/test_phase9_acceptance.py`

The scenario starts from a real IFC file and verifies:

- successful IFC Schema validation;
- distinct Manual Input overrides for two windows;
- missing Space area;
- invalid/open Space boundary;
- opening endpoint overflow;
- real compliance `FAIL` and `NOT_EVALUATED` outcomes;
- JSON, HTML, PDF, and Full BCF generation;
- BCF component GUIDs against the source IFC.

## Documentation

Added or fully refreshed:

- `README.md`
- `ARCHITECTURE.md`
- `CHANGELOG_PHASE9.md`
- `PROJECT_CHANGES_PHASE0_TO_PHASE9.md`
- `docs/MIGRATION_PHASE9.md`
- `docs/PHASE9_IMPLEMENTATION.md`
- `docs/PHASE9_ACCEPTANCE.md`

## Regression policy

The unchanged reference IFC and complete clause corpus retain all 341 Phase 8
compliance findings and the exact summary:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

No deterministic verdict behavior was intentionally changed in Phase 9.
