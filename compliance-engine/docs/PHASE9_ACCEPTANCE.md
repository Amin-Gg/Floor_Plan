# Phase 9 Final Acceptance

## Automated gates

Run from repository root:

```bash
python -m compileall api ingest services domain validation reporting manual_inputs standards
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Required result:

```text
0 failures
0 errors
0 IfcOpenShell unraisable warnings
```

## One-command end-to-end acceptance

```bash
python -m scripts.run_validation_acceptance \
  --ifc tests/fixtures/sample_plan.ifc \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --clauses data/mabhas_clauses.json \
  --output-dir artifacts/remediation_acceptance
```

Required evidence:

- Schema stage is `passed`;
- Quality includes `QC-SPACE-004`;
- Quality includes `QC-SPACE-006`;
- Quality includes `QC-PLACE-007`;
- `Wb.width_mm == 1400`;
- `W-ACCEPT-02.width_mm == 900`;
- Compliance includes at least one `FAIL`;
- Compliance includes at least one `NOT_EVALUATED`;
- JSON/HTML/PDF/BCF outputs exist;
- BCF archive validates;
- selected IFC GUIDs exist in the source IFC.

## Regression gate

The unchanged sample IFC and complete corpus must match Phase 8 on all 341
behavioral compliance rows using:

```text
article_id
verdict
message
object
measured
required
unit
element_id
unsupported
```

Expected summary:

```text
PASS             15
FAIL              9
NEEDS_REVIEW    309
NOT_EVALUATED     8
```

## Manual BCF GUI verification

Automated BCF 2.1 and source-GUID validation is complete. GUI verification in a
desktop BCF viewer remains an external manual step and is documented in:

```text
docs/BCF_INTEROPERABILITY_TEST.md
```

## Observed final result

Automated test suite:

```text
530 passed
0 failed
0 errors
0 skipped
0 PytestUnraisableExceptionWarning
```

Final acceptance fixture:

```text
Schema status:      passed
Quality status:     failed (expected deliberate fixture defects)
PASS:               19
FAIL:               12
NEEDS_REVIEW:       309
NOT_EVALUATED:      5
BCF topics:         19
BCF viewpoints:     18
```

The deliberate Quality failures include the missing Space area, invalid/open
Space boundary, and opening endpoint overflow required by the acceptance plan.

The unchanged reference-model regression remains:

```text
341/341 behavioral rows identical to Phase 8
PASS: 15
FAIL: 9
NEEDS_REVIEW: 309
NOT_EVALUATED: 8
```
