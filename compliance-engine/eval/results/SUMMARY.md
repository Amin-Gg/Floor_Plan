# Stage 1 — Retrieval Ablation Summary

Eval set: `data/mabhas_retrieval_eval.json` — 43 items (32 zero-hop, 11
multi-hop; 28 numeric / 6 spatial / 6 exception / 3 definition), gold =
`source_article_ids`, corpus = 328 ingestable Mabhas clauses.

Each row changes exactly ONE variable relative to the row above
(cumulative pipeline; per-run config, git commit and timestamp are recorded
in the corresponding `eval/results/<run>_<lang>.json`).

**Primary query language: English.** The production retrieval consumer is
the LLM interpretive pass, which queries with the clause's English rule
text (`services/orchestrator.py`, `retriever.retrieve(rule_text[:120])`);
the deterministic agents do not query the retriever at all. Persian is
reported as the secondary language for the Persian-facing UI path.

## English (primary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 |
|---|---|---|---|---|---|---|
| Baseline (e5 dense cosine, raw text) | 0.663 | 0.884 | 0.895 | 0.853 | 0.830 | 0.727 |
| + normalized text (Step 2) | 0.639 | 0.872 | 0.907 | 0.838 | 0.820 | 0.682 |
| + BM25 hybrid / RRF (Step 3) | 0.395 | 0.651 | 0.698 | 0.570 | 0.579 | 0.455 |
| + cross-encoder rerank (Step 4) | 0.721 | 0.895 | 0.919 | 0.891 | 0.868 | 0.773 |
| + contextual retrieval (Step 5) | 0.733 | 0.895 | 0.930 | 0.889 | 0.875 | 0.773 |
| + embedding ablation: BGE-m3 (Step 6) **←** | 0.733 | 0.919 | 0.954 | 0.901 | 0.889 | 0.773 |

## Persian (secondary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 |
|---|---|---|---|---|---|---|
| Baseline (e5 dense cosine, raw text) | 0.639 | 0.907 | 0.942 | 0.846 | 0.842 | 0.727 |
| + normalized text (Step 2) | 0.721 | 0.930 | 0.942 | 0.901 | 0.886 | 0.818 |
| + BM25 hybrid / RRF (Step 3) | 0.244 | 0.593 | 0.733 | 0.452 | 0.503 | 0.500 |
| + cross-encoder rerank (Step 4) | 0.733 | 0.919 | 0.942 | 0.915 | 0.885 | 0.773 |
| + contextual retrieval (Step 5) | 0.791 | 0.930 | 0.977 | 0.936 | 0.918 | 0.818 |
| + embedding ablation: BGE-m3 (Step 6) **←** | 0.791 | 0.919 | 0.965 | 0.936 | 0.912 | 0.773 |

## Stage 2 — Reasoning & Robustness (CRAG + selective query transformation)

All Stage 2 rows run on the Stage 1 winning configuration (hybrid RRF +
cross-encoder rerank, winning EMBED_MODEL). Unlike Stage 1, rows are not
cumulative: each transform row layers ONE strategy on the winner; `auto`
and `CRAG` are the selective compositions. Confidence labels use a sigmoid
over the cross-encoder logits (see services/retrieval_evaluator.py).
"LLM calls/100q" is the Stage 2 efficiency metric — selective routing must
keep most of the always-on recall gain at a fraction of this cost.

### English (primary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 | LLM calls/100q |
|---|---|---|---|---|---|---|---|
| Stage 1 winner + confidence evaluator (gating only) | — | — | — | — | — | — | — |
| + HyDE-always (Step 2) | — | — | — | — | — | — | — |
| + step-back-always (Step 3) | — | — | — | — | — | — | — |
| + multi-query-always, n=3 (Step 3) | — | — | — | — | — | — | — |
| + selective router, primary only (Step 4) | — | — | — | — | — | — | — |
| + CRAG corrective loop (Step 5) | — | — | — | — | — | — | — |

### Persian (secondary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 | LLM calls/100q |
|---|---|---|---|---|---|---|---|
| Stage 1 winner + confidence evaluator (gating only) | — | — | — | — | — | — | — |
| + HyDE-always (Step 2) | — | — | — | — | — | — | — |
| + step-back-always (Step 3) | — | — | — | — | — | — | — |
| + multi-query-always, n=3 (Step 3) | — | — | — | — | — | — | — |
| + selective router, primary only (Step 4) | — | — | — | — | — | — | — |
| + CRAG corrective loop (Step 5) | — | — | — | — | — | — | — |

**Winning configuration: + embedding ablation: BGE-m3 (Step 6)**
<!-- One-sentence note on the winner, e.g.: chosen on overall EN recall@5;
FA direction consistent; gaps under ~1 item (≈2.3pp) treated as ties. -->

## Notes

- `lexical` (BM25-only) is a diagnostic leg, not a pipeline stage; see
  `eval/results/lexical_*.json`.
