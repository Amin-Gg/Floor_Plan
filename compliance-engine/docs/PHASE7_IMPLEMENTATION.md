# Stage 8 Remediation — Phase 7 Implementation

## Objective

Phase 7 establishes one report contract for all validation outputs. Prior to
this phase, `services/report_generator.py` assembled a temporary staged JSON
object and computed HTML/PDF status independently from the pipeline contracts.
That design allowed schema, quality, compliance, and rendered output to drift.

The Phase 7 architecture is:

```text
Schema ValidationResult
Quality ValidationResult
ComplianceResult
BuildingModel + pipeline metadata
              ↓
       ValidationReport v1.0
              ↓
     ┌────────┼────────┐
     JSON     HTML     PDF
                         \
                          current minimal BCF topic adapter
```

All status and finding semantics are resolved before rendering.

## Authoritative model

`reporting.report_model.ValidationReport` contains:

```text
report_schema_version
engine_version
run_id
generated_at
mode
model
overall
stages
summary
findings
metadata
skipped_stages
```

### Model identity

The report carries only portable model identity:

```text
name
source_type
ifc_schema
project_guid
fingerprint
```

Absolute local/server paths are not emitted. IFC-sourced element findings carry
both:

```text
element_internal_id
element_ifc_guid
```

The legacy source-facing `element_id` remains in the compatibility portion of
the finding payload for one transition release.

## Shared finding shape

Every schema, quality, and compliance finding is normalized to the shared
`domain.findings.Finding` serialization. Stage findings and the flat top-level
`findings[]` therefore use the same fields and stable IDs.

The flat list is sorted by:

1. stage;
2. severity;
3. verdict priority;
4. check/rule code;
5. clause ID;
6. IFC/internal element identity;
7. stable finding ID.

This makes report ordering reproducible even when an upstream list arrives in a
different order.

## Central overall-status policy

Only `compute_overall_status()` determines the report status.

### Full-check precedence

```text
schema failed
  → rejected

compliance FAIL exists
  → non_compliant

compliance blocked / NOT_EVALUATED exists
  → incomplete

NEEDS_REVIEW exists
  → needs_review

quality failed
  → incomplete

quality alerts remain
  → compliant_with_quality_alerts

otherwise
  → compliant
```

A missing-data result is never labelled compliant.

### Precheck policy

Precheck never claims regulatory compliance:

```text
quality failed
  → precheck_failed

quality alerts
  → precheck_ready_with_alerts

otherwise
  → precheck_ready
```

The Compliance stage is represented as skipped with an explicit reason.

## JSON Schema

Every JSON artifact is validated against:

```text
reporting/schemas/validation_report_v1.schema.json
```

The schema uses JSON Schema Draft 2020-12 and validates:

- required top-level fields;
- UUID and date-time formats;
- valid mode and overall-status vocabularies;
- one shared finding shape;
- stage structure and skip semantics;
- identity fields;
- summary counters.

A report that fails schema validation is not written successfully.

## Rendering

### JSON

`reporting/json_report.py` validates and writes the authoritative artifact with
UTF-8, no NaN values, and stable field/list ordering.

### HTML

`reporting/html_report.py` consumes only `ValidationReport`. It does not inspect
IFC, rerun checks, or infer status. It renders:

- model and run identity;
- overall status and reasons;
- schema, quality, and compliance stages;
- skipped-stage reasons;
- deterministic findings;
- coverage information;
- engine/checker/catalog versions.

### PDF

`reporting/pdf_report.py` renders the exact Phase 7 HTML using WeasyPrint. Thus
HTML and PDF cannot disagree about status or findings.

### BCF boundary

Phase 7 keeps the existing minimal BCF 2.1 topic archive for compatibility. It
is now fed from the canonical flat report findings, but it does **not** claim
Full BCF interoperability. Real IFC component selection, viewpoints, and viewer
validation belong to Phase 8.

## Sensitive-data policy

Report metadata is recursively sanitized:

- credential/token/password/API-key fields are omitted;
- absolute Unix and Windows paths are reduced to portable filenames;
- model source paths are never emitted;
- known source paths are removed from nested messages/metadata;
- arbitrary non-JSON and non-finite values are normalized safely.

This is covered by automated tests.

## Pipeline integration

`services.validation_pipeline` passes the canonical `BuildingModel`, pipeline
mode, and skipped-stage reasons into report generation.

When requested:

- full checks generate normal reports;
- prechecks generate reports with Compliance explicitly skipped;
- schema-blocked IFC input generates a rejection report with Quality and
  Compliance explicitly skipped.

## Compatibility adapter

`services.report_generator.generate_reports()` remains the public transition
adapter. Existing callers may continue passing legacy result/stage dictionaries,
but those dictionaries are immediately converted into one `ValidationReport`.
No renderer consumes the legacy shapes directly.
