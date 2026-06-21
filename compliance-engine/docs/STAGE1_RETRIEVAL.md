# Stage 1 — Retrieval Upgrades: What Was Built and How to Run It

**Branch:** `feature/stage1-retrieval` (full git history included in this zip — one
conventional commit per step)
**Test suite:** `pytest --tb=short` → **54 passed** (verified in this snapshot)
**Scope guarantee:** the four deterministic agents, their PASS/FAIL logic, and
`MabhasRetriever.retrieve()`'s public signature and hit-dict shape are unchanged.
A permanent regression test now enforces this.

---

## 1. What each step added (one ablation variable per step)

| Step | Variable changed | Key files | Commit |
|---|---|---|---|
| 1 | Evaluation harness + baseline run slot | `eval/metrics.py`, `eval/retrieval_eval.py`, `eval/test_metrics.py` | `d335171` |
| 2 | Embed **normalized** Persian text (320/328 passages change) | `rag/rag_index.py` (`_build_passage_text`), `--retriever` flag | `a99213e` |
| 3 | **Lexical** leg (tsvector + GIN, `'simple'` config) + **hybrid RRF** fusion | `rag/schema.sql`, `rag/rag_retriever.py` (`lexical_retrieve`, `hybrid_retrieve`, `rrf_fuse`), `eval/test_rrf.py` | `fc0b721` |
| 4 | **Cross-encoder rerank** of fused top-50 (BAAI/bge-reranker-v2-m3) | `rag/reranker.py`, `hybrid_retrieve(rerank=True)`, `--rerank` flag, latency capture | `da307da` |
| 5 | **Contextual retrieval** — LLM-generated `context_fa` prepended at index time | `rag/contextualize.py`, schema (`context_fa`, lexeme over stored `passage`), `eval/test_contextualize.py` | `7547e29` |
| 6 | **Embedder ablation** — `EMBED_MODEL` env switch (e5 default ↔ `BAAI/bge-m3`) | `rag/embeddings.py`, `eval/test_embeddings.py` | `013658c` |
| 7 | **Production wiring** + verdict **regression guard** + SUMMARY generator | `build_default_retriever()`, `services/orchestrator.py`, `api/pipeline.py`, `eval/test_verdict_regression.py`, `eval/make_summary.py`, `conftest.py` | `0953e4b` |

### Architecture after Stage 1

```
query ──► dense leg (pgvector cosine, EMBED_MODEL) ──┐
      └─► lexical leg (tsvector ts_rank_cd, OR-query) ┴─► RRF fuse (k=60, top-50)
                                                           │
                                              cross-encoder rerank (bge-reranker-v2-m3)
                                                           │
                                                        top-k hits
```
Indexed passage (per clause) = `context_fa + heading_fa_normalized +
text_fa_normalized + text_en`, stored verbatim in the `passage` column.
The dense embedding, the lexical `lexeme` tsvector, **and** the reranker all
consume this identical stored passage (three-way byte parity).
`text_fa` in the DB remains the raw, authoritative clause text for the agents.

---

## 2. Deviations from the step specs (all surfaced during the work)

1. **Step 3 — `plainto_tsquery` → OR `to_tsquery`.** As specified, `plainto_tsquery`
   ANDs every token: all 43 eval questions matched **zero** rows in both languages
   (verified: 0 AND vs 315 EN / 222 FA OR matches). `lexical_retrieve` builds an OR
   tsquery over unique `\w+` tokens; `ts_rank_cd` + `'simple'` are exactly per spec.
2. **Step 5 — lexeme requires stored columns.** A generated column cannot see JSON
   fields, so `context_fa` became a real column and `lexeme` was regenerated over
   the stored `passage` (DROP+ADD migration; generated expressions cannot be altered).
3. **Known confound:** Step 5 also moved the *lexical* leg from raw to
   normalized+contextual text for the first time. To isolate normalization-only on
   the lexical leg, re-ingest `mabhas_clauses_normalized.json` on the Step 5 schema
   and run `--retriever lexical`.
4. **Bug fixes found by the harness** (pre-existing, would have failed identically
   with real models): query vector bound as `numeric[]` breaking pgvector `<=>`
   (fixed with `np.asarray`); stale `app.*` imports after the `app/`→`api/` rename
   (API could not start; fixed in `api/main.py`, `api/tasks.py`, `tests/test_api.py`).
5. **`created_at` is not refreshed by the upsert** — re-ingests use
   `TRUNCATE mabhas_clauses;` first, so timestamps genuinely reflect re-embeds.

---

## 3. What is already verified vs. what YOU must run

**Verified in this zip (no models needed):** all 54 tests; lexical eval end-to-end —
`eval/results/lexical_{en,fa}.json` are **real** numbers (the lexical leg uses no
embeddings) and must reproduce exactly on your machine: EN recall@10 = 0.326,
FA = 0.244. Treat that as your integrity check.

**Must run on your machine (model downloads / GPU / API key):**

