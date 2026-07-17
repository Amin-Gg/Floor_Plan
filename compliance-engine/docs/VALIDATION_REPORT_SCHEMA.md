# Validation Report Schema v1.0

## Canonical files

```text
Python model:
  reporting/report_model.py

JSON Schema:
  reporting/schemas/validation_report_v1.schema.json

JSON writer/validator:
  reporting/json_report.py
```

## Top-level example

```json
{
  "report_schema_version": "1.0",
  "engine_version": "stage8-remediation-phase7",
  "run_id": "11111111-1111-4111-8111-111111111111",
  "generated_at": "2026-07-10T12:00:00Z",
  "mode": "full_check",
  "model": {
    "name": "sample_plan.ifc",
    "source_type": "ifc",
    "ifc_schema": "IFC4",
    "project_guid": "...",
    "fingerprint": "..."
  },
  "overall": {
    "code": "non_compliant",
    "label": "non-compliant",
    "status": "error",
    "reasons": ["9 deterministic compliance failure(s) found."]
  },
  "stages": {
    "schema": {},
    "quality": {},
    "compliance": {}
  },
  "summary": {},
  "findings": [],
  "metadata": {},
  "skipped_stages": {}
}
```

## Report modes

```text
precheck
full_check
```

A precheck includes Schema/Quality results and an explicit skipped Compliance
stage. It must not use a `compliant` overall code.

## Overall codes

```text
rejected
non_compliant
incomplete
needs_review
compliant_with_quality_alerts
compliant
precheck_failed
precheck_ready_with_alerts
precheck_ready
```

## Stage structure

Every materialized stage contains:

```text
stage
status
checker_version
started_at
completed_at
metadata
summary
coverage
duration_s
skipped
skip_reason
findings
```

Skipped stages remain present in the `stages` object with:

```json
{
  "skipped": true,
  "skip_reason": "blocked by IFC schema failure",
  "findings": []
}
```

## Finding identity

Every finding has a valid stable UUID `finding_id` and includes independent
identity fields:

```text
element_internal_id
element_ifc_guid
element_id          (legacy compatibility)
model_name
model_fingerprint
storey_id
```

BCF must use `element_ifc_guid` in Phase 8. The legacy `element_id` must not be
used as an IFC component GUID.

## Summary semantics

The top-level summary counts all findings, including Schema and Quality:

```text
findings_total
findings_by_stage
findings_by_severity
verdicts
coverage
```

The Compliance stage retains its own compliance-only summary. Therefore the
flat report may contain more `NOT_EVALUATED` findings than the Compliance
summary because model-quality deficiencies also use that safe verdict.

## Determinism

With fixed `generated_at` and `run_id`, semantically identical input produces
byte-equivalent JSON data regardless of upstream finding order. Runtime runs
normally receive a unique time-derived run ID while stable finding IDs remain
unchanged.

## Validation

Use:

```python
from reporting.json_report import validate_report_dict

validate_report_dict(report_dict)
```

or write through:

```python
from reporting.json_report import write_json_report

write_json_report(report, "compliance_result.json")
```

Every production JSON report is validated before writing.

## Compatibility period

The old `generate_reports(result, meta, ..., stages=...)` signature remains for
one transition release. Its output is Report v1.0; the temporary `stage3`
report shape is no longer generated.
