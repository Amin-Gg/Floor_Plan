# Dead and Obsolete Files Removed in Final R2

## Removed runtime code

- `services/orchestrator.py`
  - exposed the obsolete direct compliance entry point;
  - accepted/merged flat `building_params` outside Manual Inputs v1;
  - allowed public callers to bypass the unified validation pipeline.

Its deterministic implementation was moved to the private
`validation/compliance/runner.py` module. The public production entry point is
`services.validation_pipeline.run_validation_pipeline()`.

## Removed stale documentation

- `docs/IFC_INGESTION.md`
  - described removed functions and pre-remediation architecture;
  - instructed callers to use entry points that no longer exist;
  - described mutable Python aliases that were replaced by the standards YAML.

## Excluded generated runtime debris

The final Git archive and ZIP contain no:

- `__pycache__/` directories;
- `.pytest_cache/` directories;
- `*.pyc` files;
- `.env` files;
- local build directories.

Historical phase evidence is retained where it remains useful for audit
traceability. Authoritative R2 evidence is under `_delivery/`.
