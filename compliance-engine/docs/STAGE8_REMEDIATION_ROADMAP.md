# Stage 8 Remediation — Execution Roadmap

**Source plan:** `compliance_engine_stage8_remediation_plan.md`
**Codebase:** `compliance-engine-stage8.zip` (verified against actual files, line numbers below are real)
**Rule:** No new compliance rules, no Full BCF work, until all P0 phases (0–4) pass their exit gates.

---

## Verified baseline (what the code actually shows today)

| Plan claim | Verified in code |
|---|---|
| Two+ divergent entry points | `api/pipeline.py:316 run_pipeline`, `api/pipeline.py:362 run_pipeline_from_ifc`, `ingest/ifc_pipeline.py:62 run_ifc_compliance` |
| Global alias mutation | `ingest/category_normalizer.py:93` → `ALIASES.update(...)` mutates module-level dict |
| Split finding contracts | `ingest/schema_validator.py:50 SchemaFinding` vs `services/numeric_checker.py:229 Finding` |
| Monolithic quality file | `services/quality_checker.py` (369 lines, multiple unrelated checks, `QC-PARAM-001` at :344) |
| Failing eval test | `eval/test_verdict_regression.py` (373 passed / 1 failed) |
| Existing catalog | `data/irpset_catalog.yaml` present, but Python mappings duplicate it |

---

## Phase 0 — Baseline & Safety (½–1 day)

**Tickets:** REM-TEST-001 start, ADRs.

| # | Task | Files |
|---|---|---|
| 0.1 | Create branch `remediation/stage8` off the reviewed ZIP state; tag ZIP commit | git |
| 0.2 | Record baseline: `pytest -q tests > docs/STAGE8_BASELINE.md` (219 pass) and `pytest -q` (373/1) + `pip freeze` | `docs/STAGE8_BASELINE.md` |
| 0.3 | Write ADRs | `docs/adr/ADR-001-canonical-building-model.md`, `ADR-002-unified-finding-contract.md`, `ADR-003-unified-pipeline.md` |
| 0.4 | Diagnose `eval/test_verdict_regression.py` — decide: restore advisory note on the enabled LLM path OR update test with explicit product decision. Do not skip/delete. | `eval/test_verdict_regression.py`, `services/orchestrator.py` (advisory path) |
| 0.5 | Document current API request/report shapes for later compatibility adapters | `docs/STAGE8_BASELINE.md` |

**Exit gate:** baseline reproducible; eval failure root-caused and fixed or product-decided; `pytest -q` = 0 failures (REM-TEST-001 closed).

---

## Phase 1 — Domain & Result Contracts (PR 1 + PR 2)

**Tickets:** REM-ARCH-001, REM-ARCH-002, REM-ID-001 foundation. All P0. No dependencies except each other.

### PR 1 — REM-ARCH-001 + REM-ID-001: BuildingModel & identities
| # | Task | Files |
|---|---|---|
| 1.1 | Create `domain/` package: `ElementIdentity` (`internal_id`, `ifc_guid`, `source_id`, `model_name` — four separate fields, never overloaded), `Wall/Door/Window/Space/Storey`, `BuildingModel`, `Point2D/Polygon2D`, unit helpers | NEW `domain/model.py`, `elements.py`, `geometry.py`, `identifiers.py`, `units.py` |
| 1.2 | Bidirectional legacy adapter: `BuildingModel ⇄ bim_data` dict. New pipeline uses BuildingModel internally; existing deterministic agents keep receiving `bim_data` via adapter. Do NOT rewrite agents. | NEW `validation/compliance/adapter.py` |
| 1.3 | IFC ingest populates identities: `ifc_guid` = original `GlobalId`, `internal_id` generated, `OriginalId`/detector IDs → `source_id` only | `ingest/ifc_to_bim_data.py` |
| 1.4 | Round-trip tests: GUID + internal ID survive; `None` stays `None` (never becomes 0); raw + canonical room type both preserved; per-element provenance retained | NEW `tests/domain/test_building_model.py`, `test_legacy_adapter.py`, `test_identity_preservation.py`, `test_units.py` |

