# Phase 1 Change Log

## Added

- `domain/` canonical model package.
- `validation/compliance/adapter.py` bidirectional compatibility adapter.
- `reporting/report_model.py` report-facing shared result wrapper.
- Phase 1 domain and reporting contract tests.
- Phase 0 sanitization record.

## Modified

- `ingest/ifc_to_bim_data.py`: canonical model boundary, deterministic identity,
  IFC GUID preservation, parsed-model reuse, spatial identity metadata.
- `ingest/ifc_pipeline.py`: reuses the schema gate's parsed IFC model.
- `ingest/schema_validator.py`: shared Finding and ValidationResult contracts.
- `services/numeric_checker.py`: imports/re-exports the shared Finding/Verdict;
  keeps the legacy summary contract.
- `services/quality_checker.py`: shared result contract and identity enrichment.
- `services/orchestrator.py`: shared compliance-stage result and identity
  enrichment.
- `pyproject.toml`: includes new runtime packages.
- `.gitignore` and baseline documentation: Phase 0 final hygiene correction.

## Deleted

- No runtime file was deleted.

## Verdict behavior

No intended deterministic verdict change. Existing tests and the full suite
remain green.

## Known warnings

Two pre-existing `ifcopenshell.file.__del__` warnings remain in corrupt-IFC
schema tests. They do not fail the suite and are not introduced by Phase 1.
