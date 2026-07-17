# Stage 8 Remediation — Phase 1 Implementation

**Scope:** REM-ARCH-001, REM-ARCH-002, REM-ID-001 foundation  
**Branch:** `remediation/stage8`  
**Architecture:** canonical domain model + compatibility adapters + one shared
finding/result contract.

## Implemented

### Canonical domain model

Added typed internal contracts under `domain/`:

- `ElementIdentity` with independent `internal_id`, `ifc_guid`, `source_id`, and
  `model_name` fields;
- deterministic UUID5 internal IDs using the ADR-001 algorithm;
- file and canonical-JSON model fingerprints;
- `Point2D`, `Polygon2D`, `Storey`, `Wall`, `Door`, `Window`, `Space`,
  `SimpleElement`, and `BuildingModel`;
- explicit unit conversion helpers that return `None` for unknown units instead
  of guessing;
- typed building-parameter and model-provenance containers.

### Legacy compatibility adapter

Added `validation/compliance/adapter.py` with:

- `building_model_from_legacy()`;
- `building_model_to_legacy()`;
- identity indexing and finding enrichment helpers;
- preservation of `None`, raw/canonical room types, provenance, extras, and dual
  element identity;
- compatibility output for the existing deterministic agents.

The IFC ingest path now reads into a canonical `BuildingModel` and adapts back
at the existing agent seam. The schema gate's parsed IFC object is reused, so
schema validation and ingest do not reopen the file.

### Shared Finding contract

Added `domain/findings.py`:

- one `Finding` used by schema, quality, and compliance;
- `FindingStage`, `FindingSeverity`, and the extended `Verdict` enum;
- deterministic UUID5 finding IDs based on stage, code, model fingerprint,
  element key, and clause ID;
- separate internal and IFC element IDs;
- backward-compatible constructor fields and serialization keys required by
  the Stage 8 agents and reports.

`services.numeric_checker.Finding` and `Verdict` are now compatibility
re-exports of the domain contract. `SchemaFinding` is a thin compatibility
wrapper over the same class.

### Shared ValidationResult contract

Added `domain/validation.py`:

- centralized schema/quality/compliance status vocabulary;
- centralized status computation;
- timestamps, checker version, and metadata;
- report-facing `StageReport` wrapper.

Quality and schema now serialize through `ValidationResult`; compliance exposes
`ComplianceResult.as_validation_result()` while preserving the existing
Stage-8 payload.

### Identity propagation

IFC readers now preserve:

```text
id                 legacy source-facing identifier
internal_id        deterministic engine identifier
ifc_guid           original IFC GlobalId
source_id          detector / OriginalId / external identifier
model_name         display/source file name
```

Compliance and quality findings are enriched with the canonical identities and
model fingerprint before stable IDs are assigned.

## Compatibility guarantees

- Existing deterministic agents still consume `bim_data` dictionaries.
- Existing API/report keys remain present.
- Existing four-key compliance summary remains unchanged; `NOT_APPLICABLE` is
  available in the enum but appears in the summary only when actually emitted.
- Missing measurements remain `None` and are never converted to zero.
- No deterministic PASS/FAIL logic was changed.

## Tests added

```text
tests/domain/test_building_model.py
tests/domain/test_legacy_adapter.py
tests/domain/test_identity_preservation.py
tests/domain/test_units.py
tests/domain/test_finding_contract.py
tests/reporting/test_finding_serialization.py
tests/reporting/test_stable_finding_ids.py
tests/reporting/test_stage_status.py
```

## Deferred to later phases

The following items are intentionally not implemented in Phase 1:

- unified public pipeline orchestration;
- versioned manual-input schema and per-element overrides;
- Quality plugin registry;
- full Room/Space checks;
- semantic-catalog consolidation;
- report v1.0 JSON Schema;
- full BCF viewpoints/component selection.