### PR 2 — REM-ARCH-002: Shared Finding / ValidationResult
| # | Task | Files |
|---|---|---|
| 1.5 | One `Finding` dataclass + `FindingStage`, `FindingSeverity`, `Verdict` enums (PASS/FAIL/NEEDS_REVIEW/NOT_EVALUATED/NOT_APPLICABLE). Deterministic `finding_id` = UUID5 over `stage+code+model+ifc_guid|internal_id+clause_id` | NEW `domain/findings.py`, `domain/validation.py` |
| 1.6 | `ValidationResult` per stage with fixed status vocab — schema/quality: `passed|passed_with_alerts|failed`; compliance: `completed|completed_with_review|blocked`. Status computed centrally, never re-inferred by reporters | `domain/validation.py`, NEW `reporting/report_model.py` |
| 1.7 | Migrate emitters to shared Finding: `SchemaFinding` (schema_validator.py:50) becomes shared Finding or thin wrapper; quality + numeric_checker Finding (:229) unify | `ingest/schema_validator.py`, `services/quality_checker.py`, `services/numeric_checker.py`, `services/orchestrator.py`, `services/report_generator.py` |
| 1.8 | Contract tests | NEW `tests/domain/test_finding_contract.py`, `tests/reporting/test_finding_serialization.py`, `test_stable_finding_ids.py`, `test_stage_status.py` |

**Exit gate:** all existing verdict tests unchanged; adapter round-trip green; all three stages can emit shared Finding; 219 core tests still pass.

---

## Phase 2 — Unified Pipeline & Manual Inputs (PR 3 + PR 4)

**Tickets:** REM-ARCH-003 (deps: ARCH-001/002), REM-INPUT-001 (dep: ARCH-001), REM-INPUT-002 (dep: INPUT-001), REM-TEST-002. All P0.

### PR 3 — REM-ARCH-003: One orchestrator
| # | Task | Files |
|---|---|---|
| 2.1 | `PipelineRequest` (source_type: ifc/building_model/legacy_bim_data; mode: precheck/full_check) + single `run_validation_pipeline(request) -> ValidationReport` | `services/orchestrator.py` (or NEW `api/pipeline.py` core) |
| 2.2 | Fixed stage order: parse → schema (IFC only) → BuildingModel → **manual merge** → quality → compliance → RAG advisory → report → export. Schema blocking failure stops quality+compliance; report records skipped stages + reason | same |
| 2.3 | Demote existing entry points to deprecated wrappers that build a `PipelineRequest`: `run_pipeline` (:316), `run_pipeline_from_ifc` (:362), `run_ifc_compliance` (ifc_pipeline.py:62). Raw `bim_data` path can no longer bypass Quality. | `api/pipeline.py`, `ingest/ifc_pipeline.py`, `ingest/run_ifc.py`, `api/tasks.py` |
| 2.4 | REM-TEST-002 contract tests: entry-point equivalence, stage order, schema blocking, raw-input-cannot-bypass-quality | NEW `tests/pipeline/test_pipeline_entrypoint_equivalence.py`, `test_stage_order.py`, `test_schema_blocking.py`, `test_raw_input_cannot_bypass_quality.py` |

### PR 4 — REM-INPUT-001/002: Versioned manual inputs
| # | Task | Files |
|---|---|---|
| 2.5 | Versioned wire schema (`schema_version`, `project`, `defaults`, `element_overrides.windows/doors/walls`) with strict validation: reject unknown keys, bool-for-number, non-finite; range checks; cross-field (sill+height ≤ wall height, door height ≤ host wall height); unmatched override IDs rejected unless `allow_unmatched_overrides=true` (then quality alert) | NEW `manual_inputs/models.py`, `parser.py`, `defaults.py` |
| 2.6 | Merge engine with exact precedence `element_override > model_property > operator_default > system_fallback`, producing `ResolvedValue(value, unit, source, confidence)` per value. Unasserted verdict-driving values → dependent rules `NOT_EVALUATED`, never hidden PASS/FAIL | NEW `manual_inputs/merger.py` |
| 2.7 | Merge runs **before** Quality in the orchestrator — kills false `QC-PARAM-001` (quality_checker.py:344) when API supplied the value | `services/orchestrator.py`, `services/quality_checker.py` |
| 2.8 | Old flat dict → `defaults` conversion for one transition release, deprecation warning in report metadata. One parser only. Per-element overrides (windows/doors/walls) are built **from scratch** in the new merger — `apply_window_overrides` exists only as a comment at `ingest/ifc_to_bim_data.py:329`, there is no existing callable to wire through (confirmed by Phase 0 independent audit) | `api/main.py`, `api/pipeline.py`, `ingest/ifc_to_bim_data.py` |
| 2.9 | Tests | NEW `tests/manual_inputs/test_parser.py`, `test_ranges.py`, `test_merge_precedence.py`, `test_per_window_overrides.py`, `test_unmatched_override.py`, `test_backward_compatibility.py`, `test_cross_field_validation.py`; `tests/pipeline/test_manual_inputs_before_quality.py` |

