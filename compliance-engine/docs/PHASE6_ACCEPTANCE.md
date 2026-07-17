# Stage 8 Remediation — Phase 6 Acceptance Record

## Exit-gate evidence

- disconnected Project/Site/Building/Storey entities no longer pass;
- wrong spatial parent types are reported with element GUIDs and details;
- duplicate IFC `GlobalId` values are blocking;
- mandatory explicit attributes are validated from IfcOpenShell schema metadata;
- required aggregate lower bounds are enforced;
- default supported schemas are IFC4, IFC4X1, and IFC4X3 family;
- IFC4X2 is rejected by default;
- IFC2X3 requires explicit policy opt-in;
- schema and ingest share one `ParsedIfcSource` object;
- a pipeline test proves one IFC open per run;
- precheck and full-check apply the same blocking schema gate;
- the historical `ingest.schema_validator` import seam remains compatible.

## Reference model result

```text
Schema: passed
Checker: stage8-remediation-phase6
Schema identifier: IFC4
Schema findings: 0
Single parse context: true
```

## Deterministic compliance regression

Reference input:

```text
tests/fixtures/sample_plan.ifc
data/mabhas_clauses.json excluding skip_category entries
```

Phase 5 and Phase 6 are identical for all 341 compliance findings:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

The exact compliance finding payloads, excluding stable identifier fields, are
unchanged.

## Automated verification

```text
Core suite: 352 passed
Full suite: 508 passed
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
docs/test-results/phase6_core.xml
docs/test-results/phase6_full.xml
```

Generated artifacts:

```text
artifacts/phase6_acceptance/compliance_result.json
artifacts/phase6_acceptance/compliance_report.html
artifacts/phase6_acceptance/compliance_report.pdf
artifacts/phase6_acceptance/compliance_issues.bcf
artifacts/phase6_acceptance/pipeline_execution.json
artifacts/phase6_acceptance/verdict_comparison_phase5.json
```
