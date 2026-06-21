"""
scripts/dump_provenance.py
==========================
Stage 3, Step 5 — render eval/results/PROVENANCE.md from a graph-retriever
run's per-item records (thesis figure material).

For each requested item (default: the first multi_hop item and the first
exception item where the graph layer contributed), formats the full
retrieval trace recorded by GraphRetriever.last_graph_trace and the
harness: seed hits, graph-added candidates, exception expansions, fused
pool, and final cross-encoder ranking with provenance tags.

Usage:
    python -m scripts.dump_provenance \
        --results eval/results/stage3_graph_en.json \
        [--item-ids MH-017 EXC-003] \
        [--out eval/results/PROVENANCE.md]

Requires a run made with `--retriever graph` (or graph_base): only those
records carry the "graph" trace and per-hit "provenance".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _pick_default_items(per_item: List[Dict[str, Any]]) -> List[str]:
    """First multi_hop and first exception item where the graph layer
    actually added something (fallback: first of each subset)."""
    picked: List[str] = []
    for subset_key, subset_val in (("hop_type", "multi_hop"),
                                   ("rule_type", "exception")):
        candidates = [r for r in per_item if r.get(subset_key) == subset_val
                      and r.get("graph")]
        contributing = [
            r for r in candidates
            if r["graph"].get("exception_added_n", 0) > 0
            or r["graph"].get("graph_candidates_n", 0) > 0
        ]
        chosen = (contributing or candidates)
        if chosen:
            picked.append(chosen[0]["item_id"])
    return picked


def _gold_mark(article_id: str, gold: set) -> str:
    return " **← gold**" if article_id in gold else ""


def _render_item(record: Dict[str, Any]) -> str:
    g = record.get("graph") or {}
    gold = set(record.get("gold_ids") or [])
    lines: List[str] = []
    add = lines.append

    add(f"### Item `{record['item_id']}` — {record.get('hop_type')}, "
        f"rule_type={record.get('rule_type')}, lang={record.get('query_lang')}")
    add("")
    add(f"**Query:** {record.get('question')}")
    add("")
    add(f"**Gold clause(s):** {', '.join(sorted(gold)) or '—'}")
    add("")
    add(f"**Elements detected (rule-based, no LLM):** "
        f"{', '.join(g.get('elements_detected') or []) or 'none'}")
    add("")

    add("**A — Vector seed** (wrapped Stage 1/2 stack, in seed order):")
    add("")
    for i, aid in enumerate(g.get("seed_ids") or [], 1):
        add(f"{i}. `{aid}`{_gold_mark(aid, gold)}")
    add("")

    exc = g.get("exception_added_ids") or []
    add(f"**B — Exception expansion** (HAS_EXCEPTION children of the seed; "
        f"{len(exc)} added):")
    add("")
    if exc:
        for aid in exc:
            add(f"- `{aid}`{_gold_mark(aid, gold)}")
    else:
        add("- *(none — no seed clause carries a HAS_EXCEPTION edge)*")
    add("")

    gids = g.get("graph_candidate_ids") or []
    add(f"**C — Graph element candidates** (GOVERNS edges of detected "
        f"elements, degree-ranked; {len(gids)}):")
    add("")
    if gids:
        shown = gids[:15]
        for aid in shown:
            add(f"- `{aid}`{_gold_mark(aid, gold)}")
        if len(gids) > len(shown):
            add(f"- *(… {len(gids) - len(shown)} more)*")
    else:
        add("- *(none — query names no vocabulary element)*")
    add("")

    add(f"**D — Fused candidate pool** (RRF over A/B/C, "
        f"{g.get('fused_pool_n', '—')} candidates -> cross-encoder):")
    add("")
    add(f"`{', '.join(g.get('fused_pool_ids') or [])}`")
    add("")

    add("**E — Final ranking** (cross-encoder vs the original query):")
    add("")
    add("| rank | article_id | CE score | provenance | gold |")
    add("|---|---|---|---|---|")
    prov = record.get("provenance") or []
    scores = record.get("retrieved_scores") or []
    for i, aid in enumerate(record.get("retrieved_ids") or []):
        score = scores[i] if i < len(scores) else "—"
        tag = prov[i] if i < len(prov) else "—"
        add(f"| {i + 1} | `{aid}` | {score} | {tag} | "
            f"{'✔' if aid in gold else ''} |")
    add("")

    m = record.get("metrics") or {}
    r5 = m.get("recall@5", m.get("recall", {}).get("5", "—")) \
        if isinstance(m, dict) else "—"
    add(f"**recall@5 for this item:** {r5}")
    add("")
    fp = g.get("final_provenance") or {}
    add(f"**Final-hit provenance histogram:** vector={fp.get('vector', 0)}, "
        f"graph_element={fp.get('graph_element', 0)}, "
        f"exception_expansion={fp.get('exception_expansion', 0)} "
        f"(LLM calls added by the graph layer: "
        f"{g.get('llm_calls_added', 0)})")
    add("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", required=True,
                   help="A stage3 graph run JSON (eval/results/<run>_<lang>.json)")
    p.add_argument("--item-ids", nargs="*", default=None,
                   help="Explicit item_ids; default picks one multi_hop "
                        "and one exception item automatically")
    p.add_argument("--out", default="eval/results/PROVENANCE.md")
    args = p.parse_args()

    blob = json.loads(Path(args.results).read_text(encoding="utf-8"))
    per_item: List[Dict[str, Any]] = blob.get("per_item") or blob.get("items") or []
    if not per_item:
        raise SystemExit("No per-item records found in the results file.")
    if not any(r.get("graph") for r in per_item):
        raise SystemExit("Records carry no 'graph' trace — rerun the eval "
                         "with --retriever graph (or graph_base).")

    wanted = args.item_ids or _pick_default_items(per_item)
    by_id = {r["item_id"]: r for r in per_item}
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise SystemExit(f"item_ids not in results: {missing}")

    run_cfg = blob.get("run_config", {})
    header = [
        "# Retrieval Provenance — Worked Examples (Stage 3, Step 5)",
        "",
        f"Run: `{run_cfg.get('run_name', Path(args.results).stem)}` | "
        f"retriever: `{run_cfg.get('retriever_type', '—')}` | "
        f"commit: `{run_cfg.get('git_commit', '—')}`",
        "",
        "Each example shows the full graph-aware retrieval trace: the vector",
        "seed (A), deterministic HAS_EXCEPTION expansion (B), element-governed",
        "graph candidates (C), the RRF-fused pool (D), and the cross-encoder",
        "final ranking with per-hit provenance (E). The graph layer adds zero",
        "LLM calls; every non-vector hit below was reachable only through a",
        "typed regulation-graph edge.",
        "",
    ]
    body = [_render_item(by_id[i]) for i in wanted]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header) + "\n" + "\n---\n\n".join(body),
                   encoding="utf-8")
    print(f"wrote {out} ({len(wanted)} worked example(s): {', '.join(wanted)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
