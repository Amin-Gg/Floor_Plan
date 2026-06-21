# Stage 2 — Reasoning & Robustness (CRAG): What Was Built and How to Run It

Branch: `feature/stage2-reasoning` (on top of `feature/stage1-retrieval`).
Verified in this zip: **138 tests pass** — Stage 1's original 54 (verdict
regression and factory routing included) plus 84 new Stage 2 tests. None of
them need a database, a model download, or an API key.

Goal: make retrieval **adaptive**. When a retrieval looks unreliable, try a
different strategy (HyDE / step-back / multi-query) *before* escalating to
the human-review queue — so the existing agent architecture yields more
automated coverage at an audited, per-query LLM cost. The four deterministic
agents and their PASS/FAIL verdicts are untouched (out of scope by design,
and guarded by `eval/test_verdict_regression.py`).

---

## 1. What each step added

| Step | Component | File | One-line behavior |
|---|---|---|---|
| 1 | Confidence evaluator | `rag/retrieval_evaluator.py` | HIGH/MEDIUM/LOW from reranker-score signals only (top-1, top-1→3 gap, dispersion); zero models, zero LLM calls |
| 2 | HyDE | `rag/query_transforms.py` + `hyde_retrieve` | LLM writes a hypothetical Mabhas-register clause; retrieval runs on it (Gao et al. 2022) |
| 3 | Step-back & multi-query | same file + `stepback_retrieve`, `multi_query_retrieve` | broader principle question / n reformulations; RRF-fused, then cross-encoded against the ORIGINAL query (Zheng et al. 2023) |
| 4 | Rule-based router | `rag/query_router.py` | six ordered deterministic rules (R1–R6) pick a transform per query; explainable, free, reproducible |
| 5 | Corrective loop | `rag/crag_retriever.py` + `CRAG_ENABLED` factory flag | confidence gate → route → primary → fallback → give-up-to-human-queue (Yan et al. 2024; cf. Self-RAG, Asai et al. 2023) |

### Architecture after Stage 2

```
agent / LLM-pass calls  retriever.retrieve(query, top_k=3, filters...)
        │   build_default_retriever()  —  CRAG_ENABLED=1 (default)
        ▼
CorrectiveRetriever                                  services/crag_retriever.py
  A  hybrid_retrieve(rerank=True, filters, top_k=max(top_k,5))
  B  evaluate_retrieval(sigmoid over CE logits)
  C  HIGH ──────────────────────────────▶ return        (zero LLM calls)
  D  route_query  (R2 exception / R3 short-numeric / R4 multi-element /
                   R5 low-confidence / R6 default; R1 unreachable here)
  E  primary transform → re-evaluate → not LOW ─▶ return  (1 LLM call)
  F  fallback transform ────────────────▶ return          (2 LLM calls)
  G  give up ───────────────────────────▶ return initial
        ▼
4 deterministic agents (verdicts unchanged) → NEEDS_REVIEW → human queue
                                  ▲ the queue stays the FINAL corrective fallback
```

Invariants preserved: `retrieve()` signature and default `top_k=3`; the
canonical hit-dict shape (+ additive `rrf_score`); `mabhas_part`/`rule_type`
filters honored at the SQL level in every leg; every LLM call uses
`claude-sonnet-4-6` @ temperature 0.2 and is counted
(`services.query_transforms.llm_counters()`); the LLM never touches a verdict.

## 2. Deviations & corrections surfaced while integrating against the real Stage 1 code

The Stage 2 steps were specified before this snapshot was available; wiring
them into the real code falsified several assumptions. All fixes below are
in this zip and covered by tests.

1. **CE scores are logits → sigmoid before thresholding.** The Step 1 spec
   thresholds (0.6 / 0.3 / 0.15 / 0.03) assume scores in [0, 1], but
   `services/reranker.py` returns raw unbounded logits. `evaluate_retrieval`
   gained `score_transform="sigmoid"` (monotonic — ranking unchanged, only
   the threshold space normalized); CRAG, the router pass-through and the
   harness always use it. RRF scores (~0.01–0.03) fit neither space, hence
   `--log-confidence` requires a reranked mode.
2. **No `rag/fusion.py`.** Stage 1 already exposes a pure, tested
   `rrf_fuse()` (id-lists → fused scores, `eval/test_rrf.py`); the Step 3
   plan to extract an RRF helper was obsolete. `stepback_retrieve` /
   `multi_query_retrieve` reuse the existing function with hybrid's
   `by_id` representative-hit pattern — no Stage 1 code path was touched, so
   no RRF-equivalence regression run is needed.
