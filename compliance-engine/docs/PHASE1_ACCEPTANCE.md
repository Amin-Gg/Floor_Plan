# Stage 8 Remediation — Phase 1 Acceptance Record

## Scope

- REM-ARCH-001 — canonical `BuildingModel`
- REM-ID-001 foundation — separate deterministic identities
- REM-ARCH-002 — shared `Finding` / `ValidationResult`

## Exit-gate results

| Requirement | Result |
|---|---|
| Typed domain package exists | PASS |
| `internal_id`, `ifc_guid`, `source_id`, `model_name` are separate | PASS |
| Deterministic UUID5 internal IDs | PASS |
| `BuildingModel ⇄ bim_data` adapter | PASS |
| Missing values remain `None` | PASS |
| Raw and canonical room type preserved | PASS |
| Provenance retained | PASS |
| IFC ingest preserves original `GlobalId` | PASS |
| Schema, Quality, Compliance use shared `Finding` | PASS |
| Central `ValidationResult` status policy | PASS |
| Existing deterministic verdicts unchanged | PASS |
| Full test suite green | PASS — 395 passed |

## Independent behavior comparison

Full clause corpus against `tests/fixtures/sample_plan.ifc`:

```text
Before Phase 1:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

After Phase 1:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Compared finding rows:
341
Result:
identical
```

Comparison fields:

```text
article_id, verdict, element_id, measured, required
```

## Verification commands

```bash
python -m compileall -q api ingest services domain validation reporting
pytest -q
python -m pip install -e . --no-deps
```

## Final automated test result

```text
395 passed
0 failed
0 errors
0 skipped
```

JUnit artifact:

```text
docs/test-results/phase1_full.xml
```

## Known non-blocking warning

Two existing `PytestUnraisableExceptionWarning` messages from
`ifcopenshell.file.__del__` remain in corrupt-IFC tests. They were present before
Phase 1 and do not affect the exit gate.

## Phase boundary

Phase 1 does not implement the unified public orchestrator or the versioned
manual-input schema. Those remain Phase 2 tasks.
