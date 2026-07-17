# Mabhas BIM Compliance Engine — Final R2 Release

A modular-monolith validation engine for checking Iranian residential building
models against digitised National Building Regulations (*Mabhas*). The system
accepts IFC or canonical BIM data, validates model integrity and quality before
regulatory evaluation, and produces explainable JSON, HTML, PDF, and BCF 2.1
reports.

## Final architecture

```text
IFC / bim_data / BuildingModel
             │
             ▼
     Boundary parsing and validation
             │
             ├── IFC Schema Gate
             │   file, version, hierarchy, GUIDs, mandatory attributes
             │
             ▼
      Canonical BuildingModel
             │
             ▼
      Manual Inputs v1.0 merge
             │
             ▼
      Plugin-based Quality Layer
             │
             ▼
  Deterministic Compliance Engine
             │
             ├── optional RAG/LLM explanation only
             │
             ▼
       ValidationReport v1.0
             │
             ├── JSON
             ├── HTML
             ├── PDF
             └── Full BCF 2.1
```

The deterministic engine is the only component allowed to produce `PASS` or
`FAIL`. RAG/LLM components may retrieve clauses and add advisory explanations,
but they cannot create, change, or override deterministic verdicts.

## Validation stages

1. **IFC Schema Validation**
   - supported IFC version policy;
   - one `IfcProject`;
   - Project → Site → Building → Storey hierarchy;
   - duplicate and missing `GlobalId` detection;
   - mandatory IFC attributes;
   - single IFC parse shared with ingest.

2. **Model Quality Validation**
   - required semantic properties and units;
   - Space identity, name, type, area, boundary, overlap, and storey;
   - Door/Window host and endpoint placement;
   - opening vertical fit;
   - Manual Input completeness and provenance;
   - plugin isolation and explicit internal-error findings.

3. **Regulatory Compliance**
   - deterministic numeric, topology, opening, and safety checks;
   - `NEEDS_REVIEW` for interpretive or unsupported rules;
   - `NOT_EVALUATED` when required model data is missing or untrusted;
   - RAG/LLM used only for eligible advisory explanations.

## Repository layout

| Path | Responsibility |
|---|---|
| `domain/` | Canonical `BuildingModel`, elements, geometry, identity, findings, and stage results |
| `ingest/` | IFC-safe opening, IFC → canonical model conversion, category normalization |
| `validation/schema/` | IFC schema policy and checks |
| `validation/quality/` | Plugin protocol, registry, context, and quality-check plugins |
| `validation/compliance/` | Canonical adapter and private prepared-input compliance runner |
| `manual_inputs/` | Manual Inputs v1.0 parser, typed models, and merge/provenance logic |
| `standards/` | Semantic Property Catalog, Controlled Values, validated loaders, query API |
| `services/` | Authoritative validation pipeline plus deterministic agent implementations |
| `reporting/` | ValidationReport v1.0 and JSON/HTML/PDF/BCF renderers |
| `api/` | FastAPI, optional Celery execution, job and artifact storage |
| `rag/` | Retrieval, graph retrieval, reranking, and optional advisory generation |
| `scripts/` | Acceptance, BCF validation, coverage, and audit utilities |
| `tests/`, `eval/` | Architecture, pipeline, validation, report, and retrieval tests |
| `docs/` | Architecture decisions, schemas, phase acceptance records, and migration docs |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

The exact Phase 0 environment snapshot is retained at:

```text
docs/STAGE8_PIP_FREEZE.txt
```

## CLI: validate an IFC model

```bash
python -m ingest.run_ifc tests/fixtures/sample_plan.ifc \
  --clauses data/mabhas_clauses.json \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --out artifacts/ifc_run
```

Precheck mode runs Schema + Quality without regulatory verdicts:

```bash
python -m ingest.run_ifc tests/fixtures/sample_plan.ifc \
  --precheck \
  --out artifacts/precheck
```

## Python API