3. **Reranker is a module function, not an attribute.** `_rerank_against`
   mirrors hybrid's rerank stage exactly: stored ingest-time passage first,
   `_build_rerank_passage` fallback, lazy `services.reranker` import,
   `last_rerank_seconds` probe, fused score preserved as `rrf_score`.
4. **Filters are SQL-level, not post-hoc.** `hybrid_retrieve` already
   forwards `mabhas_part`/`rule_type` into both legs (Stage 1 / Step 7), so
   the planned over-fetch-and-post-filter in CRAG was replaced by plain
   forwarding into every leg, including the transform legs.
5. **`rag.*` package.** The Stage 2 modules live in `rag/` and import as
   `rag.*` (a standard Python package with `__init__.py`). Dependency order
   still matters — `query_transforms` precedes `rag_retriever`, which imports
   it. (Historical note: earlier stages briefly mirrored these modules under
   both `RAG/` and `services/`; the repository was reconciled to a single
   `rag/` package during the GitHub-publication cleanup.)
6. **Default `top_k=3`, signal floor `max(top_k, 5)`.** Confidence gaps need
   a few ranks of context; CRAG's legs fetch ≥5 hits for evaluation and the
   result is sliced back to the caller's `top_k`.
7. **English is the primary query language** (the orchestrator's LLM pass
   retrieves with `rule_text[:120]` EN; agents never query). Transforms
   default to `language="auto"` (script detection), so the frozen
   `retrieve()` signature needs no language plumbing.
8. **Router bugfix:** the punctuation normalizer originally stripped
   apostrophes, so the R2 phrase `"doesn't apply"` could never match
   (`doesn't` → `doesn t`). Fixed + regression-tested; bare Persian «در»
   stays excluded from the door vocabulary (preposition homograph).
9. **Spec reconciliation (Step 1):** "all scores high and tightly clustered"
   classifies **LOW**, not HIGH — a tie among candidates is ambiguity. The
   single change point is marked in `retrieval_evaluator.py`.
10. **Structural notes:** R1 is unreachable inside CRAG (HIGH returns before
    routing; R1 serves standalone router use) and R6's `fallback=hyde` is
    dormant by spec (step E gates on `primary != none`) — so MEDIUM/no-trigger
    items land in `give_up_no_primary`. The trace separates the two give-up
    kinds so the ablation can quantify the dormant-fallback opportunity.

## 3. What is already verified vs. what YOU must run

**Verified in this zip (no models, no DB, no key):** all 138 tests
(`pytest --tb=short -q`). That includes: every router rule positive+negative,
every CRAG branch, sigmoid threshold behavior, rerank-against-original,
filter forwarding, the factory flag both ways, and Stage 1's untouched 54.

**Must run on your machine** (needs the ingested index, the reranker
download from Stage 1, and `ANTHROPIC_API_KEY` for transform runs — at 43
items × 2 languages ≈ 86 calls/run, each always-on run costs well under $1):

