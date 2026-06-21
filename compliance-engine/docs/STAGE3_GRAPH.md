# Stage 3 — Graph Extension (runbook)

Regulation knowledge graph + graph-aware retrieval. The thesis novelty layer:
deterministic, zero added LLM calls, wraps whatever the Stage 1/2 stack returns.

## What was built (one commit per step)

| Step | Commit | Artifact |
|---|---|---|
| 1 | `docs(stage3-step1)` | `rag/regulation_graph_schema.md` — node/edge schema, closed Element + Property vocabularies, unresolved-issue policies |
| 2 | `feat(stage3-step2)` | `rag/build_regulation_graph.py` → `data/regulation_graph.graphml` (393 nodes / 922 edges) + `docs/regulation_graph_report.txt` + `eval/test_regulation_graph.py` |
| 3 | `feat(stage3-step3)` | `services/graph_linker.py` (on-demand SpatialGraph↔RegulationGraph join) + `tests/test_graph_linker.py` + `docs/element_clause_coverage.txt` |
| 4 | `feat(stage3-step4)` | `rag/graph_retriever.py` (element extraction → seed → exception expansion → graph candidates → RRF → CE rerank, provenance-tagged) + `eval/test_graph_retriever.py` |
| 5 | `feat(stage3-step5)` | factory wiring (`GRAPH_ENABLED`, graph wraps CRAG), `eval/make_summary.py` templates, `eval/results/SUMMARY.md` + `PROVENANCE.md`, `scripts/dump_provenance.py` |

## Environment flags

| Var | Default | Meaning |
|---|---|---|
| `GRAPH_ENABLED` | `1` | factory wraps the retriever in `GraphRetriever` (graph wraps CRAG, never the reverse) |
| `CRAG_ENABLED` | `1` | Stage 2 corrective layer underneath the graph |
| `REGULATION_GRAPH` | `data/regulation_graph.graphml` | pinned graph artifact |
| `MABHAS_CLAUSES_FILE` | *(unset)* | clause corpus for hit reconstruction; when unset, resolves to `mabhas_clauses_contextual.json` if present, else falls back to `mabhas_clauses_normalized.json` with a warning |

The test suite pins `CRAG_ENABLED=0` / `GRAPH_ENABLED=0` via `conftest.py` so the
verdict-regression guard stays layer-independent.

## Known data caveat (action required before final runs)

`data/exception_links.json` has **4 UNRESOLVED entries** (exception clauses whose
base article is not cited in the text: `1-5-4-5-4`, `1-2-6-4`, `1-3-6-4`, `2-8-4b`).
The graph currently carries only 2 HAS_EXCEPTION edges out of 6 exception clauses.
Adjudicate these manually (fill `base_article_id`), re-run
`python -m rag.build_regulation_graph`, and only then run the stage3 eval rows —
HAS_EXCEPTION ground truth must be frozen during evaluation. This directly drives
the headline exception-recall@5 result.

## Note: the contextual corpus is generated, not committed

`data/mabhas_clauses_contextual.json` is produced locally by
`python -m rag.contextualize` (one-time API pass, Stage 1 / Step 5) and is not in
git. All graph-layer entry points fall back to the normalized corpus when it is
absent (graph structure is identical; only cross-encoder passages lose the
prepended `context_fa`). Regenerate it before the final eval runs so the stage3
numbers reflect the full Stage 1 winner.

## Final run sequence (after adjudication + contextualize)

```bash
python -m rag.build_regulation_graph
python -m eval.retrieval_eval --run-name stage3_graph_base --retriever graph_base --query-lang both
python -m eval.retrieval_eval --run-name stage3_graph      --retriever graph      --query-lang both
python -m scripts.dump_provenance --results eval/results/stage3_graph_en.json
pytest eval/test_verdict_regression.py   # must pass on the same commit
```

Then fill the Stage 3 + FINAL tables in `eval/results/SUMMARY.md`.
