# ADR-002 — One Shared Finding and ValidationResult Contract

**Status:** Accepted (Phase 0, 2026-07-10) · **Implements:** REM-ARCH-002 · **Phase:** 1 (PR 2)

## Context

Three incompatible result shapes exist: `SchemaFinding`
(`ingest/schema_validator.py:50`), the compliance `Finding`
(`services/numeric_checker.py:229`, reused by quality), and ad hoc stage
dictionaries assembled in the report generator. Severity/status is re-inferred
per reporter. Finding IDs are not stable, blocking BCF topic stability and
report snapshot testing.

## Decision

1. One shared `Finding` dataclass in `domain/findings.py` with enums
   `FindingStage{SCHEMA,QUALITY,COMPLIANCE}`,
   `FindingSeverity{FAIL,ALERT,INFO}`,
   `Verdict{PASS,FAIL,NEEDS_REVIEW,NOT_EVALUATED,NOT_APPLICABLE}`, dual
   element identity (`element_internal_id` + `element_ifc_guid`), clause
   linkage, expected/actual/unit, and free-form `details`.
2. The existing `unsupported` distinction is preserved on the shared contract
   (engine-limitation NEEDS_REVIEW vs interpretive NEEDS_REVIEW). Stage 7
   behavior — LLM advisory notes target interpretive findings only — is a
   ratified product decision (see STAGE8_BASELINE.md §3) and depends on this
   field.
3. `finding_id` is deterministic and fully specified:

   ```text
   finding_id = uuid5(SIMSYS_FINDING_NAMESPACE, canonical_basis)
   canonical_basis = "v1" + US + stage + US + code + US + model_fingerprint
                     + US + element_key + US + (clause_id or "")
                     [+ US + ordinal            # only when ordinal > 0]
   ```

   - **Ordinal disambiguator (amendment, Phase 9 review).** Stage-8
     deterministic agents may legitimately emit more than one finding with
     the same semantic basis (same stage, code, element, and clause).
     `assign_finding_ordinals()` assigns a deterministic per-run ordinal to
     such duplicates; a non-zero ordinal is appended to the basis as an
     additional `US`-separated component. Findings that are unique on their
     semantic basis carry ordinal 0 and omit the component, so their IDs are
     identical to the pre-amendment specification. This amendment documents
     behavior implemented since Phase 1; it is not an ID-format change and
     does not bump the `"v1"` token.
   - `US` is the ASCII unit separator `\x1f` — it cannot occur in any
     component, so no escaping scheme is needed; components are used
     verbatim. Empty/absent components serialize as the empty string, never
     as a literal `"None"`.
   - **Model identity** = `model_fingerprint` exactly as defined in ADR-001
     (SHA-256 of source bytes / canonical JSON) — not the mutable display
     name.
   - **Element key** = `ifc_guid` when present, else `internal_id` (which is
     itself deterministic per ADR-001, including its documented fallback), so
     a missing `ifc_guid` never makes the finding ID unstable. Non-element
     (model-level) findings use the empty element key.
   - `SIMSYS_FINDING_NAMESPACE` is a constant UUID checked into
     `domain/findings.py`. The leading `"v1"` version token makes any future
     change to the basis (fields, ordering, namespace) an explicit versioned
     event: the version token is bumped, the change is recorded in the
     report's `report_schema_version` notes, and a migration entry is added
     to the changelog. UUID5/SHA-1 collision risk at this scale is
     negligible; determinism, not secrecy, is the requirement.
4. Shared `ValidationResult(stage, status, findings, started_at,
   completed_at, checker_version, metadata)` with a fixed status vocabulary —
   schema/quality: `passed | passed_with_alerts | failed`; compliance:
   `completed | completed_with_review | blocked`. Status is computed once,
   centrally; reporters serialize, never infer.
5. Migration: schema, quality, and compliance emitters move to the shared
   contract behind thin wrappers; the report generator serializes both old
   and new shapes during the transition (removed in Phase 9).

## Consequences

- All three stages serialize to one JSON shape — prerequisite for Report v1.0
  (REM-REPORT-001) and BCF topic stability (REM-BCF-001).
- `NOT_EVALUATED` stays a first-class verdict, distinct from quality alerts.
- Determinism becomes testable: identical input ⇒ identical finding IDs.

## Rejected alternatives

- **Field-level convergence of the two existing classes:** leaves status
  inference scattered and IDs unstable.
- **Verdict-only model without severity:** schema/quality stages need
  ALERT/INFO semantics that verdicts don't express.
