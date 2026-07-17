# Stage 8 Remediation — Phase 3 Acceptance Record

## Scope

- REM-QC-001 — plugin-based Quality Check architecture
- migration of every existing quality check out of the monolithic service
- compatibility wrapper for legacy callers
- plugin registry, isolation and error-policy tests

## Acceptance matrix

| Requirement | Result |
|---|---|
| `QualityCheck` protocol exists | PASS |
| Explicit ordered registry exists | PASS |
| Registry rejects duplicate names/prefixes/codes | PASS |
| Existing checks migrated to independent modules | PASS |
| Old `services/quality_checker.py` is only a wrapper | PASS |
| New validators consume `BuildingModel` | PASS |
| Adding a check requires registration, not editing a central `if/elif` block | PASS |
| Plugin exception produces visible `QC-INTERNAL-001` | PASS |
| A failed plugin cannot suppress later plugins | PASS |
| Blocking/non-blocking error policy is tested | PASS |
| Request context does not mutate global state | PASS |
| Existing Quality tests remain green | PASS |
| Existing deterministic compliance verdicts remain unchanged | PASS |
| IfcOpenShell unraisable warning remains absent | PASS |

## Built-in registry order

```text
contract_read
space_tagging
element_confidence
scale_confidence
manual_parameters
opening_placement
```

## Verification commands

```bash
python -m compileall -q \
  api ingest services domain validation reporting manual_inputs

python -m pytest -q tests/validation/quality

python -m pytest -q \
  -W error::pytest.PytestUnraisableExceptionWarning
```

## Scope boundary

The following checks remain intentionally pending for Phase 4:

- complete Room/Space geometry validation;
- catalog-driven required properties;
- unit consistency;
- storey consistency;
- opening endpoint extent;
- semantic Door/Space and Window/Space connectivity;
- vertical opening fit.

Their absence is not represented as Phase 3 completion.

## Recorded results

```text
Phase 3 plugin tests: 18 passed
Full repository suite: 445 passed
Failures: 0
Errors: 0
Skipped: 0
PytestUnraisableExceptionWarning: 0
```

Reference IFC regression comparison:

```text
Quality rows identical to Phase 2: true
Quality status: passed_with_alerts
Quality finding count: 1

Compliance rows identical to Phase 2: true
Compliance finding count: 341
PASS: 15
FAIL: 9
NEEDS_REVIEW: 309
NOT_EVALUATED: 8
```

Machine-readable evidence:

- `docs/test-results/phase3_quality_plugins.xml`
- `docs/test-results/phase3_full.xml`
- `docs/test-results/phase3_regression_comparison.json`
