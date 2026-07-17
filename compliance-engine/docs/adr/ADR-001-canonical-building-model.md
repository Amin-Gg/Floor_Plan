# ADR-001 — Canonical BuildingModel as the Internal Contract

**Status:** Accepted (Phase 0, 2026-07-10) · **Implements:** REM-ARCH-001, REM-ID-001 · **Phase:** 1 (PR 1)

## Context

Modules currently exchange mutable `bim_data` dictionaries with loosely agreed
keys. Hidden coupling exists between `ingest/ifc_to_bim_data.py`,
`services/quality_checker.py`, `services/numeric_checker.py`,
`validation/compliance/runner.py`, `services/report_generator.py`, and
`api/pipeline.py`. Element identity is ambiguous: IFC `GlobalId`, detector
`OriginalId`, and engine IDs are not consistently separated, which blocks
reliable BCF component selection and stable finding IDs.

## Decision

1. Introduce a typed `domain/` package with dataclasses:
   `ElementIdentity(internal_id, ifc_guid, source_id, model_name)`,
   `Wall`, `Door`, `Window`, `Space`, `Storey`, `BuildingModel`,
   `Point2D`/`Polygon2D`, and explicit unit helpers. Pydantic only at
   external API boundaries.
2. Identity fields are never overloaded: `ifc_guid` holds the original IFC
   `GlobalId` only; `internal_id` is engine-generated and stable;
   `source_id` holds detector/`OriginalId`/external IDs. BCF selection uses
   `ifc_guid` exclusively.
3. `internal_id` generation is a defined deterministic algorithm, not a
   convention. UUID5 under a fixed project namespace
   (`SIMSYS_ID_NAMESPACE`, a constant UUID checked into `domain/identifiers.py`):

   ```text
   IFC source:      internal_id = uuid5(NS, model_fingerprint + ":" + ifc_guid)
   non-IFC source:  internal_id = uuid5(NS, model_fingerprint + ":" + source_type + ":" + source_id)
   fallback:        internal_id = uuid5(NS, model_fingerprint + ":" + element_type + ":" + stable_geometry_key)
   ```

   `model_fingerprint` = SHA-256 of the source file bytes (IFC) or of the
   canonical-JSON serialization of the input (non-IFC). The fallback branch
   (no `ifc_guid`, no `source_id`) uses a rounded geometric key
   (element type + storey + quantized insertion/centroid coordinates); its
   limitation — two geometrically identical unidentified elements collide —
   is documented, and the ingest layer must emit a Quality alert when the
   fallback is used, since such elements also cannot be targeted by manual
   overrides or BCF. Same input ⇒ same `internal_id` across runs, by test.
4. IFC remains an adapter format. Validators and rule evaluators consume
   `BuildingModel`, never raw IFC entities or raw dictionaries.
5. Migration via bidirectional adapters (`BuildingModel ⇄ legacy bim_data`,
   `validation/compliance/adapter.py`). Existing deterministic agents keep
   receiving `bim_data` through the adapter; they are not rewritten in PR 1.
6. Semantic invariants enforced by round-trip tests: `None` stays `None`
   (missing ≠ 0), raw + canonical room types both preserved, per-element
   provenance retained, both IDs survive conversion.

## Consequences

- New validators get a documented, typed contract; undocumented dict-key
  access becomes a review-rejectable violation.
- Temporary duplication (model + adapters) until Phase 9 cleanup.
- Existing 219 core tests must pass unchanged through the adapter — this is
  the PR 1 exit gate.

## Rejected alternatives

- **Keep dicts, add TypedDicts:** no runtime identity guarantees, no
  invariant enforcement, doesn't fix ID overloading.
- **Rewrite agents to consume the model directly in one pass:** violates the
  no-massive-refactor guardrail; risks silent verdict changes.
- **Pydantic everywhere:** validation overhead in hot paths; dataclasses
  suffice internally.
