"""
scripts/qdrant_audit.py
=======================
Answers "is my Qdrant deployment good enough for production?" with evidence,
not opinion. Run it from the engine root on the machine that has the
credentials:

    set QDRANT_URL=https://<cluster>.cloud.qdrant.io:6333     (Windows cmd)
    set QDRANT_API_KEY=<cluster key>
    python scripts/qdrant_audit.py
    python scripts/qdrant_audit.py --eval        # + retrieval-quality metrics

The API key is read ONLY from the environment — never pass it as an
argument (argv leaks into shell history and process lists).

Checks, in order:
  1. Connectivity + collection existence
  2. Vector config: dimension, distance metric, named vectors
     (dimension → likely embedding model inference; the engine's query
      embedder MUST be the same model, or every search is garbage)
  3. Point count vs the corpus JSON (--corpus, default data/mabhas_clauses.json)
  4. Payload schema: layout (flat vs metadata-nested), which canonical hit
     fields resolve, duplicate / missing article_id scan
  5. --eval: runs the engine's QdrantRetriever (dense + shared cross-encoder
     rerank) over the labelled eval set and prints hit@1 / hit@5 / recall@5 /
     MRR next to the Stage-1 pgvector hybrid+rerank baseline
     (hit@1 0.907, recall@5 0.919, MRR 0.936 on Persian queries).

Exit code: 0 = all hard checks passed; 1 = at least one hard failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dimension → plausible embedding models (query embedder must match!)
_DIM_HINTS = {
    384: "MiniLM-L6 family / bge-small",
    768: "bert-base family / bge-base / e5-base / nomic",
    1024: "multilingual-e5-large (PROJECT DEFAULT) or BAAI/bge-m3",
    1536: "OpenAI text-embedding-3-small / ada-002",
    3072: "OpenAI text-embedding-3-large",
}

_BASELINE = {"hit@1": 0.907, "hit@5": 0.977, "recall@5": 0.919, "mrr": 0.936}

_OK, _WARN, _FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def _p(tag: str, msg: str) -> None:
    print(f"  {tag} {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--collection",
                    default=os.environ.get("QDRANT_COLLECTION",
                                           "mabhas_clauses"))
    ap.add_argument("--corpus", default="data/mabhas_clauses.json",
                    help="corpus JSON to compare counts against")
    ap.add_argument("--eval", action="store_true",
                    help="run retrieval-quality metrics on the eval set "
                         "(loads the embedding + reranker models)")
    ap.add_argument("--eval-set", default="data/mabhas_eval_set.json")
    ap.add_argument("--sample", type=int, default=1000,
                    help="points to scroll for the payload/duplicate scan")
    args = ap.parse_args()

    hard_fail = False
    url = os.environ.get("QDRANT_URL", "")
    if not url:
        _p(_FAIL, "QDRANT_URL is not set")
        return 1
    print(f"Qdrant audit — endpoint {url}  collection {args.collection!r}\n")

    # 1 ── connectivity + collection ----------------------------------------
    try:
        from qdrant_client import QdrantClient
        cl = QdrantClient(url=url,
                          api_key=os.environ.get("QDRANT_API_KEY") or None,
                          timeout=float(os.environ.get("QDRANT_TIMEOUT_S",
                                                       "30")))
        collections = [c.name for c in cl.get_collections().collections]
    except Exception as exc:  # noqa: BLE001 — this IS the connectivity check
        _p(_FAIL, f"cannot reach Qdrant: {type(exc).__name__}: {exc}")
        return 1
    _p(_OK, f"connected; collections on cluster: {collections}")
    if args.collection not in collections:
        _p(_FAIL, f"collection {args.collection!r} does not exist "
                  f"(set --collection or QDRANT_COLLECTION)")
        return 1

    info = cl.get_collection(args.collection)
    points_count = info.points_count or 0

    # 2 ── vector config ------------------------------------------------------
    cfg = info.config.params.vectors
    if isinstance(cfg, dict):
        for name, vp in cfg.items():
            _p(_OK, f"named vector {name!r}: dim={vp.size} "
                    f"distance={vp.distance}")
            dim, dist = vp.size, str(vp.distance)
        if len(cfg) > 1:
            _p(_WARN, "multiple named vectors — set QDRANT_VECTOR_NAME for "
                      "the engine")
    else:
        dim, dist = cfg.size, str(cfg.distance)
        _p(_OK, f"single unnamed vector: dim={dim} distance={dist}")

    hint = _DIM_HINTS.get(dim, "unknown model family")
    _p(_OK if dim == 1024 else _WARN,
       f"dim {dim} → {hint}")
    active_embed = os.environ.get("EMBED_MODEL", "")
    if dim == 1024:
        # e5-large and bge-m3 are BOTH 1024-dim: equal dims cannot prove the
        # same model. The collection is bge-m3-embedded (operator, 2026-07),
        # so the engine's query embedder must be pinned to match.
        if active_embed == "BAAI/bge-m3":
            _p(_OK, "EMBED_MODEL=BAAI/bge-m3 — matches the bge-m3-embedded "
                    "collection")
        elif not active_embed:
            _p(_FAIL, "EMBED_MODEL is UNSET → the engine defaults to "
                      "multilingual-e5-large, but this collection is "
                      "bge-m3-embedded. Both are 1024-dim, so nothing else "
                      "will catch this — set EMBED_MODEL=BAAI/bge-m3")
            hard_fail = True
        else:
            _p(_WARN, f"EMBED_MODEL={active_embed!r} — confirm this is the "
                      "model that built the collection (dims alone cannot)")
    if dim != 1024:
        _p(_WARN, "the engine's query embedder (multilingual-e5-large) is "
                  "1024-dim — if the collection was built with a different "
                  "model, set EMBED_MODEL to match or re-embed; the engine "
                  "refuses mismatched dims at query time")
    if "cosine" not in dist.lower():
        _p(_WARN, f"distance is {dist}, not Cosine — scores will not match "
                  "the pgvector similarity scale (1 - cosine distance); "
                  "CRAG confidence thresholds were tuned on cosine")

    # 3 ── counts vs corpus ---------------------------------------------------
    _p(_OK, f"points in collection: {points_count}")
    if points_count == 0:
        _p(_FAIL, "collection is EMPTY")
        hard_fail = True
    if os.path.exists(args.corpus):
        try:
            with open(args.corpus, encoding="utf-8") as f:
                corpus = json.load(f)
            n = len(corpus) if isinstance(corpus, list) else len(
                corpus.get("clauses", []))
            tag = _OK if points_count == n else _WARN
            _p(tag, f"corpus JSON {args.corpus!r} has {n} clauses "
                    f"({'match' if points_count == n else 'MISMATCH'} vs "
                    f"{points_count} points)")
        except (ValueError, OSError) as exc:
            _p(_WARN, f"could not read corpus JSON: {exc}")
    else:
        _p(_WARN, f"corpus JSON not found at {args.corpus!r} — skipped count "
                  "comparison")

    # 4 ── payload schema + duplicates ---------------------------------------
    from rag.qdrant_retriever import _ALIASES  # single source of alias truth

    scanned, ids, next_off = 0, [], None
    sample_payload: Dict[str, Any] = {}
    while scanned < args.sample:
        pts, next_off = cl.scroll(args.collection,
                                  limit=min(256, args.sample - scanned),
                                  offset=next_off, with_payload=True)
        if not pts:
            break
        for pt in pts:
            payload = pt.payload or {}
            if not sample_payload:
                sample_payload = payload
            merged = (dict(payload.get("metadata") or {}) | payload
                      if isinstance(payload.get("metadata"), dict)
                      else payload)
            aid = next((merged[a] for a in _ALIASES["article_id"]
                        if merged.get(a) not in (None, "")), None)
            ids.append(str(aid) if aid is not None else None)
        scanned += len(pts)
        if next_off is None:
            break

    layout = ("metadata" if isinstance(sample_payload.get("metadata"), dict)
              else "flat")
    _p(_OK, f"payload layout: {layout}; top-level keys of first point: "
            f"{sorted(sample_payload)}")

    missing = sum(1 for i in ids if i is None)
    if missing:
        _p(_FAIL, f"{missing}/{scanned} scanned points have NO resolvable "
                  f"article_id (aliases tried: {list(_ALIASES['article_id'])})")
        hard_fail = True
    else:
        _p(_OK, f"article_id resolves on all {scanned} scanned points")
    dupes = [i for i, c in Counter(i for i in ids if i).items() if c > 1]
    if dupes:
        _p(_WARN, f"{len(dupes)} duplicate article_ids in the sample "
                  f"(first few: {dupes[:5]}) — duplicates skew RRF/rerank")

    merged_first = (dict(sample_payload.get("metadata") or {}) | sample_payload
                    if layout == "metadata" else sample_payload)
    for field in ("text_fa", "text_en", "rule_type", "mabhas_part"):
        found = next((a for a in _ALIASES[field] if merged_first.get(a)
                      not in (None, "")), None)
        _p(_OK if found else _WARN,
           f"hit field {field!r}: "
           f"{'via payload key ' + repr(found) if found else 'NOT FOUND — engine hits will carry None'}")

    # 5 ── retrieval-quality eval ---------------------------------------------
    if args.eval:
        if not os.path.exists(args.eval_set):
            _p(_WARN, f"eval set not found at {args.eval_set!r} — skipped")
        else:
            print("\nRetrieval-quality eval (dense + cross-encoder rerank; "
                  "loads models — first run is slow):")
            from eval.metrics import all_metrics
            from rag.qdrant_retriever import QdrantDefaultRetriever

            with open(args.eval_set, encoding="utf-8") as f:
                eval_rows = json.load(f)
            retriever = QdrantDefaultRetriever(collection=args.collection)
            agg: Dict[str, List[float]] = {}
            for row in eval_rows:
                query = row.get("query_fa") or row.get("query") or ""
                gold = set(map(str, row.get("gold_article_ids")
                               or row.get("gold_ids") or []))
                if not query or not gold:
                    continue
                hits = retriever.retrieve(query, top_k=5)
                m = all_metrics([h["article_id"] for h in hits], gold)
                for k, v in m.items():
                    agg.setdefault(k, []).append(v)
            print(f"  queries evaluated: {len(next(iter(agg.values()), []))}")
            print(f"  {'metric':<10}{'qdrant':>9}{'pgvector baseline':>20}")
            for k in ("hit@1", "hit@5", "recall@5", "mrr"):
                if k in agg:
                    got = sum(agg[k]) / len(agg[k])
                    base = _BASELINE.get(k)
                    delta = f"  (Δ {got - base:+.3f})" if base else ""
                    print(f"  {k:<10}{got:>9.3f}{base:>20.3f}{delta}")
            print("  Verdict guidance: within ~0.02 of baseline → ship; "
                  "recall@5 drop > 0.05 → the missing lexical leg matters "
                  "for your corpus; keep pgvector or add sparse vectors.")

    print()
    print("RESULT:", "HARD FAILURES — fix before production" if hard_fail
          else "no hard failures (review WARN lines)")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())