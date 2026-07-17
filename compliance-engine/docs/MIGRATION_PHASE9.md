# Phase 9 Migration Guide

## 1. Pipeline API

### Removed

```python
run_pipeline(...)
run_pipeline_from_ifc(...)
run_ifc_compliance(...)
```

### Use

```python
from services.validation_pipeline import PipelineRequest, run_validation_pipeline

execution = run_validation_pipeline(PipelineRequest(
    source_type="ifc",
    ifc_path="plan.ifc",
    clauses=clauses,
    manual_inputs=manual_inputs,
    out_dir="artifacts/run",
))
```

For in-memory canonical models:

```python
PipelineRequest(source_type="building_model", building_model=model, ...)
```

For upstream 2D-to-3D mapping payloads:

```python
PipelineRequest(source_type="bim_data", bim_data=payload, ...)
```

## 2. Manual Inputs

### Removed

Flat public input:

```json
{"wall_height": 3200, "window_width": 1200}
```

### Use

```json
{
  "schema_version": "1.0",
  "project": {"default_storey_height_mm": 3200},
  "defaults": {
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
    "doors": {},
    "walls": {}
  }
}
```

The API returns HTTP 400 if the removed `building_params` field is sent to
either JSON `/analyze` or multipart `/analyze-ifc`. The unified pipeline applies
the same rejection for raw `bim_data` calls.

Value-bearing enriched `bim_data` produced after Manual Inputs resolution is
output-only. It must not be resubmitted as raw input. Use `BuildingModel` for
trusted in-process reuse, or resubmit the original raw mapping plus the Manual
Inputs v1 document.

## 3. Reporting

### Removed

```python
from services.report_generator import generate_reports
```

### Use

Prefer pipeline-generated reports. For explicit report rendering:

```python
from reporting.generator import generate_report_bundle
```

All formats render from a `ValidationReport v1.0`.

## 4. Quality checks

### Removed

```python
from services.quality_checker import run_quality_checks
```

### Use

```python
from validation.quality import QualityContext, run_model_quality_checks

context = QualityContext.from_model(model)
result = run_model_quality_checks(model, context=context)
```

Quality plugins require a canonical `BuildingModel`, not a raw mapping.

## 5. IFC Schema validation

### Removed import path

```python
from ingest.schema_validator import validate_ifc_schema
```

### Use

```python
from validation.schema import validate_ifc_schema_context
```

The returned `ParsedIfcSource` can be shared with ingest to avoid parsing twice.

## 6. Semantic catalog

### Removed import path

```python
from ingest.semantic_catalog import ...
```

### Use

```python
from standards import catalog_api
```

The YAML files under `standards/` remain the single source of truth.

## 7. Internal adapters

The canonical compliance seam remains:

```python
from validation.compliance.adapter import (
    building_model_from_bim_data,
    building_model_to_bim_data,
)
```

This is not a public alternate pipeline. It is the explicit adapter needed by
the already-validated deterministic agents.
