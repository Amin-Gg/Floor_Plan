# Stage 8 Remediation — Phase 8 Acceptance

## Automated exit gate

Run:

```bash
python -m pytest -q -W error::pytest.PytestUnraisableExceptionWarning
python -m scripts.run_phase8_acceptance
python -m scripts.validate_bcf artifacts/phase8_acceptance/compliance_issues.bcf
```

Verified results:

```text
Core suite: 377 passed
Full suite: 533 passed
Failures: 0
Errors: 0
Skipped: 0
IfcOpenShell unraisable warnings: 0
```

Required results:

- all tests pass;
- zero `PytestUnraisableExceptionWarning`;
- source IFC schema passes;
- BCF archive validates as the supported BCF XML 2.1 subset;
- each viewpoint uses a real IFC `GlobalId`;
- Phase 7 compliance verdict payloads remain unchanged;
- JSON/HTML/PDF remain Phase 7 report-model outputs.

## Acceptance findings

The reference execution produces 341 compliance findings:

```text
PASS: 15
FAIL: 9
NEEDS_REVIEW: 309
NOT_EVALUATED: 8
```

The BCF archive exports 14 actionable IFC-anchored topics. Global findings are
not converted to empty topics and remain in the other report formats.

## Completion status

- Export implementation: complete.
- Automated archive validation: complete.
- Automated component-selection verification: complete.
- Regression verification: complete.
- Independent desktop viewer verification: pending external execution and
  explicitly documented in `docs/BCF_INTEROPERABILITY_TEST.md`.
