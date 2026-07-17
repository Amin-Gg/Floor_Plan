# Phase 2 Deterministic Verdict Comparison

Reference input:

```text
tests/fixtures/sample_plan.ifc
data/mabhas_clauses.json
```

Configuration:

```text
LLM_PASS_ENABLED=0
no new manual-input values
use_langgraph=False
```

Results:

```text
Phase 1:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Phase 2:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Compared rows: 341
Comparison fields:
article_id, verdict, element_id, measured, required

Result: identical
```

A separate acceptance run supplied `wall_height_mm=3200`; its changed height
verdicts are intentional input effects, not an engine regression.