```bash
# ── 0. setup ────────────────────────────────────────────────────────────────
git checkout feature/stage2-reasoning
export DATABASE_URL=postgresql://...        # Stage 1 index, winning EMBED_MODEL
export EMBED_MODEL=<stage1-winner>          # same value the index was ingested with
export ANTHROPIC_API_KEY=sk-ant-...

# ── 1. GATING ONLY (Step 1) — retrieval identical to the Stage 1 winner ─────
# Metric columns MUST equal the Stage 1 'reranked'/winner run (integrity
# check); the new information is the per-label confidence stratification.
python -m eval.retrieval_eval --run-name s2_gating \
    --retriever hybrid --rerank --log-confidence --query-lang both
# Calibration target in stage2.confidence_summary:
#   LOW.recall@5 < MEDIUM.recall@5 < HIGH.recall@5, with a clear gap and a
#   meaningful minority of items in LOW+MEDIUM. If not, tune DEFAULT_THRESHOLDS
#   (services/retrieval_evaluator.py) and re-run — this is free (0 LLM calls).

# ── 2. ALWAYS-ON TRANSFORMS (Steps 2–3) ─────────────────────────────────────
python -m eval.retrieval_eval --run-name s2_hyde       --transform hyde       --log-confidence --query-lang both
python -m eval.retrieval_eval --run-name s2_stepback   --transform stepback   --log-confidence --query-lang both
python -m eval.retrieval_eval --run-name s2_multiquery --transform multi_query --log-confidence --query-lang both
# Headline cut (multi-hop recall@5; 11 items):  add  --hop-type multi_hop
# with run names like s2_hyde_mh so files don't collide.

# ── 3. SELECTIVE ROUTER (Step 4) ────────────────────────────────────────────
python -m eval.retrieval_eval --run-name s2_auto --transform auto \
    --log-confidence --query-lang both
# stage2.router_stats: per-rule counts + top-5 change rate;
# stage2.llm_cost.llm_calls_per_100_queries should be FAR below 100.

# ── 4. FULL CRAG LOOP (Step 5) ──────────────────────────────────────────────
python -m eval.retrieval_eval --run-name s2_crag --crag \
    --log-confidence --query-lang both
# stage2.crag_summary: branch counts (high_no_transform / transform_primary /
# transform_fallback / give_up_*) + top-5 change rate. per_item[].crag holds
# the full trace for stratified joins on item_id against any other run.

# ── 5. SUMMARY ──────────────────────────────────────────────────────────────
python -m eval.make_summary        # Stage 2 tables fill themselves from the
cat eval/results/SUMMARY.md        # s2_* result JSONs; '—' until runs exist

# ── 6. FLIP PRODUCTION ON (after the numbers justify it) ────────────────────
# CRAG_ENABLED defaults to 1: build_default_retriever() returns the wrapped
# retriever. Tests pin CRAG_ENABLED=0 via conftest.py. To run production
# without the corrective layer:  export CRAG_ENABLED=0
python -m pytest eval/test_verdict_regression.py -v     # must stay green
```

How to read the ablation (thesis framing): the **efficiency claim** is
structural — `auto`/`crag` calls-per-100-queries is just the share of
non-HIGH, rule-triggered items, so "X% of the best always-on recall@5 gain
at Y% of its LLM cost" can always be computed. The **effectiveness claim**
is empirical: expect transforms to help on the LOW stratum and the multi-hop
subset and to be neutral-to-negative on HIGH — that asymmetry is precisely
what justifies routing over always-on transformation. With 43 items, treat
differences under ~1 item (≈2.3pp overall, ≈9pp on the 11 multi-hop items)
as ties, mirroring the Stage 1 convention.

## 4. File inventory added/changed in Stage 2

```
rag/retrieval_evaluator.py       NEW   Step 1 — confidence gate (+ sigmoid)
rag/query_transforms.py          NEW   Steps 2-3 — hyde/stepback/multi-query + LLM cost counters
rag/query_router.py              NEW   Step 4 — R1-R6 rules, bilingual VOCAB in one dict
rag/crag_retriever.py            NEW   Step 5 — CorrectiveRetriever + last_trace
rag/rag_retriever.py             MOD   +_rerank_against/_fused_candidates, +hyde/stepback/
                                       multi_query_retrieve, factory CRAG_ENABLED branch
eval/retrieval_eval.py           MOD   namespace registration; --transform/--crag/--hop-type/
                                       --log-confidence; _AutoRouterRetriever; stage2 payload
eval/make_summary.py             MOD   Stage 2 tables (s2_* runs, LLM calls/100q column)
eval/results/SUMMARY.md          MOD   regenerated — Stage 2 section with '—' placeholders
conftest.py                      MOD   CRAG_ENABLED=0 pinned for the test suite
eval/test_retrieval_evaluator.py NEW   16 tests   eval/test_query_transforms.py   NEW 20 tests
eval/test_query_router.py        NEW   27 tests   eval/test_crag_retriever.py     NEW 13 tests
eval/test_stage2_retrieve_methods.py NEW 8 tests
STAGE2_REASONING.md              NEW   this document
```

## 5. Hand-off to Stage 3 (Graph Extension)

Stage 3 builds on: the `route_query` seam (R2 already flags exception
lookups — the graph-aware retriever with exception expansion can take over
that route), the per-item `crag` traces and `confidence` labels in the run
JSONs (join on `item_id` for the consolidated ablation table), the
`_build_retriever` factory seam for a graph-aware mode, and clause metadata
(`entities.applies_to_article` for `rule_type="exception"`) as graph edges.
The 6 exception items in the eval set are the headline Stage 3 subset;
expect to grow that slice when the regulation graph makes exception
expansion measurable.
