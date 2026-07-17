# Final R2 Delivery

This directory contains release evidence generated from the clean Git tag
`stage8-phase9-final-r2`.

## Contents

- `git/compliance-engine-final-r2.bundle` — complete transferable Git history.
- `test-results/final_r2_clean_full.xml` — clean-clone full-suite JUnit result.
- `acceptance/` — final acceptance JSON, HTML, PDF, BCF and execution evidence.
- `regression/FINAL_R2_VERIFICATION.json` — verdict parity, entry-point
  equivalence and source-IFC BCF GUID verification.
- `wheel/mabhas_compliance_engine-1.0.1-py3-none-any.whl` — release wheel.
- `GIT_EVIDENCE.txt` — commit/tag/bundle evidence.
- `FILE_MANIFEST.txt` — file list with byte sizes.
- `SHA256SUMS.txt` — SHA-256 checksums for the delivered tree.

## Verified release gates

```text
Full suite: 548 passed / 0 failed / 0 errors / 0 skipped
PytestUnraisableExceptionWarning: 0
Reference verdicts: PASS 15 / FAIL 9 / NEEDS_REVIEW 309 / NOT_EVALUATED 8
IFC vs raw bim_data signatures: identical
Acceptance: ok=true; JSON/HTML/PDF/BCF generated and non-empty
BCF: every exported component GUID exists in the source IFC
```

The acceptance JSON evidence uses relative delivery paths so the package is
transferable; the actual outputs were generated in a clean clone before being
copied here.