**Exit gate:** both paths run Quality; merge precedes Quality; per-window override test green; no false QC-PARAM-001.

---

## Phase 3 — Quality Plugin Framework (PR 5)

**Ticket:** REM-QC-001 (dep: ARCH-002). P0.

| # | Task | Files |
|---|---|---|
| 3.1 | `QualityCheck` protocol (`code_prefix`, `name`, `blocking`, `applies_to()`, `run() -> list[Finding]`) + explicit ordered registry + `QualityContext` | NEW `validation/quality/protocol.py`, `registry.py`, `checker.py`, `context.py` |
| 3.2 | Migrate every existing check out of `services/quality_checker.py` into `validation/quality/checks/` (contract read, required properties, room/space tagging, element confidence, scale confidence, manual params, storey, units, opening placement). Old file becomes a compatibility wrapper, deleted in Phase 9 | `services/quality_checker.py` → NEW `validation/quality/checks/*.py` |
| 3.3 | Enforce restrictions: no report-generator calls, no global mutation, no compliance execution, no silent exception swallowing — failed plugin emits internal-error Finding per policy | `validation/quality/checker.py` |
| 3.4 | Tests: registry, isolation, error policy, order | NEW `tests/validation/quality/test_registry.py`, `test_checker_isolation.py`, `test_checker_error_policy.py`, `test_check_order.py` |

**Exit gate:** old quality behavior fully covered; adding a dummy check = registration only; one failed plugin can't suppress others.

---

## Phase 4 — Room/Space, Properties, Units, Storey, Placement (PR 6 + PR 7)

**Tickets:** REM-QC-002/003/004 (P0, dep: QC-001; QC-003 also dep CAT-001 — implement against `data/irpset_catalog.yaml` now, re-point in Phase 5), REM-PLACE-001 (P1).

### PR 6 — QC-SPACE / QC-PROP / QC-UNIT / QC-STOREY
| # | Task | Codes |
|---|---|---|
| 4.1 | Room/space plugin: representation exists, stable identity, name+canonical type (raw retained, unknown → alert not silent change), area >0 with declared/derived tolerance compare, boundary exists, boundary closed/valid (≥3 unique vertices, no NaN/inf, non-zero area, self-intersection where lib supports), storey assignment, untagged enclosed regions (only where topology available), door-space connectivity (2 spaces or 1+outside), overlapping spaces | QC-SPACE-001…010 → NEW `validation/quality/checks/room_space_tagging.py`, `domain/geometry.py` |
| 4.2 | Selective blocking: missing area → area-dependent rules `NOT_EVALUATED`; missing boundary → spatial rules `NOT_EVALUATED`; unknown type → type rules `NOT_EVALUATED`/`NEEDS_REVIEW`. Never block all compliance for one bad space | compliance dependency map |
| 4.3 | Catalog-driven required properties + wrong-mapping detection; units (missing unit, mixed units — normalize once at boundary to mm/m², never guess unlabelled); storey (no storey, unknown storey ref, FFL consistency for 1-storey scope) | QC-PROP-001/002, QC-UNIT-001/002, QC-STOREY-001/002/003 → `checks/required_properties.py`, `units.py`, `storey_consistency.py` |
| 4.4 | Tests | `tests/validation/quality/test_space_identity.py`, `test_space_area.py`, `test_space_boundary.py`, `test_untagged_regions.py`, `test_door_space_connectivity.py`, `test_space_overlap.py` |

