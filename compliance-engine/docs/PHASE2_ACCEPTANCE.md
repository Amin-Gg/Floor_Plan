# Stage 8 Remediation — Phase 2 Acceptance Record

## Scope

- REM-ARCH-003 — unified pipeline orchestrator;
- REM-TEST-002 — pipeline contract tests;
- REM-INPUT-001 — Manual Inputs Schema v1.0;
- REM-INPUT-002 — per-element overrides;
- IfcOpenShell malformed-file warning correction.

## Exit-gate results

| Requirement | Result |
|---|---|
| One `PipelineRequest` and one authoritative orchestrator | PASS |
| IFC, BuildingModel and legacy-data sources supported | PASS |
| Raw input can no longer bypass Quality | PASS |
| Schema failures block downstream stages | PASS |
| Skipped stages and reasons recorded | PASS |
| Manual merge runs before Quality | PASS |
| `precheck` and `full_check` modes | PASS |
| Strict versioned manual-input schema | PASS |
| Per-window, per-door and per-wall overrides | PASS |
| Deterministic merge precedence | PASS |
| Resolved-value provenance | PASS |
| Unmatched override reject/alert policy | PASS |
| Legacy flat compatibility and deprecation metadata | PASS |
| FastAPI, thread and Celery wiring | PASS |
| Existing deterministic verdicts unchanged without new inputs | PASS |
| IfcOpenShell unraisable warnings removed at source | PASS |
| Full suite green with unraisable warnings treated as errors | PASS |

## Final automated test result

```text
427 passed
0 failed
0 errors
0 skipped
```

Command:

```bash
pytest -q -W error::pytest.PytestUnraisableExceptionWarning \
  --junitxml=docs/test-results/phase2_full.xml
```

## Deterministic regression result

Full clause corpus against `tests/fixtures/sample_plan.ifc`, with no new manual
inputs:

```text
Phase 1:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Phase 2:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

341 compared finding rows: identical
```

See `docs/test-results/phase2_verdict_comparison.md`.

## Real IFC acceptance run

The unified IFC path generated:

```text
artifacts/phase2_acceptance/compliance_result.json
artifacts/phase2_acceptance/compliance_report.html
artifacts/phase2_acceptance/compliance_report.pdf
artifacts/phase2_acceptance/compliance_issues.bcf
```

Recorded stage trace:

```text
parse_source
schema
building_model
manual_merge
quality
compliance
reporting
```

This run deliberately supplied `wall_height_mm=3200`, demonstrating that a new
manual value reaches the deterministic engine after Quality validation.

## Verification commands

```bash
python -m compileall -q api ingest services domain validation reporting manual_inputs
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
python -m pip install -e . --no-deps
```

## Phase boundary

Phase 2 does not move individual Quality checks into plugins. That is Phase 3.
No Full BCF interoperability work was started.
