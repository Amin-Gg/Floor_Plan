# Phase 8 Changelog — Full BCF XML 2.1

## Added

- `reporting/bcf_exporter.py`
- BCF XML 2.1 topic, viewpoint, component-selection, camera, coloring, and
  snapshot generation
- deterministic topic/viewpoint UUIDs
- BCF project file and export manifest
- generated-output XSD conformance subsets under
  `reporting/schemas/bcf_2_1/`
- BCF validator CLI and Phase 8 acceptance CLI
- Phase 8 BCF contract and corruption tests
- implementation, acceptance, and interoperability documentation
- direct `lxml` and `Pillow` requirements

## Changed

- `services.report_generator.generate_reports()` now uses the Full BCF exporter
  and returns `bcf_manifest`
- unanchored `NOT_EVALUATED` findings stay in JSON/HTML/PDF instead of becoming
  empty BCF topics
- legacy `_build_bcf()` remains as a compatibility adapter for internal-ID
  markup-only topics

## Preserved

- deterministic compliance verdicts
- Validation Report v1.0 JSON contract
- HTML/PDF rendering behavior
- legacy internal element reference links during the transition period

## Known external verification limitation

No independent desktop BCF viewer was available in the execution environment.
Automated XML/archive/component-selection verification is complete; manual GUI
verification is documented but not falsely claimed.
