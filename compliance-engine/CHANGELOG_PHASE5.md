# Changelog — Stage 8 Remediation Phase 5

## Scope

Phase 5 replaces duplicated semantic mappings and hard-coded normalization
vocabularies with two validated, versioned YAML contracts. IFC ingest, Quality
requirements, unit declarations, and numeric-clause property semantics now read
from the same semantic catalog. Controlled values are request-isolated and
multilingual.

## Added

- `standards/semantic_property_catalog.yaml` — canonical property mapping,
  element requirements, units, and compliance dependency declarations.
- `standards/controlled_values.yaml` — room, boolean, occupancy, and door-type
  vocabularies with Persian and English aliases.
- Typed standards models and thread-safe fail-fast loaders.
- Generic `NormalizedValue` result containing raw/canonical value, vocabulary,
  matched alias, confidence, and source.
- Catalog-driven IFC property extraction for Wall, Door, Window, and Space.
- Catalog-driven numeric-clause property vocabulary and dimensional unit lookup.
- Startup validation of both standards contracts.
- Tests for malformed catalogs, custom mapping behavior, dependency changes,
  multilingual normalization, unknown preservation, and request concurrency.

## Changed

- `ingest/semantic_catalog.py` is now a compatibility facade; it contains no
  duplicated domain mapping.
- `data/irpset_catalog.yaml` is a migration marker, not a second contract.
- Required-property validation reads required/min/max/unit/required-for directly
  from the canonical catalog.
- Unit Quality messages derive supported unit lists from the catalog.
- Room aliases are no longer hard-coded in Python.
- Request-specific aliases no longer mutate process-global state.
- Quality checker version is `quality-stage8-phase5`.

## Compatibility

- Existing `pset_name`, `prop`, `param_map`, `quality_requirements`, and
  `reload_catalog` imports remain available through the compatibility facade.
- `IRPSET_CATALOG` remains accepted as a legacy environment alias;
  `SEMANTIC_PROPERTY_CATALOG` is the preferred variable.
- Existing public API and pipeline entry points are unchanged.
- All 341 reference compliance findings are unchanged from Phase 4.

## Verification

See `docs/PHASE5_ACCEPTANCE.md` and the JUnit reports in
`docs/test-results/`.
