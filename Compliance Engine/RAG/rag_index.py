"""
services/rag_index.py  —  v2.0
==============================
Step 2 ingestion: load the tagged clause JSON produced by DeepSeek using the
v2.0 prompt, filter by the configured occupancy scope, embed, and upsert into
the mabhas_clauses table.

Key change from v1: the old binary `applicable_to_floor_plan` field has been
replaced by `applicable_occupancies` (a list of occupancy group codes).
Filtering is now scope-driven: you configure which occupancy groups are in
scope and the script keeps only clauses relevant to those groups.

Current scope  ->  M-4 residential (1-2 household, max 3 storeys)
Future scope   ->  add "M-2" to INGEST_SCOPE to include apartment buildings,
                   no re-running of DeepSeek needed.

Usage
-----
    # preview what will be skipped (no DB changes):
    python -m services.rag_index --input data/mabhas_clauses.json --dry-run

    # full ingest:
    DATABASE_URL=postgresql://user:pass@localhost:5432/compliance \
        python -m services.rag_index --input data/mabhas_clauses.json

    # ingest with expanded scope (e.g. add M-2 apartments):
    python -m services.rag_index --input data/mabhas_clauses.json \
        --scope M-4 all_residential M-2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from services.embeddings import embed_passages

# ---------------------------------------------------------------------------
# Scope configuration
# ---------------------------------------------------------------------------
# Current target: M-4 residential (1-2 household, max 3 storeys from street).
# "all_residential" catches rules written for all residential groups without
# specifying a sub-group (the majority of habitability rules).
#
# To expand scope later, add codes here — no other changes needed:
#   DEFAULT_SCOPE = {"M-4", "all_residential", "M-2"}
DEFAULT_SCOPE: Set[str] = {"M-4", "all_residential", "any"}

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/compliance"
)

REQUIRED_FIELDS = ("mabhas_part", "article_id", "text_fa")

VALID_SKIP_CATEGORIES = {
    "administrative", "structural_calc", "material_spec",
    "construction_method", "mep_detail", "energy_calc",
    "quality_control", "legal", "non_residential",
}


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def _is_in_scope(clause: Dict[str, Any], scope: Set[str]) -> bool:
    """
    Return True when a clause should be ingested for the given scope.

    Kept when: skip_category is null AND at least one applicable_occupancies
    code matches the scope.

    Backward-compatible with v1 format (applicable_to_floor_plan boolean).
    """
    if clause.get("skip_category"):
        return False
    occ = clause.get("applicable_occupancies")
    if occ is not None:
        return bool(set(occ) & scope)
    # v1 backward compatibility
    v1 = clause.get("applicable_to_floor_plan")
    if v1 is not None:
        return bool(v1)
    return True  # no metadata -> keep by default


def _partition(
    clauses: List[Dict[str, Any]], scope: Set[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    keep, skip = [], []
    for c in clauses:
        (keep if _is_in_scope(c, scope) else skip).append(c)
    return keep, skip


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(clauses: List[Dict[str, Any]]) -> None:
    if not isinstance(clauses, list):
        raise ValueError("Input JSON must be an array of clause objects.")
    seen: set = set()
    for i, c in enumerate(clauses):
        if not isinstance(c, dict):
            raise ValueError(f"Clause #{i} is not a JSON object.")
        for field in REQUIRED_FIELDS:
            if not c.get(field):
                raise ValueError(
                    f"Clause #{i} missing required field '{field}'. "
                    f"Keys: {sorted(c.keys())}"
                )
        cat = c.get("skip_category")
        if cat and cat not in VALID_SKIP_CATEGORIES:
            print(
                f"  WARNING [{c.get('article_id')}]: "
                f"unknown skip_category '{cat}'. "
                f"Valid: {sorted(VALID_SKIP_CATEGORIES)}"
            )
        if (c.get("skip_category") is None
                and "applicable_occupancies" not in c
                and "applicable_to_floor_plan" not in c):
            print(
                f"  WARNING [{c.get('article_id')}]: "
                "no applicable_occupancies field — kept by default."
            )
        key = (str(c["mabhas_part"]), str(c["article_id"]))
        if key in seen:
            raise ValueError(
                f"Duplicate (mabhas_part, article_id): {key}. "
                "Use suffixes: '4-5-2-1a', '4-5-2-1b'."
            )
        seen.add(key)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _print_preview(
    keep: List[Dict[str, Any]],
    skip: List[Dict[str, Any]],
    scope: Set[str],
) -> None:
    total = len(keep) + len(skip)
    print(f"\n{'─'*62}")
    print(f"  Active scope          : {sorted(scope)}")
    print(f"  Total clauses in file : {total}")
    print(f"  Will be ingested      : {len(keep)}")
    print(f"  Will be skipped       : {len(skip)}")

    if skip:
        skip_hard  = [c for c in skip if c.get("skip_category")]
        skip_scope = [c for c in skip if not c.get("skip_category")]
        if skip_hard:
            cat_counts = Counter(c.get("skip_category","—") for c in skip_hard)
            print(f"\n  Hard-skipped by category ({len(skip_hard)}):")
            for cat, n in sorted(cat_counts.items()):
                print(f"    {cat:<22} {n}")
        if skip_scope:
            print(f"\n  Scope-filtered, out of current scope ({len(skip_scope)}):")
            for c in skip_scope[:5]:
                print(f"    [{c.get('article_id')}] "
                      f"occupancies={c.get('applicable_occupancies',[])} "
                      f"| {(c.get('heading_fa') or '')[:40]}")
            if len(skip_scope) > 5:
                print(f"    … and {len(skip_scope)-5} more")
        print(f"\n  First 10 skipped:")
        for c in skip[:10]:
            art = c.get("article_id","?")
            cat = c.get("skip_category") or f"scope:{c.get('applicable_occupancies',[])}"
            hd  = (c.get("heading_fa") or c.get("text_en",""))[:40]
            print(f"    [{art}] {cat} | {hd}")
        if len(skip) > 10:
            print(f"    … and {len(skip)-10} more")

    if keep:
        rt_counts = Counter(c.get("rule_type","—") for c in keep)
        print(f"\n  Kept by rule_type:")
        for rt, n in sorted(rt_counts.items()):
            print(f"    {rt:<12} {n}")

    print(f"{'─'*62}\n")


# ---------------------------------------------------------------------------
# Passage builder
# ---------------------------------------------------------------------------

def _build_passage_text(clause: Dict[str, Any]) -> str:
    parts: List[str] = []
    if clause.get("heading_fa"):
        parts.append(str(clause["heading_fa"]).strip())
    parts.append(str(clause["text_fa"]).strip())
    if clause.get("text_en"):
        parts.append(str(clause["text_en"]).strip())
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------

def ingest(
    input_path: str,
    db_url: str = DEFAULT_DB_URL,
    batch_size: int = 16,
    dry_run: bool = False,
    scope: Optional[Set[str]] = None,
) -> int:
    if scope is None:
        scope = DEFAULT_SCOPE

    with open(input_path, encoding="utf-8") as f:
        all_clauses = json.load(f)

    _validate(all_clauses)
    print(f"Loaded {len(all_clauses)} clauses from {input_path}")

    keep, skip = _partition(all_clauses, scope)
    _print_preview(keep, skip, scope)

    if dry_run:
        print("Dry-run — no database changes made.")
        return 0

    if not keep:
        print("Nothing to ingest after filtering. Exiting.")
        return 0

    passages = [_build_passage_text(c) for c in keep]
    print("Embedding passages (first run downloads the ~2 GB model)...")
    embeddings = embed_passages(passages, batch_size=batch_size)
    assert len(embeddings) == len(keep), "embedding count mismatch"

    import psycopg2
    from psycopg2.extras import Json, execute_values
    from pgvector.psycopg2 import register_vector

    rows = []
    for clause, emb in zip(keep, embeddings):
        rows.append((
            str(clause["mabhas_part"]),
            str(clause["article_id"]),
            clause.get("heading_fa"),
            clause["text_fa"],
            clause.get("text_en"),
            clause.get("rule_type"),
            Json(clause.get("entities")) if clause.get("entities") is not None else None,
            Json(clause.get("applicable_occupancies") or []),
            Json(clause.get("applicable_height_groups") or []),
            emb,
        ))

    upsert_sql = """
        INSERT INTO mabhas_clauses
            (mabhas_part, article_id, heading_fa, text_fa,
             text_en, rule_type, entities,
             applicable_occupancies, applicable_height_groups,
             embedding)
        VALUES %s
        ON CONFLICT (mabhas_part, article_id) DO UPDATE SET
            heading_fa               = EXCLUDED.heading_fa,
            text_fa                  = EXCLUDED.text_fa,
            text_en                  = EXCLUDED.text_en,
            rule_type                = EXCLUDED.rule_type,
            entities                 = EXCLUDED.entities,
            applicable_occupancies   = EXCLUDED.applicable_occupancies,
            applicable_height_groups = EXCLUDED.applicable_height_groups,
            embedding                = EXCLUDED.embedding;
    """

    conn = psycopg2.connect(db_url)
    try:
        register_vector(conn)
        with conn.cursor() as cur:
            execute_values(cur, upsert_sql, rows)
        conn.commit()
    finally:
        conn.close()

    print(f"Upserted {len(rows)} clauses into mabhas_clauses.")
    return len(rows)


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest Mabhas clauses into pgvector (scope-aware, v2.0)."
    )
    p.add_argument("--input", required=True,
                   help="Path to the classified clauses JSON.")
    p.add_argument("--db-url", default=DEFAULT_DB_URL)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--dry-run", action="store_true",
                   help="Show filter preview without writing to the database.")
    p.add_argument("--scope", nargs="+", default=None,
                   help="Occupancy codes to ingest. "
                        "Default: M-4 all_residential. "
                        "Example: --scope M-4 all_residential M-2")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    scope = set(args.scope) if args.scope else DEFAULT_SCOPE
    ingest(args.input, db_url=args.db_url,
           batch_size=args.batch_size, dry_run=args.dry_run, scope=scope)