```python
import json

from services.validation_pipeline import PipelineRequest, run_validation_pipeline

with open("data/mabhas_clauses.json", encoding="utf-8") as handle:
    clauses = [row for row in json.load(handle) if not row.get("skip_category")]

execution = run_validation_pipeline(PipelineRequest(
    source_type="ifc",
    ifc_path="tests/fixtures/sample_plan.ifc",
    clauses=clauses,
    manual_inputs={
        "schema_version": "1.0",
        "project": {"default_storey_height_mm": 3200},
        "defaults": {"window_sill_height_mm": 900},
        "element_overrides": {
            "windows": {
                "Wb": {
                    "width_mm": 1400,
                    "height_mm": 1500,
                    "sill_height_mm": 900,
                }
            },
            "doors": {},
            "walls": {},
        },
    },
    out_dir="artifacts/python_run",
    metadata={"plan_name": "sample_plan.ifc"},
    mode="full_check",
))

print(execution.compliance.summary if execution.compliance else {})
print(execution.reports)
```

All public source paths delegate to the same authoritative function:

```python
run_validation_pipeline(request)
```

## Manual Inputs v1.0

The public manual-input contract supports:

- project-level storey height, finished floor level, and floor thickness;
- defaults for walls, doors, and windows;
- per-window, per-door, and per-wall overrides;
- explicit merge provenance;
- strict unknown-key, type, range, and cross-field validation.

Full documentation:

```text
docs/MANUAL_INPUTS_SCHEMA.md
```

The old flat `building_params` public input was removed in Phase 9 and is
rejected at both HTTP and pipeline boundaries. A value-bearing enriched
`bim_data` mapping produced after Manual Inputs resolution is an internal,
output-only agent seam. Reuse `BuildingModel` in-process, or submit the
original raw `bim_data` together with Manual Inputs v1 again.

## FastAPI service

```bash
export CLAUSES_PATH=data/mabhas_clauses.json
uvicorn api.main:app --reload
```

Endpoints:

```text
GET  /health
POST /analyze
POST /analyze-ifc
GET  /jobs/{job_id}
GET  /jobs/{job_id}/report/{kind}
```

`kind` may be `html`, `pdf`, or `bcf`. JSON results are returned in the job
result payload and written as `compliance_result.json` in the job artifact set.

When `CELERY_BROKER_URL` is configured, jobs run through Celery. Without a
broker, development mode uses background threads and the same pipeline.

## Report outputs

Every report bundle is rendered from one `ValidationReport v1.0`:

```text
compliance_result.json
compliance_report.html
compliance_report.pdf
compliance_issues.bcf
compliance_issues.bcf.manifest.json
```

The JSON contract is published at:

```text
reporting/schemas/validation_report_v1.schema.json
```

BCF export uses real IFC `GlobalId` values for component selection. Findings
without a trustworthy IFC anchor remain in JSON/HTML/PDF instead of becoming
empty BCF topics.

## Final acceptance scenario

Run the complete Phase 9 acceptance scenario with one command:

```bash
python -m scripts.run_validation_acceptance \
  --ifc tests/fixtures/sample_plan.ifc \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --output-dir artifacts/remediation_acceptance
```

The scenario verifies:

- IFC schema success;
- two windows with different manual dimensions;
- a malformed Space with missing area and open boundary;
- a Window extending beyond its host Wall;
- at least one real compliance `FAIL`;
- at least one `NOT_EVALUATED` result;
- schema-valid JSON, HTML, PDF, and BCF output;
- BCF selected GUIDs against the source IFC.

## Tests

```bash
python -m compileall api ingest services domain validation reporting manual_inputs standards
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

The final release must have zero failures, errors, skips caused by missing core
functionality, and zero IfcOpenShell unraisable warnings.

## Release documentation

- `ARCHITECTURE.md` — final module boundaries and runtime contracts
- `PROJECT_CHANGES_PHASE0_TO_PHASE9.md` — complete remediation history
- `CHANGELOG_PHASE9.md` — final cleanup and release changes
- `docs/MIGRATION_PHASE9.md` — removed APIs and migration examples
- `docs/PHASE9_IMPLEMENTATION.md` — implementation details
- `docs/PHASE9_ACCEPTANCE.md` — final evidence and release gates
- `docs/BCF_INTEROPERABILITY_TEST.md` — BCF verification and GUI-viewer limitation

## Scope and known limitation

The automatic checks cover the deterministic rule types implemented by the
engine. Many regulations remain interpretive or unsupported and therefore
correctly produce `NEEDS_REVIEW`; missing trustworthy model data produces
`NOT_EVALUATED`. The engine does not claim full automation of the complete
Mabhas corpus.

The BCF archive is structurally and schema validated and its IFC selections are
cross-checked against the source model. A desktop GUI-viewer verification still
requires a supported viewer installed by the project team, as documented in
`docs/BCF_INTEROPERABILITY_TEST.md`.
