"""
eval/make_summary.py
====================
Generate eval/results/SUMMARY.md — the consolidated Stage 1 ablation table —
from the per-run result JSONs written by eval/retrieval_eval.py.

Usage:
    python -m eval.make_summary                  # uses default run names
    python -m eval.make_summary --winner bge_m3  # mark a specific winner

Missing run files render as "—" cells, so the template is usable before
all runs exist. The winner row gets a "←" marker; by default the winner is
the run with the highest primary-language overall recall@5.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[1]
RESULTS = _ROOT / "eval" / "results"

PRIMARY_LANG = "en"   # production path: the LLM interpretive pass queries in
SECONDARY_LANG = "fa"  # English (orchestrator retrieves with rule_text EN);
                       # FA reported for the Persian-facing UI path.

ROWS = [
    ("baseline",   "Baseline (e5 dense cosine, raw text)"),
    ("normalized", "+ normalized text (Step 2)"),
    ("hybrid",     "+ BM25 hybrid / RRF (Step 3)"),
    ("reranked",   "+ cross-encoder rerank (Step 4)"),
    ("contextual", "+ contextual retrieval (Step 5)"),
    ("bge_m3",     "+ embedding ablation: BGE-m3 (Step 6)"),
]
# the lexical-only run is a diagnostic leg, not a pipeline configuration;
# it is reported in the appendix, not in the headline ablation.

METRICS = ["recall@1", "recall@5", "recall@10", "mrr", "ndcg@10"]

# ---------------------------------------------------------------------------
# Stage 2 — Reasoning & Robustness (CRAG). Run names match the runbook in
# STAGE2_REASONING.md. Rows are NOT cumulative like Stage 1: each transform
# row layers exactly one strategy on the Stage 1 winner; auto and crag are
# the selective compositions. The extra column is the Stage 2 efficiency
# metric (LLM calls per 100 queries, from payload["stage2"]["llm_cost"]).
# ---------------------------------------------------------------------------
STAGE2_ROWS = [
    ("s2_gating",     "Stage 1 winner + confidence evaluator (gating only)"),
    ("s2_hyde",       "+ HyDE-always (Step 2)"),
    ("s2_stepback",   "+ step-back-always (Step 3)"),
    ("s2_multiquery", "+ multi-query-always, n=3 (Step 3)"),
    ("s2_auto",       "+ selective router, primary only (Step 4)"),
    ("s2_crag",       "+ CRAG corrective loop (Step 5)"),
]


def _load(run: str, lang: str) -> Optional[Dict[str, Any]]:
    p = RESULTS / f"{run}_{lang}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _cell(agg: Optional[Dict[str, Any]], path: list) -> str:
    if agg is None:
        return "—"
    node: Any = agg
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return "—"
    return f"{node:.3f}"


def build_table(lang: str, winner: Optional[str]) -> str:
    header = ("| Configuration | recall@1 | recall@5 | recall@10 | MRR "
              "| nDCG@10 | multi-hop recall@5 |")
    sep = "|" + "---|" * 7
    lines = [header, sep]
    for run, label in ROWS:
        data = _load(run, lang)
        agg = data["aggregates"] if data else None
        cells = [_cell(agg, ["overall", m]) for m in METRICS]
        cells.append(_cell(agg, ["by_hop_type", "multi_hop", "recall@5"]))
        mark = " **←**" if run == winner else ""
        lines.append(f"| {label}{mark} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def autodetect_winner() -> Optional[str]:
    best, best_val = None, -1.0
    for run, _ in ROWS:
        data = _load(run, PRIMARY_LANG)
        if not data:
            continue
        val = data["aggregates"]["overall"].get("recall@5", -1.0)
        if val > best_val:
            best, best_val = run, val
    return best


def _llm_cell(data: Optional[Dict[str, Any]]) -> str:
    """LLM calls per 100 queries; '0' for runs without an LLM in the loop."""
    if data is None:
        return "—"
    cost = (data.get("stage2") or {}).get("llm_cost")
    if not cost:
        return "0"
    return f"{cost.get('llm_calls_per_100_queries', 0):g}"


def build_stage2_table(lang: str) -> str:
    header = ("| Configuration | recall@1 | recall@5 | recall@10 | MRR "
              "| nDCG@10 | multi-hop recall@5 | LLM calls/100q |")
    sep = "|" + "---|" * 8
    lines = [header, sep]
    for run, label in STAGE2_ROWS:
        data = _load(run, lang)
        agg = data["aggregates"] if data else None
        cells = [_cell(agg, ["overall", m]) for m in METRICS]
        cells.append(_cell(agg, ["by_hop_type", "multi_hop", "recall@5"]))
        cells.append(_llm_cell(data))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--winner", default=None,
                    help="run name to mark as winner (default: best EN recall@5)")
    args = ap.parse_args(argv)

    winner = args.winner or autodetect_winner()
    winner_label = dict(ROWS).get(winner, winner or "TBD")

    md = f"""# Stage 1 — Retrieval Ablation Summary

Eval set: `data/mabhas_retrieval_eval.json` — 43 items (32 zero-hop, 11
multi-hop; 28 numeric / 6 spatial / 6 exception / 3 definition), gold =
`source_article_ids`, corpus = 328 ingestable Mabhas clauses.

Each row changes exactly ONE variable relative to the row above
(cumulative pipeline; per-run config, git commit and timestamp are recorded
in the corresponding `eval/results/<run>_<lang>.json`).

**Primary query language: English.** The production retrieval consumer is
the LLM interpretive pass, which queries with the clause's English rule
text (`validation/compliance/runner.py`, `retriever.retrieve(rule_text[:120])`);
the deterministic agents do not query the retriever at all. Persian is
reported as the secondary language for the Persian-facing UI path.

## English (primary)

{build_table('en', winner)}

## Persian (secondary)

{build_table('fa', winner)}

## Stage 2 — Reasoning & Robustness (CRAG + selective query transformation)

All Stage 2 rows run on the Stage 1 winning configuration (hybrid RRF +
cross-encoder rerank, winning EMBED_MODEL). Unlike Stage 1, rows are not
cumulative: each transform row layers ONE strategy on the winner; `auto`
and `CRAG` are the selective compositions. Confidence labels use a sigmoid
over the cross-encoder logits (see services/retrieval_evaluator.py).
"LLM calls/100q" is the Stage 2 efficiency metric — selective routing must
keep most of the always-on recall gain at a fraction of this cost.

### English (primary)

{build_stage2_table('en')}

### Persian (secondary)

{build_stage2_table('fa')}

**Winning configuration: {winner_label}**
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
"""
    out = RESULTS / "SUMMARY.md"
    out.write_text(md, encoding="utf-8")
    print(f"written -> {out.relative_to(_ROOT)} (winner: {winner_label})")


if __name__ == "__main__":
    main()
