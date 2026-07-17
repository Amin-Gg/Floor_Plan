# Stage 8 Baseline Sanitization Record

The Git baseline tagged `stage8-zip-baseline` was derived from the reviewed
`compliance-engine-stage8.zip` archive.

Intentional exclusion:

- `.env` was not committed because it is local configuration and may contain
  credentials or secrets.

Repository-hygiene normalization:

- the comprehensive root `.gitignore` from the reviewed archive is restored on
  the remediation branch before Phase 1 work;
- runtime/source files other than the intentionally excluded `.env` match the
  reviewed archive baseline.

The tag should therefore be described as a **sanitized source baseline**, not a
byte-for-byte pristine copy of the original ZIP.
