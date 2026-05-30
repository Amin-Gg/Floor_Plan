# Mabhas Compliance Engine

An AI-assisted system that checks Iranian residential building floor plans
against the National Building Regulations (مقررات ملی ساختمان / Mabhas). It
takes the `bim_data` produced by a 2D-to-3D floor-plan model, checks it against
the digitised Mabhas corpus, and produces a compliance report (HTML + PDF + BCF).

## Architecture in one line

```
bim_data → spatial graph → 4 deterministic agents → orchestrator → report
                                ↑                                      ↓
                       RAG index (Mabhas)                      human review queue
```

## Design principle: deterministic spine, AI on the wings

Every PASS/FAIL verdict on a numeric or spatial rule comes from **deterministic
Python**, never an LLM. The system is conservative by design: anything it cannot
verify from the plan is flagged `NEEDS_REVIEW` for a qualified professional, never
guessed. The LLM is used only for offline regulation classification, optional
advisory notes on ambiguous clauses, and report narrative — it can never
override a deterministic verdict. This keeps every verdict reproducible and
defensible.

## Repository layout

| Folder | Contents | Roadmap step |
|---|---|---|
| `classification/` | Mabhas Word → structured JSON (prompt + script) | 1 |
| `rag/` | pgvector schema, embeddings, ingestion, retriever, Colab notebook | 2 |
| `services/` | the engine: spatial graph, 4 agents, orchestrator, report, review queue | 3, 5–9, 11, 12 |
| `api/` | FastAPI + Celery async web service | 10 |
| `tests/` | 62 tests across all modules | all |
| `data/` | the 594-clause Mabhas corpus + sample | — |
| `sample_output/` | a real generated report (HTML/PDF/BCF) | — |
| `docs/` | per-step commit notes | — |

## Quick start

```bash
pip install -r requirements.txt

# run the whole test suite (no database or API key needed)
python -m pytest tests/  -q          # or run each tests/test_*.py directly

# run the engine on a plan in Python
python -c "
from services.orchestrator import run_compliance
from services.report_generator import generate_reports
import json
clauses = [c for c in json.load(open('data/mabhas_clauses.json')) if not c.get('skip_category')]
result = run_compliance(my_bim_data, clauses)
generate_reports(result.to_dict(), {'plan_name':'Plan_01'}, out_dir='out/')
"
```

## Status

| Phase | Steps | State |
|---|---|---|
| Data foundation | 1, 2, 3 | done — RAG index live (324 clauses) |
| Compliance engine | 5, 6, 7, 8, 9 | done — 62 tests pass |
| Delivery + infra | 10, 11, 12 | done |
| GPU-dependent | 3-validation, 4 (IFC enrich), frontend, benchmarking | pending model training |

## Scope

Residential occupancy group **M-4** (1–2 household, max 3 storeys) plus rules
that apply to all residential buildings. Expanding to apartments (M-2) needs no
re-classification — just a wider ingest scope.
