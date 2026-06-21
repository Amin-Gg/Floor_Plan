"""
eval/retrieval_eval.py — REFERENCE implementation of the Stage 1+2 harness.

This is a complete, runnable consolidation of the Stage 2 diffs from Steps
1-5. You have an existing Stage 1 harness; two ways to use this file:

  (a) ADOPT IT: port your retriever construction into build_base_retriever()
      below (the single >>> ADAPTER POINT <<<) and use this file as-is. The
      metric definitions (hit@k, recall@k, MRR, nDCG@k with binary relevance)
      are standard; verify one Stage 1 run reproduces SUMMARY.md before
      trusting cross-stage comparisons.
  (b) PORT FROM IT: keep your harness and copy the clearly-sectioned Stage 2
      blocks (--log-confidence, --transform, --hop-type, --crag) into it.

Flags:
  --retriever {hybrid,dense,lexical}   base retrieval mode (default hybrid)
  --rerank                             apply the Stage 1 cross-encoder
  --query-lang {fa,en}                 which question field to use
  --eval-set PATH                      default data/mabhas_eval_set.json
  --top-k INT                          default 5
  --log-confidence                     Step 1: per-label confidence report
  --transform {none,hyde,stepback,multi_query,auto}   Steps 2-4
  --hop-type {all,zero_hop,multi_hop}  Step 3: subset filter
  --crag                               Step 5: full CorrectiveRetriever loop
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time

from rag.retrieval_evaluator import evaluate_retrieval, DEFAULT_THRESHOLDS
from rag.query_transforms import reset_llm_counters, llm_counters
from rag.query_router import route_query
from rag.crag_retriever import CorrectiveRetriever

RESULTS_DIR = "eval/results"


# ===========================================================================
# Retriever construction
# ===========================================================================

def build_base_retriever():
    """Constructs the bare MabhasRetriever using the Stage 1 winning config.

    All configuration is read from the environment:
      DATABASE_URL  — Neon/PostgreSQL connection string
      EMBED_MODEL   — path to multilingual-e5-large (services/embeddings.py
                      reads this at import time; must match the index)

    Winning Stage 1 config: hybrid + bge-m3 cross-encoder rerank.
    Run with: --retriever hybrid --rerank --query-lang fa
    Baseline numbers (SUMMARY.md):
      hit@1=0.907  hit@5=0.977  recall@5=0.919  mrr=0.936
    """
    from rag.rag_retriever import MabhasRetriever
    return MabhasRetriever()


# ===========================================================================
# Metrics (standard definitions; binary relevance against source_article_ids)
# ===========================================================================

def hit_at_k(retrieved_ids: list[str], gold_ids: list[str], k: int = 5) -> float:
    topk = retrieved_ids[:k]
    return 1.0 if any(g in topk for g in gold_ids) else 0.0


def recall_at_k(retrieved_ids: list[str], gold_ids: list[str], k: int = 5) -> float:
    if not gold_ids:
        return 0.0
    topk = set(retrieved_ids[:k])
    return sum(1 for g in gold_ids if g in topk) / len(gold_ids)


def mrr(retrieved_ids: list[str], gold_ids: list[str]) -> float:
    gold = set(gold_ids)
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: list[str], gold_ids: list[str], k: int = 5) -> float:
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
    parser.add_argument("--retriever", default="hybrid",
                        choices=["hybrid", "dense", "lexical"])
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--query-lang", default="fa", choices=["fa", "en"])
    parser.add_argument("--eval-set", default="data/mabhas_eval_set.json")
    parser.add_argument("--top-k", type=int, default=5)
    # --- Stage 2, Step 1 ---
    parser.add_argument(
        "--log-confidence", action="store_true",
        help="Run the Stage 2 retrieval confidence evaluator per item and "
             "write eval/results/stage2_step1_confidence.json.")
    # --- Stage 2, Steps 2-4 ---
    parser.add_argument(
        "--transform", default="none",
        choices=["none", "hyde", "stepback", "multi_query", "auto"],
        help="Query transformation to apply before retrieval. "
             "hyde: hypothetical Mabhas-style answer (Step 2). "
             "stepback: fuse original + broader principle question (Step 3). "
             "multi_query: fuse n reformulations (Step 3). "
             "auto: rule-based router picks per query (Step 4). "
             "All log LLM call count and total time.")
    # --- Stage 2, Step 3 ---
    parser.add_argument(
        "--hop-type", default="all",
        choices=["all", "zero_hop", "multi_hop"],
        help="Restrict evaluation to a hop_type subset of the eval set.")
    # --- Stage 2, Step 5 ---
    parser.add_argument(
        "--crag", action="store_true",
        help="Use CorrectiveRetriever.retrieve() (full Stage 2 CRAG loop). "
             "Mutually exclusive with --transform.")
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


# ===========================================================================
# Main
# ===========================================================================

def main():
    args = build_arg_parser().parse_args()
    if args.crag and args.transform != "none":
        raise SystemExit("--crag and --transform are mutually exclusive")

    retriever = build_base_retriever()
    crag = CorrectiveRetriever(retriever) if args.crag else None

    with open(args.eval_set, encoding="utf-8") as f:
        dataset = json.load(f)
    if args.hop_type != "all":
        dataset = [it for it in dataset if it["hop_type"] == args.hop_type]
        print(f"[subset] hop_type={args.hop_type}: {len(dataset)} items")

    # Accumulators
    metrics = {"hit@5": [], "recall@5": [], "mrr": [], "ndcg@5": []}
    confidence_records = []   # (label, hit5, recall5)
    crag_records = []         # per-item CRAG trace + metrics
    rule_counts = {}          # auto mode
    transformed_items = 0     # auto mode
    top5_changed_auto = 0     # auto mode
    item_latencies = []
    reset_llm_counters()
    transform_wall_start = time.monotonic()

    for item in dataset:
        query = (item["question_fa"] if args.query_lang == "fa"
                 else item["question_en"])
        item_start = time.monotonic()
        trace = None

        if args.crag:
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
        else:  # plain Stage 1 modes
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
        h5 = hit_at_k(retrieved_ids, gold_ids, 5)
        r5 = recall_at_k(retrieved_ids, gold_ids, 5)
        metrics["hit@5"].append(h5)
        metrics["recall@5"].append(r5)
        metrics["mrr"].append(mrr(retrieved_ids, gold_ids))
        metrics["ndcg@5"].append(ndcg_at_k(retrieved_ids, gold_ids, 5))

        if args.log_confidence:
            verdict = evaluate_retrieval(query, hits)
            confidence_records.append((verdict["confidence"], h5, r5))

        if args.crag:
            crag_records.append({
                "item_id": item["item_id"],
                "hop_type": item["hop_type"],
                "rule_type": item["rule_type"],
                **trace,
                "hit@5": h5,
                "recall@5": r5,
            })

    # --- Aggregate metric printing -----------------------------------------
    print(f"\n[metrics] n={len(dataset)} retriever={args.retriever} "
          f"rerank={args.rerank} lang={args.query_lang} "
          f"transform={args.transform} crag={args.crag}")
    for name, vals in metrics.items():
        print(f"  mean {name}: {statistics.mean(vals):.4f}" if vals
              else f"  mean {name}: n/a")
    if item_latencies:
        print(f"[latency] mean per item: {statistics.mean(item_latencies):.3f}s "
              f"(p50 {statistics.median(item_latencies):.3f}s)")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # --- Step 2-3: LLM cost report -------------------------------------------
    if args.transform != "none" or args.crag:
        counters = llm_counters()
        wall = time.monotonic() - transform_wall_start
        mode = "crag" if args.crag else args.transform
        print(f"[transform={mode}] llm_calls={counters['llm_calls']} "
              f"llm_time={counters['llm_total_seconds']:.1f}s "
              f"(run wall time {wall:.1f}s)")
        cost_path = (f"{RESULTS_DIR}/stage2_llm_cost_{mode}"
                     f"_{args.query_lang}_{args.hop_type}.json")
        with open(cost_path, "w", encoding="utf-8") as f:
            json.dump({"transform": mode, "query_lang": args.query_lang,
                       "hop_type": args.hop_type, **counters},
                      f, ensure_ascii=False, indent=2)

    # --- Step 4: router stats -------------------------------------------------
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

    # --- Step 5: CRAG report --------------------------------------------------
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
        base = f"{RESULTS_DIR}/stage2_step5_crag_{args.query_lang}_{args.hop_type}"
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        with open(base + "_items.jsonl", "w", encoding="utf-8") as f:
            for r in crag_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[crag] {report}")

    # --- Step 1: confidence report (written last; rename per condition) -------
    if args.log_confidence:
        write_confidence_report(
            confidence_records, DEFAULT_THRESHOLDS,
            total_items=len(dataset),
            out_path=f"{RESULTS_DIR}/stage2_step1_confidence.json",
        )


if __name__ == "__main__":
    main()