# Final R2 Test Summary

- Source: clean clone of `compliance-engine-final-r2.bundle`
- Checkout: `stage8-phase9-final-r2`
- Command:

```bash
pytest -q -W error::pytest.PytestUnraisableExceptionWarning \
  --junitxml=final_r2_clean_full.xml
```

Result:

```text
548 tests
548 passed
0 failures
0 errors
0 skipped
0 PytestUnraisableExceptionWarning
```

Acceptance command:

```bash
python -m scripts.run_validation_acceptance \
  --ifc tests/fixtures/sample_plan.ifc \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --output-dir artifacts/remediation_acceptance
```

Acceptance result: `ok: true` with JSON, HTML, PDF, BCF and BCF manifest.