- Step 5 also moved the lexical leg from raw to normalized+contextual text
  (lexeme regenerated over the stored passage); the isolated
  normalization-only lexical delta can be obtained by re-ingesting
  `mabhas_clauses_normalized.json` on the Step 5 schema if needed.
- Reranked runs record mean cross-encoder latency in
  `aggregates.rerank_latency_sec_mean`.

## Stage 3 — Graph Extension (regulation graph + exception expansion)

All Stage 3 rows run on top of earlier winners; the two rows isolate the
graph layer's contribution with and without the Stage 2 corrective loop
underneath. The graph layer is fully deterministic — "LLM calls/100q" must
be IDENTICAL to the row it wraps (graph_base = Stage 1 winner = 0;
graph = CRAG row) — that identity is itself a result. Graph artifact:
`data/regulation_graph.graphml` (393 nodes / 922 edges, commit-pinned via
`docs/regulation_graph_report.txt`). Exception ground-truth links:
`data/exception_links.json` — adjudicate the 4 pending entries BEFORE the
final runs so HAS_EXCEPTION ground truth is frozen during evaluation.

Run commands:

```bash
python -m rag.build_regulation_graph    # rebuild the pinned graph artifact
python -m eval.retrieval_eval --run-name stage3_graph_base --retriever graph_base --query-lang fa
python -m eval.retrieval_eval --run-name stage3_graph      --retriever graph      --query-lang fa
python -m eval.retrieval_eval --run-name stage3_graph_exc  --retriever graph      --query-lang fa --rule-type exception
```

### English (primary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 | exception recall@5 | LLM calls/100q |
|---|---|---|---|---|---|---|---|---|
| Stage 2 winner (CRAG) — carried for comparison | — | — | — | — | — | — | — | — |
| graph only: GraphRetriever over hybrid+rerank (`graph_base`) | — | — | — | — | — | — | — | 0 |
| graph + CRAG combined (`graph`) — production default | — | — | — | — | — | — | — | — |

### Persian (secondary)

| Configuration | recall@1 | recall@5 | recall@10 | MRR | nDCG@10 | multi-hop recall@5 | exception recall@5 | LLM calls/100q |
|---|---|---|---|---|---|---|---|---|
| Stage 2 winner (CRAG) — carried for comparison | — | — | — | — | — | — | — | — |
| graph only: GraphRetriever over hybrid+rerank (`graph_base`) | — | — | — | — | — | — | — | 0 |
| graph + CRAG combined (`graph`) — production default | — | — | — | — | — | — | — | — |

> n-caveat: the exception subset is small (n=6); report the absolute hit
> counts (x/6) next to the percentage and pair the table with the per-item
> case traces in `eval/results/PROVENANCE.md` rather than leaning on the
> percentage alone.

## FINAL — consolidated ablation (thesis backbone)

One row per stage, cumulative; each row = ONE git commit + ONE harness run
(run JSONs record commit, EMBED_MODEL, retriever_type, timestamp).
Primary language English; repeat the same three tables for Persian in the
thesis appendix if space allows.

### Overall

| Configuration | recall@5 | MRR | nDCG@10 | LLM calls/100q | run |
|---|---|---|---|---|---|
| Baseline (single-vector e5, normalized text) | — | — | — | 0 | `baseline_en.json` |
| + Stage 1 (hybrid RRF + rerank + contextual) | — | — | — | 0 | `contextual_en.json` |
| + Stage 2 (CRAG, selective transforms) | — | — | — | — | `stage2_crag_fa.json` |
| + Stage 3 (graph + exception expansion) | — | — | — | — | `stage3_graph_fa.json` |

### multi_hop subset

| Configuration | recall@5 | MRR | nDCG@10 |
|---|---|---|---|
| Baseline (single-vector e5, normalized text) | — | — | — |
| + Stage 1 (hybrid RRF + rerank + contextual) | — | — | — |
| + Stage 2 (CRAG, selective transforms) | — | — | — |
| + Stage 3 (graph + exception expansion) | — | — | — |

### exception subset (headline; report x/6 counts alongside)

| Configuration | recall@5 | MRR | nDCG@10 |
|---|---|---|---|
| Baseline (single-vector e5, normalized text) | — | — | — |
| + Stage 1 (hybrid RRF + rerank + contextual) | — | — | — |
| + Stage 2 (CRAG, selective transforms) | — | — | — |
| + Stage 3 (graph + exception expansion) | — | — | — |

**Stage 3 verdict invariant:** `pytest eval/test_verdict_regression.py`
passes on the same commit as the stage3 rows — retrieval changes never
moved a deterministic PASS/FAIL (advisory text on NEEDS_REVIEW may differ).

**Provenance figure:** `eval/results/PROVENANCE.md`
(`python -m scripts.dump_provenance --results eval/results/stage3_graph_fa.json`)
shows, per worked example, which final hits arrived via vector vs
graph_element vs exception_expansion.