```bash
# ── 0. setup ────────────────────────────────────────────────────────────────
git checkout feature/stage1-retrieval
pip install -r requirements.txt
export DATABASE_URL=postgresql://<user>:<pass>@localhost:5432/compliance
psql "$DATABASE_URL" -f rag/schema.sql          # idempotent, applies ALL migrations
pytest --tb=short                                # expect: 54 passed

# ── 1. BASELINE (raw text, e5) ──────────────────────────────────────────────
# Temporarily check out the Step-1 passage builder so the baseline embeds RAW text:
git stash && git checkout d335171 -- rag/rag_index.py
psql "$DATABASE_URL" -c "TRUNCATE mabhas_clauses;"
python -m services.rag_index --input data/mabhas_clauses_normalized.json \
    --scope M-4 M-2 M-1 all_residential any
python -m eval.retrieval_eval --run-name baseline --query-lang both --k 1 3 5 10
git checkout HEAD -- rag/rag_index.py && git stash pop || true

# ── 2. NORMALIZED ───────────────────────────────────────────────────────────
psql "$DATABASE_URL" -c "TRUNCATE mabhas_clauses;"
python -m services.rag_index --input data/mabhas_clauses_normalized.json \
    --scope M-4 M-2 M-1 all_residential any
python -m eval.retrieval_eval --run-name normalized --retriever dense --query-lang both --k 1 3 5 10

# ── 3. LEXICAL + HYBRID (same index) ────────────────────────────────────────
python -m eval.retrieval_eval --run-name lexical --retriever lexical --query-lang both --k 1 3 5 10
#   ^ must reproduce the committed numbers exactly
python -m eval.retrieval_eval --run-name hybrid  --retriever hybrid  --query-lang both --k 1 3 5 10

# ── 4. RERANKED (downloads ~2.3 GB reranker on first use) ───────────────────
python -m eval.retrieval_eval --run-name reranked --retriever hybrid --rerank --query-lang both --k 1 3 5 10

# ── 5. CONTEXTUAL (needs ANTHROPIC_API_KEY; ~12–20 min, ~$1) ────────────────
export ANTHROPIC_API_KEY=sk-ant-...
python -m services.contextualize --limit 5       # pilot: eyeball 5 contexts first
python -m services.contextualize                 # full pass (resumes past pilot)
psql "$DATABASE_URL" -c "TRUNCATE mabhas_clauses;"
python -m services.rag_index --input data/mabhas_clauses_contextual.json \
    --scope M-4 M-2 M-1 all_residential any
python -m eval.retrieval_eval --run-name contextual --retriever hybrid --rerank --query-lang both --k 1 3 5 10

# ── 6. BGE-M3 ABLATION (downloads ~2.3 GB embedder) ─────────────────────────
export EMBED_MODEL=BAAI/bge-m3                   # SAME shell for ingest AND eval
psql "$DATABASE_URL" -c "TRUNCATE mabhas_clauses;"
python -m services.rag_index --input data/mabhas_clauses_contextual.json \
    --scope M-4 M-2 M-1 all_residential any
python -m eval.retrieval_eval --run-name bge_m3 --retriever hybrid --rerank --query-lang both --k 1 3 5 10
unset EMBED_MODEL

# ── 7. PICK WINNER, FINAL INDEX, SUMMARY ────────────────────────────────────
# Re-ingest with the winning EMBED_MODEL (repeat step 5's ingest, with or
# without EMBED_MODEL exported) so the production index is the winner.
python -m eval.make_summary                      # fills eval/results/SUMMARY.md
pytest --tb=short                                # final guard: 54 passed
```

> **EMBED_MODEL hazard:** an index ingested under one model and queried under
> another returns silent garbage. The ingest banner prints the active model and
> every run JSON records `model_name` — check them when in doubt.

---

## 4. File inventory added/changed in Stage 1

```
conftest.py                       pytest bootstrap (services.* namespace)
rag/embeddings.py                 EMBED_MODEL switch (e5 prefixes vs raw bge-m3)
rag/rag_index.py                  contextual passage builder + context_fa column
rag/rag_retriever.py              dense/lexical/hybrid(+rerank), rrf_fuse,
                                  build_default_retriever()
rag/reranker.py                   lazy FlagReranker singleton
rag/contextualize.py              one-time LLM context pass (FA/EN prompts inside)
rag/schema.sql                    + passage, context_fa, lexeme migrations (idempotent)
services/orchestrator.py          factory fallback for the LLM pass (verdicts untouched)
api/pipeline.py                   factory-built retriever passed through
eval/metrics.py / test_metrics.py             pure metrics + 15 tests
eval/retrieval_eval.py                        CLI harness (—run-name/—retriever/—rerank/—k)
eval/test_rrf.py / test_contextualize.py /    8 + 10 + 7 tests
     test_embeddings.py
eval/test_verdict_regression.py               6-test deterministic-spine guard
eval/make_summary.py                          SUMMARY.md generator
eval/results/lexical_{en,fa}.json             REAL committed results (reproduce!)
eval/results/SUMMARY.md                       ablation table (fills after your runs)
```

## 5. Hand-off to Stage 2

Stage 2 (CRAG) builds on: the eval harness and its run-JSON format, the
`_build_retriever` factory seam in `eval/retrieval_eval.py`, the
`build_default_retriever()` production seam, and the SUMMARY table as the
measured baseline. The retrieval-confidence evaluator (Stage 2, step 1) can
consume `score`/`rrf_score` directly from the hit dicts.
