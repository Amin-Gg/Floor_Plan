# Stage 8 Remediation — Phase 7 Acceptance Record

## Exit-gate evidence

- one authoritative `ValidationReport` feeds JSON, HTML, and PDF;
- every generated JSON report validates against the published v1.0 schema;
- Schema, Quality, and Compliance use one normalized finding shape;
- all findings have valid stable UUIDs;
- element findings preserve internal IDs and IFC GlobalIds separately;
- overall status is computed centrally and never inferred by renderers;
- schema-invalid input is never labelled compliant;
- missing required data prevents a clean compliant state;
- precheck never claims regulatory compliance;
- skipped stages and reasons are materialized;
- engine, checker, and standards versions are recorded;
- absolute paths and secrets are excluded from portable reports;
- deterministic finding order is tested;
- HTML and PDF are rendered from the same report object;
- the legacy report-generator entry point remains compatible;
- current minimal BCF behavior is retained without claiming Phase 8 completion.

## Reference model

```text
Input:
  tests/fixtures/sample_plan.ifc

Clauses:
  data/mabhas_clauses.json excluding skip_category entries

Schema:
  passed

Quality:
  passed_with_alerts

Report schema:
  1.0

Overall:
  non_compliant
```

## Deterministic compliance regression

Phase 6 and Phase 7 remain identical for all 341 compliance findings:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

The exact compliance finding payloads, excluding report-only stable identifiers
and deterministic report ordering, are unchanged.

## Report-level findings

The flat report contains Schema + Quality + Compliance findings. On the
reference model it contains 344 findings because the Quality stage contributes
three model-quality alerts in addition to the 341 compliance findings.

## Generated artifacts

```text
artifacts/phase7_acceptance/compliance_result.json
artifacts/phase7_acceptance/compliance_report.html
artifacts/phase7_acceptance/compliance_report.pdf
artifacts/phase7_acceptance/compliance_issues.bcf
artifacts/phase7_acceptance/pipeline_execution.json
artifacts/phase7_acceptance/verdict_comparison_phase6.json
```

## Automated verification

```text
Core suite: 368 passed
Full suite: 524 passed
Failures: 0
Errors: 0
Skipped: 0
PytestUnraisableExceptionWarning: 0
```

Commands:

```bash
python -m compileall api ingest services domain validation reporting manual_inputs standards
pytest -q tests -W error::pytest.PytestUnraisableExceptionWarning
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

JUnit evidence:

```text
docs/test-results/phase7_core.xml
docs/test-results/phase7_full.xml
```

The final delivery also contains a clean-clone JUnit run generated from the
transferable Phase 7 Git bundle.
