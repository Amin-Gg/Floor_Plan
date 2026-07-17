# Phase 6 Implementation — IFC Schema Validator Completion

## Objective

Complete the IFC schema gate so downstream Quality and Compliance stages only
receive structurally trustworthy models. Phase 6 keeps IFC as an adapter format
and does not add regulatory rules.

## Public API

New code imports from:

```python
from validation.schema import (
    ParsedIfcSource,
    SchemaValidationPolicy,
    validate_ifc_schema_context,
)
```

`ingest.schema_validator` remains a compatibility facade for existing callers.

## Explicit parse context

`ParsedIfcSource` contains:

```text
path
model
model_name
model_fingerprint
schema
```

The pipeline parses the IFC once, validates that parsed object, then passes the
same object to `ifc_to_building_model`. There is no hidden process-global cache
and no path-based parser reuse.

## Default policy

```python
SchemaValidationPolicy(
    supported_versions={"IFC4", "IFC4X1", "IFC4X3"},
    allow_ifc2x3=False,
    strict_mandatory_attributes=True,
    require_spatial_hierarchy=True,
    require_unique_global_ids=True,
)
```

`IFC4X3` includes official maintenance identifiers such as `IFC4X3_ADD1`,
`IFC4X3_ADD2`, and `IFC4X3_TC1`.

IFC2X3 requires explicit opt-in because the canonical semantic catalog and
normalization path are IFC4-oriented. Opt-in does not bypass mandatory
attribute validation unless the caller explicitly disables that independent
policy flag.

## Blocking checks

| Code | Check |
|---|---|
| `IFC-SCHEMA-001` | file exists, is readable, and parses |
| `IFC-SCHEMA-002` | schema identifier is allowed by policy |
| `IFC-SCHEMA-003` | exactly one `IfcProject` |
| `IFC-SCHEMA-004` | at least one `IfcSite` |
| `IFC-SCHEMA-005` | at least one `IfcBuilding` |
| `IFC-SCHEMA-006` | at least one `IfcBuildingStorey` |
| `IFC-SCHEMA-007` | core spatial/product entities have `GlobalId` |
| `IFC-SCHEMA-008` | every Site/Building/Storey has the expected direct `IfcRelAggregates` parent |
| `IFC-SCHEMA-010` | every non-empty `IfcRoot.GlobalId` is unique |
| `IFC-SCHEMA-011` | non-optional explicit attributes satisfy schema metadata, including aggregate lower bounds |

`IFC-SCHEMA-009` remains a non-blocking alert when the model contains no
engine-consumable spaces, walls, doors, or windows.

## Spatial hierarchy

Entity existence is not sufficient. The required direct chain is:

```text
IfcProject
  └─ IfcRelAggregates → IfcSite
       └─ IfcRelAggregates → IfcBuilding
            └─ IfcRelAggregates → IfcBuildingStorey
```

Every Site, Building, and Storey must have exactly one parent of the expected
type. Product containment relationships such as
`IfcRelContainedInSpatialStructure` are covered by generic mandatory-attribute
validation; semantic Storey assignment remains a Quality-layer responsibility.

## Mandatory attributes

The implementation uses IfcOpenShell schema declarations:

```text
schema_by_name(...)
declaration_by_name(...)
all_attributes()
attribute.optional()
aggregation.bound1()
```

It does not maintain a hand-written copy of buildingSMART mandatory attributes.
A required aggregate with fewer members than its declared lower bound is a
schema failure.

## Pipeline modes

Both modes run identical blocking schema checks:

```text
precheck   = schema + quality
full_check = schema + quality + compliance + report
```

Mode never weakens schema validation.
