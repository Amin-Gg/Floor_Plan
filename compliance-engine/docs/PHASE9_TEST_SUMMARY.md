# Phase 9 Test Summary

## Full repository suite

Command:

```bash
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Result:

```text
Tests:    530
Passed:   530
Failures: 0
Errors:   0
Skipped:  0
```

JUnit evidence:

```text
docs/test-results/phase9_full.xml
```

## Static compilation

```bash
python -m compileall api ingest services domain validation reporting manual_inputs standards
```

Result: passed.

## Wheel packaging smoke test

A wheel was built with `pip wheel --no-deps`. The wheel contains:

- `standards/semantic_property_catalog.yaml`;
- `standards/controlled_values.yaml`;
- `reporting/schemas/validation_report_v1.schema.json`;
- BCF 2.1 XSD subset files.

## Acceptance and regression

Acceptance evidence:

```text
artifacts/remediation_acceptance/
```

Reference regression evidence:

```text
artifacts/phase9_regression/verdict_comparison.json
```
