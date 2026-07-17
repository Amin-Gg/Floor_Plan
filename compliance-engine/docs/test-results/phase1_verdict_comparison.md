# Phase 1 Deterministic Verdict Comparison

The same full clause corpus and `tests/fixtures/sample_plan.ifc` were executed
against the Phase 0 remediation baseline and the Phase 1 implementation with
LLM advisory disabled.

```text
Phase 0 summary:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Phase 1 summary:
PASS=15, FAIL=9, NEEDS_REVIEW=309, NOT_EVALUATED=8

Finding rows compared:
341

Comparison basis:
(article_id, verdict, element_id, measured, required)

Result:
identical
```

The shared contracts and canonical-model adapter did not change deterministic
compliance behavior.