### PR 7 — REM-PLACE-001: Endpoint-aware placement
| # | Task | Codes |
|---|---|---|
| 4.5 | Normalize insertion convention to canonical `OpeningPlacement(center_offset_mm)`; validate `start_offset ≥ -tol` and `end_offset ≤ wall_length + tol` (catches 900 mm door centered at 3900 on a 4000 wall) | QC-PLACE-007 → `checks/element_placement.py` (extend current `services/opening_agent.py` logic) |
| 4.6 | Missing wall geometry → NOT_EVALUATED-style deficiency (no silent pass); internal-window warning via space connectivity; door semantic connectivity cross-check; vertical fit using per-element resolved values (door h ≤ wall h; sill+window h ≤ wall h) | QC-PLACE-008/009/010/011 |
| 4.7 | Tests | `tests/validation/quality/test_opening_endpoint_extent.py`, `test_opening_insertion_conventions.py`, `test_opening_missing_geometry.py`, `test_window_internal_connection.py`, `test_opening_vertical_fit.py` |

**Exit gate:** malformed rooms fail Quality; endpoint overflow detected; dependent rules NOT_EVALUATED; no new false positives on sample IFC fixture.

---

## Phase 5 — Catalog & Controlled Values (PR 8)

**Tickets:** REM-CAT-001, REM-NORM-001, REM-NORM-002. P1.

| # | Task | Files |
|---|---|---|
| 5.1 | Versioned catalog (`catalog_version`, per-element `ifc_entities`, `properties` with aliases/ifc_mappings/data_type/unit/required_for/min/max) as single source of truth for ingest extraction, QC-PROP checks, units, compliance dependency declarations. Startup validation, fail fast on invalid | evolve `data/irpset_catalog.yaml` → `standards/semantic_property_catalog.yaml`, NEW `standards/loaders.py`, `standards/models.py` |
| 5.2 | Delete duplicate Python domain mappings (keep only generic fallback mechanics) | `ingest/semantic_catalog.py`, `ingest/ifc_to_bim_data.py` |
| 5.3 | `data/controlled_values.yaml`: room_types/boolean/occupancy_types/door_types with Persian+English aliases. Normalizer returns `NormalizedValue(raw, canonical, vocabulary, matched_alias, confidence, source)`; unknowns stay unknown | NEW `standards/controlled_values.yaml` |
| 5.4 | REM-NORM-002 — kill the global mutation at `ingest/category_normalizer.py:93`: replace `ALIASES.update(extra)` with `request_vocabulary = base.with_overrides(extra)`. Request isolation under concurrency | `ingest/category_normalizer.py` |
| 5.5 | Tests | `tests/standards/test_catalog_schema.py`, `test_catalog_startup_failure.py`, `test_catalog_driven_ingest.py`, `test_catalog_driven_quality.py`, `test_catalog_driven_dependency.py`; `tests/ingest/test_normalization_no_global_mutation.py`, `test_normalization_request_isolation.py`, `test_unknown_value_preservation.py`, `test_multilingual_aliases.py`, `tests/standards/test_controlled_values.py` |

**Exit gate:** YAML edit changes ingest + Quality behavior; no YAML/Python duplication; parallel requests isolated.

---

## Phase 6 — Schema Validator Completion (PR 9)

**Tickets:** REM-SCHEMA-001 (dep: ARCH-002), REM-SCHEMA-002 (dep: ID-001). P1.

| # | Task | Files |
|---|---|---|
| 6.1 | IFC-SCHEMA-008 spatial hierarchy: validate Project→Site→Building→Storey via `IfcRelAggregates` / `IfcRelContainedInSpatialStructure` — entity existence alone no longer passes | `ingest/schema_validator.py` → `validation/schema/checks.py` |
| 6.2 | Duplicate `GlobalId` detection = fail; mandatory attribute policy via ifcopenshell schema metadata (no hand-rolled buildingSMART validator) | same |
| 6.3 | `SchemaValidationPolicy(supported_versions={"IFC4","IFC4X1","IFC4X3"}, allow_ifc2x3=False, strict_mandatory_attributes=True)` — IFC2X3 only if explicitly documented and downstream-tested | same |
| 6.4 | Precheck (schema+quality) vs full_check modes; mode never weakens blocking checks; IFC parsed exactly once per run | `services/orchestrator.py` |
| 6.5 | Tests | `tests/validation/schema/test_spatial_hierarchy.py`, `test_duplicate_guids.py`, `test_supported_versions.py`, `test_mandatory_attributes.py`, `test_single_parse.py`; `tests/ingest/test_ifc_guid_preservation.py`, `test_duplicate_ifc_guid.py` |

