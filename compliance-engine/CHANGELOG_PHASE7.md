# Changelog — Stage 8 Remediation Phase 7

## Scope

Phase 7 replaces the temporary staged-report dictionary with a versioned,
authoritative `ValidationReport` model. JSON, HTML, and PDF are now generated
from the same report object, with one overall-status policy, deterministic
finding order, stable element identity, explicit skipped-stage reasons, and a
published JSON Schema.

## Added

- `ValidationReport` v1.0 and typed report/model/overall-status contracts.
- Central overall-status policy for full checks and prechecks.
- JSON Schema Draft 2020-12 contract:
  `reporting/schemas/validation_report_v1.schema.json`.
- JSON schema validation on every generated machine report.
- Dedicated JSON, HTML, and PDF renderers under `reporting/`.
- Deterministic flat finding list across schema, quality, and compliance stages.
- Portable model metadata: source type, model name, IFC schema, project GUID,
  and model fingerprint.
- Checker, engine, semantic-catalog, and controlled-values versions in reports.
- Sanitization of secrets and deployment-local absolute paths.
- Explicit materialization of skipped stages and their reasons.
- Precheck and schema-rejection report artifacts when report generation is
  requested.
- Phase 7 schema, determinism, identity, sanitization, pipeline, and status
  policy tests.

## Changed

- `services.report_generator.generate_reports()` is now a compatibility facade
  over `ValidationReport`; it no longer computes status independently or render
  from unrelated raw stage dictionaries.
- HTML and PDF are rendered from the exact same report model.
- Compliance findings preserve both the canonical internal ID and the legacy
  source-facing `element_id`, while also retaining IFC `GlobalId` separately.
- Overall status no longer claims compliance when required data is missing.
- Precheck reports use `precheck_*` states and never claim regulatory
  compliance.
- Schema-invalid submissions can produce a rejection report that records the
  skipped Quality and Compliance stages.
- `jsonschema` is now an explicit runtime dependency.
- Report JSON moved from temporary `report_version: stage3` to
  `report_schema_version: 1.0`.

## Compatibility

- Existing `generate_reports(result, meta, out_dir, coverage, stages)` callers
  remain supported for one transition release.
- Existing `_overall_status`, `_ORDER`, and `_VERDICT_STYLE` test/import seams
  remain available.
- Existing HTML/PDF/BCF filenames are unchanged.
- The current minimal BCF topic exporter is retained, but Full BCF component
  selection and real-viewer interoperability remain Phase 8 work.
- All 341 reference compliance finding payloads are unchanged from Phase 6,
  excluding report-only stable identifiers/order.

## Verification

See:

- `docs/PHASE7_IMPLEMENTATION.md`
- `docs/PHASE7_ACCEPTANCE.md`
- `docs/VALIDATION_REPORT_SCHEMA.md`
- `docs/test-results/phase7_core.xml`
- `docs/test-results/phase7_full.xml`
