# Stage 8 Remediation — Phase 4 Acceptance Record

## Automated verification

Commands:

```bash
pytest -q tests -W error::pytest.PytestUnraisableExceptionWarning
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Results:

```text
Core suite: 326 passed
Full suite: 482 passed
Failures: 0
Errors: 0
Skipped: 0
PytestUnraisableExceptionWarning: 0
```

JUnit files:

```text
docs/test-results/phase4_core.xml
docs/test-results/phase4_full.xml
```

## Deterministic verdict regression

Reference:

```text
tests/fixtures/sample_plan.ifc
data/mabhas_clauses.json, excluding skip_category clauses
```

Phase 3 and Phase 4 results are identical for all 341 findings:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

No article, element, message, measured value, required value, unit, unsupported
flag, or verdict changed.

## Reference Quality output

The reference IFC produces three expected alerts:

```text
QC-SPACE-004 Rkit
  declared area 6 m² vs boundary-derived 9 m²

QC-SPACE-004 Rbath
  declared area 4 m² vs boundary-derived 9 m²

QC-PARAM-001
  wall_height was not asserted by the operator
```

Valid IFC Storey containment, room boundaries, and Door connectivity produce
no false findings.

## Phase-4 exit-gate evidence

- malformed rooms generate blocking Quality findings;
- missing area produces `QC-SPACE-004` and its numeric clause remains
  `NOT_EVALUATED`;
- endpoint overflow is detected for a 900 mm opening centred at 3900 mm on a
  4000 mm wall;
- start/end placement conventions normalize to centre offset;
- missing host geometry is not silently passed;
- Window internal connectivity and Door connectivity conflicts are reported;
- vertical Door/Window fit uses resolved per-element values;
- supported input units normalize once at the adapter boundary;
- Storey identity is preserved from IFC containment.

## Generated artifacts

```text
artifacts/phase4_acceptance/compliance_result.json
artifacts/phase4_acceptance/compliance_report.html
artifacts/phase4_acceptance/compliance_report.pdf
artifacts/phase4_acceptance/compliance_issues.bcf
artifacts/phase4_acceptance/pipeline_execution.json
```
