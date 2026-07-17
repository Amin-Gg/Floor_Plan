# Phase 3 Change Log

## Added

- `validation/quality/` plugin framework:
  - protocol;
  - immutable per-run context;
  - explicit ordered registry;
  - plugin executor;
  - shared quality-finding helpers;
  - plugin isolation and error policy.
- Independent built-in check modules for:
  - contract read;
  - room-category tagging;
  - element confidence;
  - scale confidence;
  - manual parameters;
  - opening placement.
- Phase 3 registry, isolation, ordering, compatibility and pipeline tests.
- Phase 3 implementation and acceptance documentation.

## Changed

- the authoritative pipeline now runs Quality plugins on the canonical
  `BuildingModel` after normalization/review preparation;
- `services/quality_checker.py` is reduced to a legacy compatibility wrapper;
- package metadata includes `validation.quality` and its built-in checks;
- quality stage metadata now records registry order, executed checks, skipped
  checks and failed checks;
- checker version is now `quality-stage8-phase3`.

## Error handling

- plugin exceptions are logged and converted to `QC-INTERNAL-001`;
- non-blocking plugin failures do not stop remaining checks;
- blocking plugin failures centrally produce a failed Quality stage;
- invalid plugin return types, stages and undeclared codes are not silently
  ignored.

## Compatibility

- the legacy `run_quality_checks(bim_data, additional_findings=...)` API remains
  available for one transition period;
- its stage dictionary and `bim_data["_quality"]` behavior are retained.

## Scope not included

Phase 4 model-completeness checks were not falsely marked implemented. Room
geometry, property, unit, storey and endpoint-aware placement work remains for
the next phase.

## Deterministic verdict impact

No deterministic compliance verdict regression was observed on the full clause
corpus and reference IFC fixture.
