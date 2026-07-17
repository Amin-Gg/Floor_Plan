# Final Architecture — Mabhas BIM Compliance Engine

## 1. Architectural style

The final system is a **modular monolith**. It deliberately avoids both a
single-file monolith and premature microservices.

Each module owns one responsibility and communicates through typed contracts:

```text
External input
  → boundary adapters
  → canonical BuildingModel
  → validation stages
  → canonical ValidationReport
  → output adapters
```

## 2. Dependency direction

```text
api / CLI
   ↓
services.validation_pipeline
   ↓
┌─────────────────────────────────────────────────────┐
│ ingest │ manual_inputs │ validation │ compliance    │
└─────────────────────────────────────────────────────┘
   ↓
              domain

reporting reads domain/results; it does not execute checks.
standards provides configuration; it does not depend on runtime services.
RAG may explain findings; it cannot alter deterministic verdicts.
```

### Forbidden dependency directions

- `domain` must not import IFC, API, reporting, or RAG modules.
- Quality plugins must not call report renderers.
- Report renderers must not execute validation or compliance rules.
- Deterministic agents must not ask an LLM to decide `PASS` or `FAIL`.
- IFC extraction must not be duplicated inside compliance evaluators.

## 3. Canonical domain model

`domain.model.BuildingModel` is the internal contract. It contains typed:

- Project/Site/Building identifiers;
- Storeys;
- Walls;
- Doors;
- Windows;
- Spaces;
- Stairs and slabs;
- building parameters;
- units, coordinate system, provenance, and extension data.

Every element has a separate `ElementIdentity`:

```text
internal_id  — stable engine identity
ifc_guid     — original IFC GlobalId, when available
source_id    — detector/exporter/external ID
model_name   — source-model display name
```

These values are never overloaded. BCF uses `ifc_guid`; internal joins use
`internal_id`; UI and operator overrides may use any unambiguous identity alias.

## 4. Unified pipeline

`services.validation_pipeline.run_validation_pipeline()` is the only production
orchestrator.

```text
1. Parse source
2. IFC Schema Validation, when source is IFC
3. Build/copy canonical BuildingModel
4. Parse and merge Manual Inputs v1.0
5. Normalize categories and run confidence review
6. Run plugin-based Quality validation
7. Run deterministic Compliance validation
8. Add optional eligible advisory explanations
9. Build coverage
10. Build ValidationReport and output bundle
```

Schema blocking failures stop Quality and Compliance. Precheck mode runs Schema
and Quality only. All source types (`ifc`, `building_model`, `bim_data`) follow
the same stage semantics.

## 5. IFC boundary

IFC is an exchange format, not the domain model.

`validation.schema` performs:

- safe read and parse;
- explicit supported-version policy;
- Project/Site/Building/Storey existence and hierarchy;
- duplicate and missing IFC GlobalIds;
- mandatory-attribute checks from IfcOpenShell metadata;
- structured blocking/non-blocking findings.

A successful IFC parse is shared with ingest through `ParsedIfcSource`, avoiding
reopening the same file.

`ingest.ifc_to_bim_data.ifc_to_building_model()` extracts model semantics into
the canonical domain. Semantic Property Catalog mappings determine which IFC
properties are read.

## 6. Manual Inputs

Manual Inputs v1.0 is a strict, versioned boundary contract.

Merge precedence:

```text
element override
  > trusted model property
  > operator default
  > system fallback
```

Every resolved value records provenance. A system fallback may not silently
produce a compliance verdict when the rule requires an asserted measurement.

## 7. Quality validation

Quality validation is plugin-based. A plugin implements:

```text
code_prefix
name
blocking
applies_to(model, context)
run(model, context) -> list[Finding]
```

The ordered registry runs plugins independently. One plugin failure produces a
visible internal-error finding according to policy and does not silently erase
other checks.

Current plugin groups include:

- contract/property read;
- required properties;
- units;
- storey consistency;
- Space completeness and geometry;
- element confidence and scale confidence;
- Manual Input completeness;
- Door/Window/Wall placement.

Quality findings selectively block dependent capabilities. A malformed Space
does not automatically stop unrelated Door checks.

## 8. Compliance engine

The deterministic agents consume a compatibility mapping produced at one
explicit seam from the canonical model. This seam remains because rewriting all
validated agents would introduce unnecessary verdict risk.

`validation.compliance.runner._run_compliance_core()` is private implementation
code. It receives only the already validated/resolved seam created by
`services.validation_pipeline`. It has no operator-parameter merge API and is
not a supported ingestion entry point.

A value-bearing enriched `bim_data` seam is output-only. Public callers must
provide the original raw mapping plus Manual Inputs v1, or pass a typed
`BuildingModel` for trusted in-process reuse. This prevents validated resolved
values from being confused with unvalidated public input.

Agent families:

- numeric/property checks;
- topology checks;
- opening checks;
- safety checks.

Verdicts:

- `PASS` — deterministic evidence satisfies the rule;
- `FAIL` — deterministic evidence violates the rule;
- `NEEDS_REVIEW` — interpretive/conditional/unsupported automation;
- `NOT_EVALUATED` — required trustworthy model data is unavailable;
- `NOT_APPLICABLE` — rule does not apply.

## 9. Standards configuration

`standards/semantic_property_catalog.yaml` is the single source of truth for:

- IFC entities;
- property aliases and IFC mappings;
- data types and units;
- required properties;
- validation ranges;
- compliance dependencies.

`standards/controlled_values.yaml` defines multilingual canonical vocabularies.
Both files are validated at startup and invalid configuration fails fast.

Request-specific aliases are copied into isolated vocabularies; global mapping
state is never mutated.

## 10. Shared result contracts

All stages use `domain.findings.Finding` and `domain.validation.ValidationResult`.
Finding IDs are deterministic UUID5 values based on the documented semantic
identity basis.

`reporting.report_model.ValidationReport` is the one report source. JSON, HTML,
PDF, and BCF render from this model.

Overall report status is computed centrally and never guessed by each renderer.
A report with blocking missing data cannot be labelled cleanly compliant.

## 11. BCF

The Full BCF 2.1 exporter produces:

- `bcf.version`;
- optional project metadata;
- one `markup.bcf` per eligible finding;
- one `viewpoint.bcfv` with real IFC component selection;
- geometry-derived camera/snapshot when trustworthy;
- a manifest recording exported and skipped findings.

Only findings with a trustworthy IFC `GlobalId` become model-addressable topics.
Global or synthetic/unanchored findings remain available in JSON/HTML/PDF.

## 12. API and jobs

FastAPI accepts either JSON `bim_data` or uploaded IFC. Jobs run through Celery
when configured, otherwise through a development background thread. Both modes
call the same pipeline and use the same report contracts.

The job store abstracts local and Redis-backed storage. Uploads and artifacts
are transferable between API and worker containers when Redis is configured.

## 13. Testing strategy

Tests cover:

- domain identity and round trips;
- schema gates and single parse;
- pipeline stage order and entry-point equivalence;
- Manual Input parsing and precedence;
- Quality plugin isolation and each check family;
- deterministic verdict regression;
- report JSON Schema and deterministic ordering;
- BCF structure, XML, topic identity, and source-GUID selection;
- final one-command acceptance scenario.

The final verification treats `PytestUnraisableExceptionWarning` as an error to
ensure the historical IfcOpenShell destructor warning does not return.
