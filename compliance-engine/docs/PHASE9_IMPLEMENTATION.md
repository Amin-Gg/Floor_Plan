# Phase 9 Implementation

## Objective

Complete final regression and cleanup after the eight implementation phases,
without changing deterministic compliance behavior.

## Work completed

### Canonical production entry points

The system now has one production entry point for each major responsibility:

```text
Pipeline     services.validation_pipeline.run_validation_pipeline
Quality      validation.quality.run_model_quality_checks
Schema       validation.schema
Standards    standards.catalog_api
Reporting    reporting.generator.generate_report_bundle
BCF          reporting.bcf_exporter
```

Deleted compatibility modules no longer compete with these canonical paths.

### Source contract terminology

The public source type is now `bim_data`, not `legacy_bim_data`. This reflects
that the upstream floor-plan model remains a supported boundary format while
the internal contract is `BuildingModel`.

### API cleanup

FastAPI and Celery both construct `PipelineRequest` directly. The API accepts
Manual Inputs v1.0 for both JSON and IFC jobs. Removed flat `building_params`
input is rejected explicitly rather than silently interpreted.

### Final acceptance model

The acceptance script validates a real IFC source, then creates a deterministic
canonical acceptance variant with:

- three IFC Spaces, one intentionally malformed;
- the original IFC Window forced beyond its Wall endpoint;
- a second synthetic Window;
- different manual dimensions for both windows;
- complete Mabhas clause corpus evaluation.

The real IFC identities remain on source elements, allowing Full BCF component
selection to be checked against the source file.

### Historical evidence

Per-phase changelogs and acceptance records are retained. They document the
migration and are not runtime dependencies.
