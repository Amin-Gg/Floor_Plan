# Stage 8 Remediation — Phase 4 Implementation

## Objective

Phase 4 turns the Phase 3 Quality plugin framework into a meaningful model
readiness gate. The implementation checks whether the canonical
`BuildingModel` contains sufficient, coherent, and geometrically usable data
before regulatory checks are interpreted.

## Runtime order

```text
source
  → canonical BuildingModel
  → manual-input merge
  → category/review enrichment
  → Phase 4 Quality plugins
  → deterministic compliance
```

A Quality failure is not automatically a project-wide compliance block.
Dependency-specific missing data stays `NOT_EVALUATED`, while independent
rules continue to run.

## Plugin registry

The default order is:

```text
contract_read
space_tagging
required_properties
unit_consistency
storey_consistency
element_confidence
scale_confidence
manual_parameters
opening_placement
```

This order is explicit and deterministic. Each plugin remains independently
testable and cannot suppress later plugins on failure.

## Room/Space validation

The `space_tagging` plugin now owns the complete Phase-4 Space contract while
preserving legacy category codes.

It checks:

- model-to-Space representation;
- stable internal and IFC identity;
- name and canonical type;
- declared area and boundary-derived area;
- boundary closure, finite coordinates, non-zero area, and topology validity;
- Storey assignment;
- topology regions supplied by reconstruction but not tagged as Spaces;
- Door-to-Space connectivity;
- Space overlap.

Finding details include `blocks_capabilities`, allowing later dependency-aware
reporting and gating without converting every model deficiency into a global
failure.

## Geometry contract

`domain.geometry` is the single geometry seam used by the new checks.
Coordinates remain in millimetres. `Polygon2D` now exposes deterministic:

- ring normalization;
- unique vertex count;
- signed/absolute area;
- derived m² area;
- centroid;
- Shapely-backed validity, intersection, coverage, and boundary distance.

No Quality plugin reads IFC geometry directly.

## Unit boundary

`building_model_from_legacy()` normalizes supported units once:

```text
mm / cm / m   → mm
mm2 / cm2 / m2 → m2
```

When the legacy payload omits units, Stage-8 compatibility still assumes
millimetres and m², but `QC-UNIT-001` records that the assumption is not a
trusted explicit contract. Unsupported units are not converted and cause a
blocking Quality finding.

## IFC Storey propagation

IFC ingest now reads Storeys before elements and resolves element containment
from:

- `IfcRelContainedInSpatialStructure`;
- `IfcRelAggregates` / `Decomposes` for Spaces.

Openings inherit host-wall Storey when direct containment is absent.

## Required properties

The Phase-4 bridge section `quality_requirements` in
`data/irpset_catalog.yaml` drives `QC-PROP-001`. It currently covers verdict-
relevant dimensions for Walls, Doors, and Windows. Phase 5 will migrate this
bridge into the richer versioned semantic catalog and remove remaining legacy
catalog structure.

`QC-PROP-002` consumes explicit mapping-evidence hooks stored under:

```text
properties._mapping_issues
extras._property_mapping_issues
```

The checker does not guess where a source property originally lived.

## Opening placement

Openings are internally interpreted with a canonical centre offset.
Legacy placement inputs may declare:

```text
insertion_convention = start | center | end
insertion_offset_mm
```

They are normalized to `OpeningPlacement.center_offset_mm` before endpoint
checking. Existing geometric insertion points remain the default source.

## Known limitations retained for later phases

- `QC-SPACE-008` needs reconstruction topology regions supplied explicitly;
  this phase does not claim complete IfcSlab coverage checking.
- `QC-PROP-002` requires ingest mapping evidence; Phase 5 will make the catalog
  the complete source of entity/Pset/property expectations.
- Internal Window detection is 2D plan-based and does not replace federated 3D
  clash detection.
- Quality dependency metadata is emitted now; richer report-level dependency
  visualization belongs to Report v1.0 in Phase 7.
