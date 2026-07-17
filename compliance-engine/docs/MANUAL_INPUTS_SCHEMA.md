# Manual Inputs Schema v1.0

The manual-input contract supplies dimensions that cannot be recovered reliably
from a 2D plan. It is merged into the canonical `BuildingModel` **before** the
Quality stage and before deterministic compliance evaluation.

## Wire format

```json
{
  "schema_version": "1.0",
  "project": {
    "default_storey_height_mm": 3200,
    "finished_floor_level_mm": 0,
    "floor_thickness_mm": 200
  },
  "defaults": {
    "wall_height_mm": 3200,
    "door_height_mm": 2100,
    "window_width_mm": 1200,
    "window_height_mm": 1500,
    "window_sill_height_mm": 900
  },
  "element_overrides": {
    "windows": {
      "W-01": {
        "width_mm": 1400,
        "height_mm": 1600,
        "sill_height_mm": 850
      }
    },
    "doors": {
      "D-01": {
        "height_mm": 2200
      }
    },
    "walls": {
      "WALL-08": {
        "height_mm": 3500,
        "thickness_mm": 200
      }
    }
  },
  "allow_unmatched_overrides": false
}
```

All dimensional values are millimetres. Unknown fields, booleans in numeric
fields, non-finite numbers, unsupported schema versions and out-of-range values
are rejected.

## Merge precedence

For v1.0 requests:

```text
element override
    > trusted model property
    > operator default
    > system fallback
```

A non-null dimension already attached to an element is treated as a model
property. Project/default values fill missing values only. An element-specific
override always wins.

The resolved source is stored under each element's provenance:

```json
{
  "_provenance": {
    "resolved_values": {
      "width_mm": {
        "value": 1400,
        "unit": "mm",
        "source": "element_override",
        "confidence": 1.0
      }
    }
  }
}
```

## Element identity matching

Override keys may match any of:

- canonical `internal_id`;
- original IFC `GlobalId` (`ifc_guid`);
- source/detector ID (`source_id`).

If a key matches multiple elements, the request is rejected as ambiguous.
An unmatched key is rejected by default. When
`allow_unmatched_overrides=true`, it is ignored and produces
`QC-MANUAL-OVERRIDE-001` in the Quality stage.

## Cross-field behavior

The parser rejects global default combinations such as:

```text
window_sill_height_mm + window_height_mm > wall_height_mm
```

Host-specific conflicts that require model geometry are detected after merge:

- `QC-MANUAL-FIT-001`: door height exceeds host-wall height;
- `QC-MANUAL-FIT-002`: window sill plus height exceeds host-wall height.

These are Quality findings. They do not create a false compliance verdict.

## API usage

### JSON `/analyze`

```json
{
  "bim_data": {"rooms": [], "walls": [], "doors": [], "windows": []},
  "manual_inputs": {
    "schema_version": "1.0",
    "defaults": {"wall_height_mm": 3200}
  }
}
```

### Multipart `/analyze-ifc`

Send the IFC as `file` and the JSON document as the `manual_inputs` form field.

## Removed flat `building_params` contract

The former public `building_params` object is not accepted. Both `/analyze`
and `PipelineRequest(source_type="bim_data")` reject value-bearing blocks with
a migration error. Manual Inputs v1.0 is the only supported operator-input
contract.

An enriched `bim_data` mapping generated after merge is an internal,
output-only deterministic-agent seam; it is not a public round-trip format.
For trusted in-process reuse, pass `BuildingModel`. For a new public run, send
the original raw `bim_data` and the Manual Inputs v1 document again.

## Honest missing-data policy

The manual-input system does not invent verdict-driving values. Missing or
untrusted data remains unavailable to the dependent rule, which returns
`NOT_EVALUATED` rather than PASS-by-absence.
