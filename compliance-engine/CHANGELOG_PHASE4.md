# Changelog — Stage 8 Remediation Phase 4

## Scope

Phase 4 completes the first production-grade Model Quality rule set on top of
the Phase 3 plugin framework. It adds Room/Space completeness, catalog-driven
required properties, unit and storey consistency, and endpoint-aware opening
placement without changing deterministic regulatory verdicts.

## Added

### Room/Space validation

- `QC-SPACE-001` — walls/enclosed model without Space representation.
- `QC-SPACE-002` — incomplete stable identity, including missing IFC GlobalId.
- `QC-SPACE-003` — missing human-readable room/space tag.
- `QC-SPACE-004` — missing/non-positive area or declared/derived area mismatch.
- `QC-SPACE-005` — missing boundary polygon.
- `QC-SPACE-006` — invalid/open/non-finite/self-intersecting boundary.
- `QC-SPACE-007` — missing or unresolved storey assignment.
- `QC-SPACE-008` — enclosed topology region not represented by a Space.
- `QC-SPACE-009` — missing/unresolved/ambiguous door-space connectivity.
- `QC-SPACE-010` — significant overlapping Spaces.
- Stage-8 category checks `QC-SPACE-TAG-001/002` remain supported.

### Semantic-property, unit, and storey checks

- `QC-PROP-001` — catalog-required property missing or invalid.
- `QC-PROP-002` — property mapping evidence indicates wrong entity/Pset/field.
- `QC-UNIT-001` — legacy payload omitted an explicit unit contract.
- `QC-UNIT-002` — unsupported unit declaration.
- `QC-STOREY-001` — Storey model/assignment missing.
- `QC-STOREY-002` — element references an unknown Storey.
- `QC-STOREY-003` — inconsistent FFL for the same Storey name or coordinate datum.

### Opening placement

- `QC-PLACE-007` — opening span exceeds either host-wall endpoint.
- `QC-PLACE-008` — missing/degenerate host geometry or missing insertion point.
- `QC-PLACE-009` — Window geometrically connects two internal Spaces.
- `QC-PLACE-010` — declared Door connectivity conflicts with geometry.
- `QC-PLACE-011` — Door/Window vertical extent exceeds host-wall height.
- Added canonical `OpeningPlacement(center_offset_mm)` and legacy
  start/center/end convention normalization.

### Geometry and ingest

- Added deterministic polygon closure, area, centroid, validity, overlap,
  containment, and boundary-distance helpers.
- IFC walls, openings, spaces, stairs, and slabs now preserve their containing
  `IfcBuildingStorey` through `storey_id`.
- IFC Space containment supports both `ContainedInStructure` and `Decomposes`.
- Supported legacy units are normalized exactly once at the domain boundary.
- Unit assumptions and source declarations are retained as Quality evidence.
- Added a Phase-4 `quality_requirements` bridge in `irpset_catalog.yaml`.

## Changed

- Quality checker version is now `quality-stage8-phase4`.
- Default plugin order now includes required-properties, units, and storeys.
- Severe Space topology failures and unsupported units mark the Quality stage
  `failed`; they do not globally suppress independent compliance checks.
- Prior category and placement tests now isolate their own check families while
  allowing the new independent plugins to report additional deficiencies.

## Compatibility

- Existing public entry points remain unchanged.
- Existing `services.quality_checker.run_quality_checks()` remains a legacy
  dictionary adapter.
- Existing `QC-PLACE-001..006` and `QC-SPACE-TAG-001/002` codes remain valid.
- Regulatory verdicts on the reference IFC and full clause corpus are byte-for-
  byte identical to Phase 3: 341/341 findings unchanged.

## Verification

- Core suite: `326 passed`.
- Full suite: `482 passed`.
- `PytestUnraisableExceptionWarning` is treated as an error: zero occurrences.
- Reference corpus verdicts:
  - PASS: 15
  - FAIL: 9
  - NEEDS_REVIEW: 309
  - NOT_EVALUATED: 8
