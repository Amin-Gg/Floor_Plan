"""
tests/test_rag_smoke.py
=======================
End-to-end smoke test for Step 2. Run this BEFORE loading the full Mabhas
corpus, so you catch setup problems (missing pgvector extension, wrong DB URL,
wrong embedding dimension) on 3 sample clauses instead of hundreds.

Prerequisites
-------------
1. PostgreSQL running with the pgvector extension available.
2. Schema applied:   psql "$DATABASE_URL" -f db/schema.sql
3. Deps installed:   pip install -r requirements_rag.txt

Run
---
    DATABASE_URL=postgresql://localhost:5432/compliance \
        python -m tests.test_rag_smoke

A successful run prints "SMOKE TEST PASSED". Any failure raises with a clear
message. This is intentionally a plain script (no pytest required) so you can
run it in any environment.
"""

from __future__ import annotations

import os

from services.embeddings import EMBEDDING_DIM, embed_query
from services.rag_index import ingest
from services.rag_retriever import MabhasRetriever

DB_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/compliance")
SAMPLE = "data/sample_mabhas_clauses.json"


def main() -> None:
    # 1. Embedding sanity: dimension must match the schema's vector(1024).
    print("1/4  Checking embedding dimension...")
    vec = embed_query("minimum bedroom area")
    assert len(vec) == EMBEDDING_DIM, (
        f"Embedding dim {len(vec)} != schema dim {EMBEDDING_DIM}. "
        "Update db/schema.sql vector(...) and embeddings.EMBEDDING_DIM to agree."
    )
    print(f"     ok ({len(vec)} dims)")

    # 2. Ingest the 3 sample clauses (idempotent — safe to re-run).
    print("2/4  Ingesting sample clauses...")
    n = ingest(SAMPLE, db_url=DB_URL)
    assert n == 3, f"Expected 3 rows ingested, got {n}"
    print(f"     ok ({n} rows)")

    # 3. Retrieve: a numeric query should surface the bedroom-area clause first.
    print("3/4  Testing semantic retrieval...")
    retriever = MabhasRetriever(db_url=DB_URL)
    hits = retriever.retrieve("how small can a bedroom be", top_k=1)
    assert hits, "Retrieval returned no results"
    top = hits[0]
    assert top["article_id"] == "4-3-2-1", (
        f"Expected article 4-3-2-1 to rank first, got {top['article_id']}. "
        "If this fails, check the e5 'query:'/'passage:' prefixes."
    )
    print(f"     ok (top hit {top['article_id']}, score {top['score']:.3f})")

    # 4. Metadata filter: rule_type='spatial' must return only spatial clauses.
    print("4/4  Testing metadata filter...")
    spatial = retriever.retrieve("room relationships", top_k=5, rule_type="spatial")
    assert all(h["rule_type"] == "spatial" for h in spatial), \
        "rule_type filter leaked non-spatial clauses"
    print(f"     ok ({len(spatial)} spatial hits)")

    print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
