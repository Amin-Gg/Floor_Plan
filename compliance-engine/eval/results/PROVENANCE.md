# Retrieval Provenance — Worked Examples (Stage 3, Step 5)

> TEMPLATE — regenerate with real traces after the stage3 runs:
>
>     python -m eval.retrieval_eval --run-name stage3_graph --retriever graph --query-lang both
>     python -m scripts.dump_provenance --results eval/results/stage3_graph_en.json
>
> The script auto-picks one multi_hop and one exception item where the
> graph layer contributed (override with --item-ids). Each example renders
> the sections below from the per-item record — no manual editing needed.

Run: `<run_name>` | retriever: `graph_crag_rrf_ce` | commit: `<git_commit>`

### Item `<item_id>` — <hop_type>, rule_type=<rule_type>, lang=<lang>

**Query:** <question>

**Gold clause(s):** <gold_ids>

**Elements detected (rule-based, no LLM):** <elements>

**A — Vector seed** (wrapped Stage 1/2 stack, in seed order):
1. `<article_id>` ← gold markers added automatically

**B — Exception expansion** (HAS_EXCEPTION children of the seed):
- `<article_id>` — the hits vector retrieval structurally cannot reach

**C — Graph element candidates** (GOVERNS edges of detected elements, degree-ranked):
- `<article_id>` …

**D — Fused candidate pool** (RRF over A/B/C → cross-encoder): `<ids>`

**E — Final ranking** (cross-encoder vs the original query):

| rank | article_id | CE score | provenance | gold |
|---|---|---|---|---|
| 1 | `<id>` | <score> | vector \| graph_element \| exception_expansion | ✔ |

**recall@5 for this item:** <value>

**Final-hit provenance histogram:** vector=<n>, graph_element=<n>,
exception_expansion=<n> (LLM calls added by the graph layer: 0)