**Exit gate:** disconnected hierarchy no longer silent-passes; schema uses shared Finding; single parse.

---

## Phase 7 — Report v1.0 (PR 10)

**Tickets:** REM-REPORT-001, REM-REPORT-002. P1 (deps: ARCH-002).

| # | Task | Files |
|---|---|---|
| 7.1 | Unified report model: `report_schema_version`, `engine_version`, `run_id`, `generated_at`, `mode`, `model` (name/source/ifc_schema/project_guid/fingerprint), `overall` (central status policy), `stages`, `summary`, flat `findings[]` in shared shape. Deterministic ordering for snapshots. No secrets/absolute paths | `reporting/report_model.py`, refactor `services/report_generator.py` → `reporting/json_report.py`, `html_report.py`, `pdf_report.py` |
| 7.2 | Semantics: schema-invalid ≠ compliant; blocking missing data → no clean compliant label; skipped checks stated; checker/catalog versions included | `reporting/report_model.py` |
| 7.3 | Publish JSON Schema; validate every generated report in tests. Legacy report adapter for one transition release only | NEW `reporting/schemas/validation_report_v1.schema.json` |
| 7.4 | HTML/PDF render from report model, not ad hoc stage dicts | `reporting/html_report.py`, `pdf_report.py` |
| 7.5 | Tests | `tests/reporting/test_report_json_schema.py`, `test_report_determinism.py`, `test_overall_status_policy.py`, `test_skipped_stage_reason.py`, `test_no_sensitive_paths.py`, `test_json_element_identity.py` |

**Exit gate:** JSON validates against schema; stable IDs; every element finding carries internal ID + IFC GUID.

---

## Phase 8 — Full BCF (PR 11)

**Tickets:** REM-BCF-001, REM-BCF-002. P2. **Hard gate: do not start before Finding contract, GUID preservation, and Report v1.0 are stable.**

| # | Task | Files |
|---|---|---|
| 8.1 | Archive: `bcf.version`, optional `project.bcfp`, per-topic `markup.bcf` + `viewpoint.bcfv` (+ optional snapshot). Topic fields per plan §20 (stable GUID, title, dates, author, type/status/priority, labels, stage, code, verdict, model, IFC GUID, clause, expected/actual) | NEW `reporting/bcf_exporter.py` (replace current minimal exporter) |
| 8.2 | Viewpoint component selection with **real IFC GlobalId** (`<Component IfcGuid="..."/>`) — never internal/detector IDs. Camera only from trustworthy geometry; otherwise selection-only viewpoint + documented limitation. Never invent coordinates | same |
| 8.3 | Eligibility: element-anchored schema/quality issues, compliance FAIL, NEEDS_REVIEW, correctable NOT_EVALUATED. No unanchored/empty topics; global findings stay in JSON/HTML | same |
| 8.4 | Manual verification in a real BCF viewer; record viewer name/version, import result, topic count, selection result, limitations | NEW `docs/BCF_INTEROPERABILITY_TEST.md` |
| 8.5 | Tests | `tests/reporting/test_bcf_archive_structure.py`, `test_bcf_stable_topic_guid.py`, `test_bcf_component_selection.py`, `test_bcf_topic_count.py`, `test_bcf_xml_validation.py` |

**Exit gate:** validates as BCF 2.1 (or documented version); real viewer imports all topics; selection works for Door/Window/Wall/Space; topic count matches eligible findings.

---

## Phase 9 — Regression & Cleanup (PR 12)

