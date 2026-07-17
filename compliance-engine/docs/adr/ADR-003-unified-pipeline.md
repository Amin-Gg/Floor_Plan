# ADR-003 — Single Pipeline Orchestrator for All Entry Points

**Status:** Accepted (Phase 0, 2026-07-10) · **Implements:** REM-ARCH-003 · **Phase:** 2 (PR 3)

## Context

Two behavioral paths exist. The IFC path
(`api/pipeline.py:362 run_pipeline_from_ifc` → `ingest/ifc_pipeline.py:62
run_ifc_compliance`) runs schema → ingest → prepass → quality → compliance →
report. The raw path (`api/pipeline.py:316 run_pipeline`) runs compliance →
report only — Quality validation can be bypassed entirely depending on the
entry point. Manual inputs are merged at inconsistent points, so
`QC-PARAM-001` can fire even when the API request supplied the value.

## Decision

1. One request object `PipelineRequest(source_type: ifc | building_model |
   legacy_bim_data, …, manual_inputs, clauses, metadata, mode: precheck |
   full_check)` and one public function
   `run_validation_pipeline(request) -> ValidationReport`.
2. Fixed stage sequence for every entry point:
   parse → IFC schema validation (IFC sources only) → BuildingModel →
   **manual input parse + merge** → quality → compliance → RAG/LLM advisory →
   ValidationReport → export.
3. Blocking semantics: a blocking schema failure stops quality and
   compliance; the report records every skipped stage and the reason.
   `precheck` mode runs schema + quality only and never weakens blocking
   checks.
4. Manual inputs merge **before** Quality — eliminates false `QC-PARAM-001`
   (`services/quality_checker.py:344`).
5. RAG/LLM remains advisory-only at the orchestration level: it runs after
   deterministic compliance and can only append notes to interpretive
   NEEDS_REVIEW findings. It cannot create, change, or overwrite PASS/FAIL.
6. Existing entry points (`run_pipeline`, `run_pipeline_from_ifc`,
   `run_ifc_compliance`) become deprecated wrappers that build a
   `PipelineRequest` and delegate. Covered by entry-point-equivalence
   contract tests (REM-TEST-002); removed in Phase 9 after callers migrate.

## Consequences

- Raw `bim_data` input can no longer bypass Quality — a behavior change for
  the `/analyze` path, surfaced as new quality findings, never as changed
  deterministic verdicts. Documented in the transition-release notes.
- The IFC file is parsed exactly once per run: schema validation and ingest
  share the same parsed IFC object through an explicit parse context — a
  `ParsedSource` object created by the orchestrator and passed to both
  stages as an argument. Caching by file path in hidden module-global state
  is explicitly disallowed (it breaks request isolation and testability).
- Equivalent input through different entry points must produce equivalent
  quality findings, verdicts, and report status — enforced by tests, not
  convention.

## Rejected alternatives

- **Patch Quality into `run_pipeline` without a shared orchestrator:** leaves
  stage order duplicated in three places; the next stage added would diverge
  again.
- **Break the old entry points immediately:** violates the compatibility
  guardrail; API consumers need one transition release.
