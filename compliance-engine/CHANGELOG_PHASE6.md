# Changelog — Stage 8 Remediation Phase 6

## Scope

Phase 6 completes the IFC schema gate. It adds explicit schema policy,
relationship-aware spatial hierarchy validation, duplicate GUID detection,
schema-metadata mandatory-attribute validation, and a one-parse IFC source
contract.

## Added

- `validation.schema` package.
- Immutable `SchemaValidationPolicy`.
- Explicit `ParsedIfcSource` shared between validation and ingest.
- `IFC-SCHEMA-008` spatial hierarchy check.
- `IFC-SCHEMA-010` duplicate `GlobalId` check.
- `IFC-SCHEMA-011` schema-metadata mandatory-attribute check.
- Tests for hierarchy, GUID uniqueness, mandatory aggregates, version policy,
  single parse, and mode-independent blocking.
- Phase 6 implementation and acceptance documentation.

## Changed

- Default schema support is now explicit: IFC4, IFC4X1, and IFC4X3 family.
- IFC4X2 is not accepted by prefix accident.
- IFC2X3 requires explicit compatibility opt-in.
- `services.validation_pipeline` stores the parsed source context and passes the
  same IfcOpenShell model to ingest.
- Schema checker version is `stage8-remediation-phase6`.
- `ingest.schema_validator` is now a compatibility facade.
- `validation.schema` is included in package metadata.

## Compatibility

- `validate_ifc_schema()` still returns `(result, parsed_model)`.
- `require_valid_ifc()` retains its historical exception contract.
- Existing imports from `ingest.schema_validator` remain valid.
- Existing API and pipeline entry points are unchanged.
- All 341 reference compliance findings are unchanged from Phase 5.

## Verification

See `docs/PHASE6_ACCEPTANCE.md` and Phase 6 JUnit reports.
