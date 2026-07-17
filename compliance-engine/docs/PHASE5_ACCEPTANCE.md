# Stage 8 Remediation — Phase 5 Acceptance Record

## Exit-gate evidence

- the semantic catalog is versioned and validated;
- missing/invalid catalog files fail loud;
- changing a YAML IFC mapping changes actual ingest output;
- changing `required_for` in YAML changes Quality dependency metadata;
- numeric clause property vocabulary and dimensions come from the catalog;
- no duplicate Python semantic property map remains;
- Persian and English room aliases normalize identically;
- unknown values retain their raw form and are not guessed;
- request-specific aliases do not mutate shared process state;
- parallel requests with different aliases remain isolated;
- service startup validates both contracts.

## Deterministic verdict regression

Reference input:

```text
tests/fixtures/sample_plan.ifc
data/mabhas_clauses.json excluding skip_category entries
```

Phase 4 and Phase 5 are identical for all 341 compliance findings:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

The exact finding payloads, excluding stable identifier fields, are unchanged.

## Reference Quality output

```text
QC-SPACE-004 Rkit
QC-SPACE-004 Rbath
QC-PARAM-001
```

## Generated artifacts

```text
artifacts/phase5_acceptance/compliance_result.json
artifacts/phase5_acceptance/compliance_report.html
artifacts/phase5_acceptance/compliance_report.pdf
artifacts/phase5_acceptance/compliance_issues.bcf
artifacts/phase5_acceptance/pipeline_execution.json
```

Automated counts and clean-clone evidence are recorded in the deliverable's
`GIT_EVIDENCE_PHASE5.txt` and JUnit XML files.

## Automated verification

```text
Core suite: 338 passed
Full suite: 494 passed
Failures: 0
Errors: 0
Skipped: 0
PytestUnraisableExceptionWarning: 0
```

Commands:

```bash
pytest -q tests -W error::pytest.PytestUnraisableExceptionWarning
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```