| # | Task |
|---|---|
| 9.1 | Remove deprecated wrappers (`run_pipeline_from_ifc`, `run_ifc_compliance`, flat-input parser, legacy report adapter, old `services/quality_checker.py`) only after all callers migrate |
| 9.2 | Remove duplicated models/mappings and dead compatibility code |
| 9.3 | Update README (layered pipeline), API manual-input schema doc, report schema doc, catalog extension procedure, migration notes |
| 9.4 | Build final acceptance fixture per plan §40 (derived from the real sample IFC with injected defects: malformed room, window beyond host wall endpoints, synthetic second window with distinct manual dims, missing semantic data, real FAIL, real NOT_EVALUATED) runnable via one command: `python -m scripts.run_validation_acceptance --ifc tests/fixtures/sample_plan.ifc --manual-inputs tests/fixtures/remediation_manual_inputs.json --output-dir artifacts/remediation_acceptance` |
| 9.5 | Compare pre/post-remediation verdicts. Verdict changes allowed only if: previous verdict demonstrably wrong + documented + regression test + changelog names clauses/element types |
| 9.6 | Assemble delivery package (plan §39): code, tests, requirements, `CHANGELOG_STAGE8_REMEDIATION.md`, `ARCHITECTURE.md`, `MANUAL_INPUTS_SCHEMA.md`, `VALIDATION_REPORT_SCHEMA.md`, `BCF_INTEROPERABILITY_TEST.md`, sample IFC/inputs/outputs, full pytest output, file change list |

---

## Ticket dependency graph

```text
REM-TEST-001 ─────────────────────────────── (Phase 0, no deps)
REM-NORM-001 ─────────────────────────────── (no deps, can start anytime)

REM-ARCH-001 ──┬── REM-ARCH-002 ──┬── REM-ARCH-003 ── REM-TEST-002
               │                  ├── REM-QC-001 ──┬── REM-QC-002
               │                  │                ├── REM-QC-003 (also ← REM-CAT-001)
               │                  │                └── REM-QC-004
               │                  ├── REM-SCHEMA-001
               │                  └── REM-REPORT-001 ── REM-REPORT-002
               ├── REM-ID-001 ──┬── REM-SCHEMA-002
               │                └── REM-BCF-001 (also ← REM-REPORT-001) ── REM-BCF-002
               ├── REM-INPUT-001 ──┬── REM-INPUT-002
               │                   └── REM-PLACE-001 (also ← REM-ARCH-001)
               └── REM-CAT-001
REM-NORM-001 ── REM-NORM-002
```

**Parallelizable:** REM-NORM-001 (controlled values YAML) and Phase 0 docs can run alongside Phase 1. Everything else follows the graph.

---

## Verification commands (run per phase, from project root)

```bash
python -m compileall api ingest services domain validation reporting manual_inputs standards
pytest -q tests
pytest -q
pytest -q tests/pipeline
pytest -q tests/validation
pytest -q tests/reporting
```

Add `mypy domain manual_inputs validation reporting` / `ruff check .` to gates only if actually configured.

---

## Hard rules (from plan Parts 0/XII — enforce in every PR review)

1. Modular monolith. No microservices. No per-rule modules (no `door_module`, no `rule_5_4_7_4_module`).
2. IFC is an adapter format — validators consume `BuildingModel`, never raw IFC or raw dicts.
3. RAG/LLM never creates, changes, or overwrites PASS/FAIL, and never infers missing geometry.
4. Missing/untrusted data → `NOT_EVALUATED`. Never PASS-by-absence. No provenance-free defaults driving verdicts.
5. One pipeline contract — every entry point delegates to the orchestrator.
6. `ifc_guid` ≠ `internal_id` ≠ `source_id`. Never overload. BCF selection uses IFC GUID only.
7. No new logic in the old quality monolith; no new hard-coded aliases in Python; reporters serialize, never check.
8. No single massive refactor — compatibility adapters + phase gates. No deleting failing tests.
9. PR rules: one ticket group, tests included, migration notes on contract changes, state verdict/JSON changes, before/after samples for reporting changes, core suite green before review.

---

## Done = plan §36 checklist

Architecture: BuildingModel is the contract / shared Finding+ValidationResult everywhere / one orchestrator / all entry points delegate / quality = plugins / IFC GUID preserved independently.
Behavior: schema blocks / manual merge before Quality / missing data → NOT_EVALUATED / RAG can't touch verdicts / per-window dims / invalid rooms detected / endpoint overflow detected.
Reporting: JSON validates vs v1 schema / stable finding IDs / dual element identity / BCF GUID selection / BCF viewer-tested.
Testing: core + full suite green / contract tests green / sample IFC E2E green / no unexplained verdict regression.
Docs: README, manual-input schema, report schema, catalog procedure, BCF interop record, migration notes.

**Success criterion:** the repo is easier to extend after remediation than before it.
