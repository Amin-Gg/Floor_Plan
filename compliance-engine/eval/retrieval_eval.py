"""
eval/retrieval_eval.py — Stage 1 + 2 + 3 evaluation harness.

Stages 1 and 2 flags are preserved verbatim and fully backward-compatible.
Stage 3 adds graph-aware retrieval modes (marked with # ── STAGE 3 ──).

Usage — Stage 1/2 (unchanged):
    python -m eval.retrieval_eval --retriever hybrid --rerank --query-lang fa
    python -m eval.retrieval_eval --crag --query-lang en
    python -m eval.retrieval_eval --transform hyde --hop-type multi_hop

Usage — Stage 3 (new):
    # Build the graph artifact first (once):
    python -m rag.build_regulation_graph

    # Graph over the full CRAG stack (production default):
    python -m eval.retrieval_eval --retriever graph --query-lang fa \
        --run-name stage3_graph

    # Graph over hybrid+rerank only — isolates graph contribution without CRAG:
    python -m eval.retrieval_eval --retriever graph_base --query-lang fa \
        --run-name stage3_graph_base

    # Exception subset only (the Stage 3 headline metric):
    python -m eval.retrieval_eval --retriever graph --query-lang fa \
        --rule-type exception --run-name stage3_graph_exception

    # Render worked examples (requires a --retriever graph run):
    python -m scripts.dump_provenance \
        --results eval/results/stage3_graph_fa.json

New Stage 3 flags:
    --retriever graph       GraphRetriever over CorrectiveRetriever(base)
    --retriever graph_base  GraphRetriever over hybrid+rerank only (no CRAG)
    --run-name NAME         Output file prefix in eval/results/ (auto if omitted)
    --graph-path PATH       GraphML artifact (default: data/regulation_graph.graphml)
    --clauses-file PATH     Corpus JSON (default: data/mabhas_clauses_contextual.json)
    --rule-type {all,numeric,spatial,definition,exception}
                            Subset filter (use "exception" for the headline metric)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import datetime
from typing import Any, Dict, List

from rag.retrieval_evaluator import evaluate_retrieval, DEFAULT_THRESHOLDS
from rag.query_transforms import reset_llm_counters, llm_counters
from rag.query_router import route_query
from rag.crag_retriever import CorrectiveRetriever

RESULTS_DIR = "eval/results"


# ===========================================================================
# Retriever construction  (>>> ADAPTER POINT <<<)
# ===========================================================================

def build_base_retriever():
    """Constructs the bare MabhasRetriever using the Stage 1 winning config.

    All configuration is read from the environment:
      DATABASE_URL  — Neon/PostgreSQL connection string
      EMBED_MODEL   — path to multilingual-e5-large (rag/embeddings.py
                      reads this at import time; must match the index)

    Winning Stage 1 config: hybrid + bge-m3 cross-encoder rerank.
    Run with: --retriever hybrid --rerank --query-lang fa
    Baseline numbers (SUMMARY.md):
      hit@1=0.907  hit@5=0.977  recall@5=0.919  mrr=0.936
    """
    from rag.rag_retriever import MabhasRetriever
    return MabhasRetriever()


# ── STAGE 3 ─────────────────────────────────────────────────────────────────

class _HybridRetrieveAdapter:
    """Adapts MabhasRetriever.hybrid_retrieve() to the .retrieve() contract
    that GraphRetriever expects.  Used only by --retriever graph_base.

    For the graph-without-CRAG ablation row: GraphRetriever seeds from the
    Stage 1 winner (hybrid+rerank), adding zero corrective LLM calls.
    """

    def __init__(self, base):
        self.base = base
        self.last_trace = None
        self.last_rerank_seconds = None

    def retrieve(self, query: str, top_k: int = 3,
                 mabhas_part=None, rule_type=None):
        return self.base.hybrid_retrieve(
            query, top_k=top_k, rerank=True,
            mabhas_part=mabhas_part, rule_type=rule_type)


def build_graph_retriever(args, base):
    """Build GraphRetriever for --retriever graph or graph_base.

    graph:      GraphRetriever wraps the full CRAG stack.
                Use this row's delta vs --crag to measure the graph layer.
    graph_base: GraphRetriever wraps hybrid+rerank only (no CRAG).
                Use this row's delta vs --rerank to measure graph alone.

    Returns: (GraphRetriever instance, retriever_type_label string)
    """
    from services.graph_linker import GraphLinker
    from rag.graph_retriever import GraphRetriever, load_clauses_by_id

    linker = GraphLinker(regulation_graph_path=args.graph_path)
    clauses_by_id = load_clauses_by_id(args.clauses_file)

    if args.retriever == "graph":
        inner = CorrectiveRetriever(base)
        retriever_type = "graph_crag_rrf_ce"
    else:                                   # graph_base
        inner = _HybridRetrieveAdapter(base)
        retriever_type = "graph_rrf_ce"

    gr = GraphRetriever(base=inner, linker=linker, clauses_by_id=clauses_by_id)
    return gr, retriever_type


# ── END STAGE 3 ──────────────────────────────────────────────────────────────


# ===========================================================================
# Metrics (standard definitions; binary relevance against source_article_ids)
# ===========================================================================

def hit_at_k(retrieved_ids: List[str], gold_ids: List[str], k: int = 5) -> float:
    topk = retrieved_ids[:k]
    return 1.0 if any(g in topk for g in gold_ids) else 0.0


def recall_at_k(retrieved_ids: List[str], gold_ids: List[str], k: int = 5) -> float:
    if not gold_ids:
        return 0.0
    topk = set(retrieved_ids[:k])
    return sum(1 for g in gold_ids if g in topk) / len(gold_ids)


def mrr(retrieved_ids: List[str], gold_ids: List[str]) -> float:
    gold = set(gold_ids)
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: List[str], gold_ids: List[str], k: int = 5) -> float:
    import math
    gold = set(gold_ids)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, rid in enumerate(retrieved_ids[:k], start=1)
        if rid in gold
    )
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(r + 1) for r in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


# ===========================================================================
# Argument parsing
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mabhas retrieval evaluation")
    # ── STAGE 3: graph and graph_base added to choices ───────────────────────
    parser.add_argument("--retriever", default="hybrid",
                        choices=["hybrid", "dense", "lexical",
                                 "graph", "graph_base"])
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--query-lang", default="fa", choices=["fa", "en"])
    parser.add_argument("--eval-set", default="data/mabhas_eval_set.json")
    parser.add_argument("--top-k", type=int, default=5)
    # --- Stage 2, Step 1 ---
    parser.add_argument(
        "--log-confidence", action="store_true",
        help="Stage 2 confidence evaluator per item → "
             "eval/results/stage2_step1_confidence.json.")
    # --- Stage 2, Steps 2-4 ---
    parser.add_argument(
        "--transform", default="none",
        choices=["none", "hyde", "stepback", "multi_query", "auto"],
        help="Query transformation before retrieval (Stage 2 Steps 2-4).")
    # --- Stage 2, Step 3 ---
    parser.add_argument(
        "--hop-type", default="all",
        choices=["all", "zero_hop", "multi_hop"],
        help="Restrict to a hop_type subset of the eval set.")
    # --- Stage 2, Step 5 ---
    parser.add_argument(
        "--crag", action="store_true",
        help="Full Stage 2 CRAG loop. Mutually exclusive with --transform.")
    # ── STAGE 3: new flags ───────────────────────────────────────────────────
    parser.add_argument(
        "--run-name", default=None,
        help="Result file prefix in eval/results/ (auto from mode+timestamp "
             "when omitted; e.g. stage3_graph_20260618_1400).")
    parser.add_argument(
        "--graph-path", default="data/regulation_graph.graphml",
        help="GraphML artifact from rag/build_regulation_graph.py.")
    parser.add_argument(
        "--clauses-file",
        default="data/mabhas_clauses_contextual.json",
        help="Corpus JSON for clauses_by_id lookup. "
             "Falls back to mabhas_clauses_normalized.json when not found.")
    parser.add_argument(
        "--rule-type", default="all",
        choices=["all", "numeric", "spatial", "definition", "exception"],
        help="Restrict to a rule_type subset. "
             "Use 'exception' for the Stage 3 headline metric (n=6).")
    return parser


# ===========================================================================
# Report writers
# ===========================================================================

def write_confidence_report(records, thresholds, total_items, out_path):
    """records: list of (confidence_label, hit5, recall5)."""
    by_conf = {}
    for label in ("HIGH", "MEDIUM", "LOW"):
        bucket = [(h, r) for (lab, h, r) in records if lab == label]
        count = len(bucket)
        by_conf[label] = {
            "count": count,
            "mean_hit@5": (statistics.mean(h for h, _ in bucket) if count else 0.0),
            "mean_recall@5": (statistics.mean(r for _, r in bucket) if count else 0.0),
        }
    report = {"thresholds": thresholds, "total_items": total_items,
              "by_confidence": by_conf}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[confidence] wrote {out_path}")
    for label in ("LOW", "MEDIUM", "HIGH"):
        b = by_conf[label]
        print(f"  {label:<6} n={b['count']:<4} "
              f"hit@5={b['mean_hit@5']:.3f} recall@5={b['mean_recall@5']:.3f}")


# ── STAGE 3 ─────────────────────────────────────────────────────────────────

def _mean_or_none(values):
    return round(statistics.mean(values), 4) if values else None


def _agg(records: List[Dict]) -> Dict:
    if not records:
        return {}
    return {
        "n":             len(records),
        "mean_hit@5":    _mean_or_none([r["metrics"]["hit@5"]    for r in records]),
        "mean_recall@5": _mean_or_none([r["metrics"]["recall@5"] for r in records]),
        "mean_mrr":      _mean_or_none([r["metrics"]["mrr"]      for r in records]),
        "mean_ndcg@5":   _mean_or_none([r["metrics"]["ndcg@5"]   for r in records]),
    }


def write_graph_report(per_item_records: List[Dict],
                       retriever_type: str,
                       run_name: str,
                       lang: str,
                       args: Any) -> None:
    """Write eval/results/{run_name}_{lang}.json for dump_provenance.py,
    and print a compact aggregate broken down by rule_type.
    """
    overall = _agg(per_item_records)
    by_rule_type = {rt: _agg([r for r in per_item_records
                               if r["rule_type"] == rt])
                    for rt in ("numeric", "spatial", "definition", "exception")}

    prov_totals: Dict[str, int] = {}
    llm_added = 0
    for r in per_item_records:
        g = r.get("graph") or {}
        for k, v in (g.get("final_provenance") or {}).items():
            prov_totals[k] = prov_totals.get(k, 0) + v
        llm_added += g.get("llm_calls_added", 0)

    blob = {
        "run_config": {
            "run_name":         run_name,
            "retriever_type":   retriever_type,
            "query_lang":       lang,
            "eval_set":         args.eval_set,
            "top_k":            args.top_k,
            "hop_type":         args.hop_type,
            "rule_type_filter": args.rule_type,
            "graph_path":       args.graph_path,
            "clauses_file":     args.clauses_file,
            "timestamp":        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
        "aggregates": {
            "overall":       overall,
            "by_rule_type":  by_rule_type,
            "provenance_totals": prov_totals,
            "llm_calls_added_by_graph_layer": llm_added,
        },
        "per_item": per_item_records,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = f"{RESULTS_DIR}/{run_name}_{lang}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False, indent=2)
    print(f"[graph] wrote {out_path}")

    # — console summary -------------------------------------------------------
    print(f"\n[graph aggregate] n={overall.get('n')}  "
          f"retriever={retriever_type}  lang={lang}")
    for k, v in overall.items():
        if k != "n":
            print(f"  {k}: {v:.4f}" if v is not None else f"  {k}: n/a")
    exc = by_rule_type.get("exception", {})
    if exc.get("n"):
        print(f"  [exception subset n={exc['n']}]  "
              f"recall@5={exc.get('mean_recall@5'):.4f}  "
              f"hit@5={exc.get('mean_hit@5'):.4f}  "
              f"(report x/{exc['n']} absolute counts alongside)")
    mh_items = [r for r in per_item_records if r["hop_type"] == "multi_hop"]
    if mh_items:
        mh_r5 = statistics.mean(r["metrics"]["recall@5"] for r in mh_items)
        print(f"  [multi_hop subset n={len(mh_items)}]  recall@5={mh_r5:.4f}")
    print(f"  provenance totals: {prov_totals}")
    print(f"  LLM calls added by graph layer: {llm_added}  "
          f"(must be 0 — deterministic spine invariant)")
    print(f"  worked examples: "
          f"python -m scripts.dump_provenance --results {out_path}")


# ── END STAGE 3 ──────────────────────────────────────────────────────────────


# ===========================================================================
# Main
# ===========================================================================

def main():
    args = build_arg_parser().parse_args()

    # --- validation ----------------------------------------------------------
    if args.crag and args.transform != "none":
        raise SystemExit("--crag and --transform are mutually exclusive")

    # ── STAGE 3 validation ───────────────────────────────────────────────────
    if args.retriever in ("graph", "graph_base"):
        if args.crag:
            raise SystemExit(
                "--retriever graph already wraps CRAG; do not add --crag")
        if args.transform != "none":
            raise SystemExit(
                "--retriever graph already wraps the full Stage 2 stack; "
                "do not add --transform")
        # resolve clauses-file fallback
        if not os.path.exists(args.clauses_file):
            fallback = "data/mabhas_clauses_normalized.json"
            if os.path.exists(fallback):
                print(f"[graph] {args.clauses_file} not found; "
                      f"using {fallback}")
                args.clauses_file = fallback
            else:
                raise SystemExit(
                    f"clauses file not found: {args.clauses_file}\n"
                    f"Build the graph first: "
                    f"python -m rag.build_regulation_graph")
        if not os.path.exists(args.graph_path):
            raise SystemExit(
                f"Regulation graph not found: {args.graph_path}\n"
                f"Build it: python -m rag.build_regulation_graph")
    # ── END STAGE 3 validation ────────────────────────────────────────────────

    retriever = build_base_retriever()
    crag = CorrectiveRetriever(retriever) if args.crag else None

    # ── STAGE 3: build graph retriever ───────────────────────────────────────
    graph_retriever = None
    retriever_type_label = args.retriever
    run_name = args.run_name
    if args.retriever in ("graph", "graph_base"):
        graph_retriever, retriever_type_label = build_graph_retriever(
            args, retriever)
        if run_name is None:
            tag = "graph" if args.retriever == "graph" else "graph_base"
            run_name = (f"stage3_{tag}_"
                        f"{datetime.utcnow().strftime('%Y%m%d_%H%M')}")
    # ── END STAGE 3 setup ─────────────────────────────────────────────────────

    with open(args.eval_set, encoding="utf-8") as f:
        dataset = json.load(f)

    if args.hop_type != "all":
        dataset = [it for it in dataset if it["hop_type"] == args.hop_type]
        print(f"[subset] hop_type={args.hop_type}: {len(dataset)} items")

    # ── STAGE 3: rule-type filter ─────────────────────────────────────────────
    if args.rule_type != "all":
        dataset = [it for it in dataset if it.get("rule_type") == args.rule_type]
        print(f"[subset] rule_type={args.rule_type}: {len(dataset)} items")
    # ── END STAGE 3 filter ────────────────────────────────────────────────────

    # --- accumulators --------------------------------------------------------
    metrics = {"hit@5": [], "recall@5": [], "mrr": [], "ndcg@5": []}
    confidence_records = []
    crag_records = []
    graph_records: List[Dict] = []     # ── STAGE 3 ──
    rule_counts = {}
    transformed_items = 0
    top5_changed_auto = 0
    item_latencies = []
    reset_llm_counters()
    transform_wall_start = time.monotonic()

    for item in dataset:
        query = (item["question_fa"] if args.query_lang == "fa"
                 else item["question_en"])
        item_start = time.monotonic()
        trace = None

        # ── STAGE 3: graph path (checked first) ───────────────────────────────
        if graph_retriever is not None:
            hits = graph_retriever.retrieve(query, top_k=args.top_k)

        # ── END STAGE 3 path — original Stage 1/2 branches follow ────────────
        elif args.crag:
            hits = crag.retrieve(query, top_k=args.top_k)
            trace = dict(crag.last_trace)
        elif args.transform == "hyde":
            hits = retriever.hyde_retrieve(
                query, top_k=args.top_k, rerank=args.rerank,
                language=args.query_lang)
        elif args.transform == "stepback":
            hits = retriever.stepback_retrieve(
                query, top_k=args.top_k, rerank=args.rerank,
                language=args.query_lang)
        elif args.transform == "multi_query":
            hits = retriever.multi_query_retrieve(
                query, n=3, top_k=args.top_k, rerank=args.rerank,
                language=args.query_lang)
        elif args.transform == "auto":
            base_hits = retriever.hybrid_retrieve(
                query, top_k=args.top_k, rerank=args.rerank)
            decision = route_query(query, initial_hits=base_hits)
            rule_counts[decision["rule"]] = rule_counts.get(decision["rule"], 0) + 1
            if decision["primary"] == "none":
                hits = base_hits
            else:
                method = {
                    "hyde": retriever.hyde_retrieve,
                    "stepback": retriever.stepback_retrieve,
                    "multi_query": retriever.multi_query_retrieve,
                }[decision["primary"]]
                hits = method(query, top_k=args.top_k, rerank=args.rerank,
                              language=args.query_lang)
                transformed_items += 1
                if ([h["article_id"] for h in hits[:5]]
                        != [h["article_id"] for h in base_hits[:5]]):
                    top5_changed_auto += 1
        else:  # plain Stage 1
            if args.retriever == "dense":
                hits = retriever.dense_retrieve(query, top_k=args.top_k)
            elif args.retriever == "lexical":
                hits = retriever.lexical_retrieve(query, top_k=args.top_k)
            else:
                hits = retriever.hybrid_retrieve(
                    query, top_k=args.top_k, rerank=args.rerank)

        item_latencies.append(time.monotonic() - item_start)

        retrieved_ids = [h["article_id"] for h in hits]
        gold_ids = item["source_article_ids"]
        h5  = hit_at_k(retrieved_ids, gold_ids, 5)
        r5  = recall_at_k(retrieved_ids, gold_ids, 5)
        m   = mrr(retrieved_ids, gold_ids)
        n5  = ndcg_at_k(retrieved_ids, gold_ids, 5)
        metrics["hit@5"].append(h5)
        metrics["recall@5"].append(r5)
        metrics["mrr"].append(m)
        metrics["ndcg@5"].append(n5)

        if args.log_confidence:
            verdict = evaluate_retrieval(query, hits)
            confidence_records.append((verdict["confidence"], h5, r5))

        if args.crag:
            crag_records.append({
                "item_id":  item["item_id"],
                "hop_type": item["hop_type"],
                "rule_type": item["rule_type"],
                **trace,
                "hit@5":    h5,
                "recall@5": r5,
            })

        # ── STAGE 3: record per-item trace ────────────────────────────────────
        if graph_retriever is not None:
            gt = dict(graph_retriever.last_graph_trace)
            graph_records.append({
                "item_id":          item["item_id"],
                "hop_type":         item["hop_type"],
                "rule_type":        item.get("rule_type"),
                "query_lang":       args.query_lang,
                "question":         query,
                "gold_ids":         gold_ids,
                "retrieved_ids":    retrieved_ids,
                "retrieved_scores": [h.get("score") for h in hits],
                "provenance":       [h.get("provenance") for h in hits],
                "metrics":          {"hit@5": h5, "recall@5": r5,
                                     "mrr": m, "ndcg@5": n5},
                "graph":            gt,
                "crag":             (dict(graph_retriever.last_trace)
                                     if graph_retriever.last_trace else None),
            })
        # ── END STAGE 3 recording ─────────────────────────────────────────────

    # --- aggregate print -----------------------------------------------------
    print(f"\n[metrics] n={len(dataset)} retriever={args.retriever} "
          f"rerank={args.rerank} lang={args.query_lang} "
          f"transform={args.transform} crag={args.crag}")
    for name, vals in metrics.items():
        print(f"  mean {name}: {statistics.mean(vals):.4f}" if vals
              else f"  mean {name}: n/a")
    if item_latencies:
        print(f"[latency] mean={statistics.mean(item_latencies):.3f}s "
              f"p50={statistics.median(item_latencies):.3f}s")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # --- Stage 2 Step 2-3: LLM cost ------------------------------------------
    if args.transform != "none" or args.crag:
        counters = llm_counters()
        wall = time.monotonic() - transform_wall_start
        mode = "crag" if args.crag else args.transform
        print(f"[transform={mode}] llm_calls={counters['llm_calls']} "
              f"llm_time={counters['llm_total_seconds']:.1f}s "
              f"(wall {wall:.1f}s)")
        cost_path = (f"{RESULTS_DIR}/stage2_llm_cost_{mode}"
                     f"_{args.query_lang}_{args.hop_type}.json")
        with open(cost_path, "w", encoding="utf-8") as f:
            json.dump({"transform": mode, "query_lang": args.query_lang,
                       "hop_type": args.hop_type, **counters},
                      f, ensure_ascii=False, indent=2)

    # --- Stage 2 Step 4: router stats ----------------------------------------
    if args.transform == "auto":
        counters = llm_counters()
        stats = {
            "rule_counts": dict(sorted(rule_counts.items())),
            "items_total": len(dataset),
            "items_transformed": transformed_items,
            "transforms_changed_top5": top5_changed_auto,
            "llm_calls": counters["llm_calls"],
            "llm_calls_per_100_queries": round(
                100.0 * counters["llm_calls"] / max(len(dataset), 1), 1),
            "mean_latency_s": round(statistics.mean(item_latencies), 3),
        }
        path = (f"{RESULTS_DIR}/stage2_step4_router_stats_"
                f"{args.query_lang}_{args.hop_type}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"[router] {stats}")

    # --- Stage 2 Step 5: CRAG report -----------------------------------------
    if args.crag:
        counters = llm_counters()
        branches = {}
        for r in crag_records:
            branches[r["branch"]] = branches.get(r["branch"], 0) + 1
        report = {
            "items_total": len(crag_records),
            "branch_counts": dict(sorted(branches.items())),
            "give_up_total": (branches.get("give_up_no_primary", 0)
                              + branches.get("give_up_after_primary", 0)),
            "top5_changed": sum(1 for r in crag_records if r["top5_changed"]),
            "llm_calls": counters["llm_calls"],
            "llm_calls_per_100_queries": round(
                100.0 * counters["llm_calls"] / max(len(crag_records), 1), 1),
            "mean_latency_s": round(statistics.mean(item_latencies), 3),
        }
        base = (f"{RESULTS_DIR}/stage2_step5_crag_"
                f"{args.query_lang}_{args.hop_type}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        with open(base + "_items.jsonl", "w", encoding="utf-8") as f:
            for r in crag_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[crag] {report}")

    # --- Stage 2 Step 1: confidence report -----------------------------------
    if args.log_confidence:
        write_confidence_report(
            confidence_records, DEFAULT_THRESHOLDS,
            total_items=len(dataset),
            out_path=f"{RESULTS_DIR}/stage2_step1_confidence.json",
        )

    # ── STAGE 3: write graph result file (for dump_provenance.py) ───────────
    if graph_retriever is not None:
        write_graph_report(
            per_item_records=graph_records,
            retriever_type=retriever_type_label,
            run_name=run_name,
            lang=args.query_lang,
            args=args,
        )
    # ── END STAGE 3 output ────────────────────────────────────────────────────


if __name__ == "__main__":
    main()
