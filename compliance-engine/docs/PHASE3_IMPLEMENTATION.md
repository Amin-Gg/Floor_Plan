# Stage 8 Remediation — Phase 3 Implementation

## Purpose

Phase 3 replaces the 375-line Quality Check monolith with an explicit,
ordered, plugin-based validation layer while preserving all existing Stage 8
quality behavior and deterministic compliance verdicts.

## Runtime architecture

```text
BuildingModel
    ↓
QualityContext.from_model(...)
    ↓
validation.quality.checker
    ↓
ordered plugin registry
    ├── contract_read
    ├── space_tagging
    ├── element_confidence
    ├── scale_confidence
    ├── manual_parameters
    └── opening_placement
    ↓
shared ValidationResult + Finding[]
```

New validation code consumes the canonical `BuildingModel`. The legacy
`services.quality_checker.run_quality_checks(bim_data)` function remains only as
a transition wrapper:

```text
legacy bim_data → BuildingModel → plugin checker → stage dictionary
```

## Package layout

```text
validation/quality/
  protocol.py
  context.py
  findings.py
  registry.py
  checker.py
  checks/
    contract_read.py
    space_tagging.py
    element_confidence.py
    scale_confidence.py
    manual_parameters.py
    opening_placement.py
```

## Plugin contract

Each plugin declares:

```python
code_prefix: str
codes: tuple[str, ...]
name: str
blocking: bool

applies_to(model, context) -> bool
run(model, context) -> list[Finding]
```

A plugin must not:

- mutate global state;
- generate reports;
- execute regulatory compliance clauses;
- read IFC directly;
- swallow exceptions silently;
- emit undeclared finding codes;
- emit findings for a stage other than `quality`.

## Registry behavior

The registry is explicit and ordered. It validates:

- unique plugin names;
- unique code prefixes;
- unique finding codes;
- code-to-prefix consistency;
- presence of required protocol members.

Request-local extensions can use `build_registry(extra_checks)` without
mutating the default registry.

## Error policy

A plugin failure never suppresses later plugins.

```text
non-blocking plugin error
    → QC-INTERNAL-001 / ALERT
    → quality status passed_with_alerts

blocking plugin error
    → QC-INTERNAL-001 / FAIL
    → quality status failed
```

The internal finding records:

- plugin name;
- code prefix;
- failure phase (`applies_to` or `run`);
- exception type;
- blocking policy.

The exception is also logged with traceback.

## Context isolation

`QualityContext` copies and freezes request-level review data, tolerances and
metadata. Built-in plugins are stateless. No request may mutate aliases,
thresholds, tolerances or another request's quality context.

## Pipeline integration

After category normalization and the review pre-pass, the authoritative
pipeline rehydrates the enriched legacy seam into a canonical `BuildingModel`.
The plugin layer runs against that model. Compliance continues to use the
existing legacy adapter, so deterministic agents are unchanged.

```text
manual merge
→ legacy adapter
→ normalize/review pre-pass
→ canonical BuildingModel
→ quality plugins
→ deterministic compliance
```

## Existing checks migrated

| Plugin | Existing codes |
|---|---|
| ContractReadCheck | QC-CONTRACT-001 |
| SpaceTaggingCheck | QC-SPACE-TAG-001, QC-SPACE-TAG-002 |
| ElementConfidenceCheck | QC-ELEM-CONF-001 |
| ScaleConfidenceCheck | QC-SCALE-001 |
| ManualParametersCheck | QC-PARAM-001 |
| OpeningPlacementCheck | QC-PLACE-001 … QC-PLACE-006 |

Phase 3 deliberately does **not** claim completion of the new Room/Space,
required-property, unit, storey or endpoint-aware placement checks. Those are
Phase 4 work and will be added as new plugins rather than placed back into a
central file.

## Backward compatibility

Existing callers may continue to import:

```python
from services.quality_checker import run_quality_checks
```

The compatibility wrapper returns the same stage dictionary shape and stores it
at `bim_data["_quality"]`.

New code should import:

```python
from validation.quality import QualityContext, run_model_quality_checks
```

## Deterministic behavior

No numeric, topology, opening, safety or RAG verdict logic was modified.
Quality finding behavior for the reference IFC was compared with Phase 2, and
the deterministic compliance snapshot was compared row-by-row.
