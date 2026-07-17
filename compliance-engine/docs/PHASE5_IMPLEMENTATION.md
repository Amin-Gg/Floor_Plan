# Phase 5 Implementation — Semantic Catalog and Controlled Values

## Objective

Make semantic mappings and controlled vocabularies configuration-driven without
turning configuration into an unvalidated free-form input.

## Canonical contracts

### Semantic Property Catalog

`standards/semantic_property_catalog.yaml` is the single source of truth for:

- IFC entity/property extraction mappings;
- required semantic properties;
- data types and canonical units;
- minimum/maximum validity bounds;
- Quality capabilities blocked by missing data;
- compliance clause-property vocabulary and dimensions;
- legacy Pset and parameter mapping compatibility.

The loader validates references between element mappings and Pset definitions,
required legacy keys, supported units, range consistency, and schema version.
Missing or invalid files fail immediately.

### Controlled Values

`standards/controlled_values.yaml` defines versioned vocabularies for:

- room types;
- booleans;
- occupancy types;
- door types.

Normalization returns a `NormalizedValue` instead of silently guessing. Unknown
or ambiguous values preserve the raw value and have no canonical value.

## Request isolation

`extra_aliases` are copied into a request-local alias map. The base vocabulary
is immutable, so concurrent requests cannot affect each other. The previous
module-level `ALIASES.update(...)` behavior was removed.

## IFC ingest

`ingest/ifc_to_bim_data.py` reads ordered mappings from the catalog. Mappings
may reference an IFC attribute or a Pset/quantity key. A custom catalog change
therefore changes extraction behavior without editing Python.

## Quality and compliance use

- `QC-PROP-001` reads required, unit, min/max, and `required_for` directly from
  the catalog.
- `QC-UNIT-*` derives allowed declarations from the catalog.
- Numeric property classification and area/length dimension selection read the
  catalog's `compliance.clause_properties` section.
- Generic unit arithmetic remains in `domain.units`; domain vocabulary does not.

## Startup behavior

FastAPI calls `validate_standards()` during import/startup. Invalid deployment
configuration prevents the service from presenting itself as healthy.

## Environment variables

```text
SEMANTIC_PROPERTY_CATALOG=/path/to/semantic_property_catalog.yaml
CONTROLLED_VALUES_CATALOG=/path/to/controlled_values.yaml
```

`IRPSET_CATALOG` remains a temporary compatibility alias for the first value.
