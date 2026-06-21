# Mabhas Compliance Engine

An AI-assisted system that checks Iranian residential building floor plans
against the National Building Regulations (مقررات ملی ساختمان / *Mabhas*). It
takes the `bim_data` produced by a 2D-to-3D floor-plan model, checks it against
the digitised Mabhas corpus, and produces a compliance report (HTML + PDF + BCF).

## Architecture in one line

```
bim_data → spatial graph → 4 deterministic agents → orchestrator → report
                                ↑                                      ↓
                    RAG over the Mabhas corpus                  human review queue
```

## Design principle: deterministic spine, AI on the wings

Every PASS/FAIL verdict on a numeric or spatial rule comes from **deterministic
Python**, never an LLM. The system is conservative by design: anything it cannot
verify from the plan is flagged `NEEDS_REVIEW` for a qualified professional,
never guessed. The LLM is used only for offline regulation classification,
optional advisory notes on ambiguous clauses, and report narrative — it can
never override a deterministic verdict. This keeps every verdict reproducible
and defensible.

## Repository layout

| Folder | Contents |
|---|---|
| `rag/` | retrieval + knowledge-graph layer: pgvector schema & ingestion, dense/lexical/hybrid retrieval, cross-encoder rerank, contextual retrieval, adaptive (CRAG) layer, the NetworkX regulation graph, and graph-aware retrieval. Colab notebook in `rag/notebooks/`. |
| `services/` | the deterministic engine: spatial graph, the four agents (numeric, topology, opening, safety), `graph_linker`, orchestrator, report generator, review queue. |
| `api/` | FastAPI + (optional) Celery async web service. |
| `classification/` | Mabhas Word document → structured clause JSON (prompt + script). |
| `scripts/` | provenance dump + element/clause coverage utilities. |
| `eval/` | retrieval-evaluation harness (hit@k, recall@k, MRR, nDCG@k) + retrieval unit tests + `eval/results/` (ablation outputs, `SUMMARY.md`, `PROVENANCE.md`). |
| `tests/` | engine + API tests (the deterministic spine). |
| `data/` | the 594-clause Mabhas corpus across pipeline stages, the 43-item retrieval eval set, a 5-clause sample, and the regulation graph (`regulation_graph.graphml`, 393 nodes / 922 edges). |
| `docs/` | per-stage write-ups (Stage 0–3), regulation-graph schema, coverage reports. |

### The Mabhas corpus (`data/`)

| File | What it is |
|---|---|
| `mabhas_clauses.json` | raw extracted clauses (594) |
| `mabhas_clauses_normalized.json` | Stage 0 — six-rule Persian text normalization (594) |
| `mabhas_clauses_contextual.json` | Stage 1 — LLM-generated `context_fa` prepended at index time (594) |
| `mabhas_eval_set.json` | 43-item retrieval evaluation set |
| `sample_mabhas_clauses.json` | 5-clause sample for quick smoke runs |

## Quick start

```bash
pip install -r requirements.txt        # runtime deps
pip install -r requirements-dev.txt    # + pytest/httpx for the test suite

# run the engine on a plan in Python (no database or API key needed)
python -c "
from services.orchestrator import run_compliance
from services.report_generator import generate_reports
import json
clauses = [c for c in json.load(open('data/mabhas_clauses.json')) if not c.get('skip_category')]
result = run_compliance(my_bim_data, clauses)
generate_reports(result.to_dict(), {'plan_name': 'Plan_01'}, out_dir='out/')
"
```

> The engine modules import one another by bare name (a flat-layout convention
> from the original codebase). `services/__init__.py` adds its own directory to
> `sys.path` on import so `from services.orchestrator import …` works from a
> plain checkout with no manual `PYTHONPATH`.

## Running the tests

```bash
pytest                  # discovers tests/ and eval/ (see pyproject.toml)
pytest tests/           # just the deterministic engine + API
```

`pyproject.toml` sets `pythonpath = [".", "services"]` so both the package-style
imports (`rag.*`, `services.*`) and the engine's bare intra-package imports
resolve without any environment setup. `conftest.py` pins `CRAG_ENABLED=0` and
`GRAPH_ENABLED=0` so unit tests exercise the deterministic Stage 1 retriever.

The deterministic-engine suite (all of `tests/`) and the pure retrieval-logic
tests run offline. Tests that need a live **PostgreSQL + pgvector** instance
(`tests/test_rag_smoke.py`) or downloaded embedding/reranker models are skipped
when those resources are absent.

> **Known stale tests.** `eval/test_query_transforms.py` and
> `eval/test_crag_retriever.py` were written against an earlier generation of
> those modules (anthropic-based transforms; Python post-filtering in CRAG). The
> shipped `rag/` modules are the newer canonical versions (Groq-based transforms
> via `groq_client`; SQL-level filtering, `top_k=3`, sigmoid score-transform in
> CRAG). These two test files need updating to the new behaviour — see
> `docs/` and the cleanup notes.

## Pipeline stages

| Stage | What it adds | Write-up |
|---|---|---|
| 0 | Persian text normalization (six rules) + 43-item eval set | `docs/STAGE0_INTEGRATION.md` |
| 1 | Hybrid retrieval (BM25 + dense, RRF), cross-encoder rerank, contextual retrieval, embedder ablation | `docs/STAGE1_RETRIEVAL.md` |
| 2 | Adaptive corrective retrieval (CRAG): confidence gate, HyDE / step-back / multi-query, rule-based router | `docs/STAGE2_REASONING.md` |
| 3 | Regulation knowledge graph (NetworkX, 393 nodes / 922 edges) + graph-aware retrieval | `docs/STAGE3_GRAPH.md` |

## Scope

Residential occupancy group **M-4** (1–2 household, max 3 storeys) plus rules
that apply to all residential buildings. Expanding to apartments (M-2) needs no
re-classification — just a wider ingest scope.
