"""
services/rag_retriever.py
=========================
Step 2 retrieval: the interface the compliance agents (Step 5-8) use to fetch
the most relevant Mabhas clauses for a given question.

Typical use from an agent:

    from services.rag_retriever import MabhasRetriever

    retriever = MabhasRetriever()            # construct once, reuse
    hits = retriever.retrieve(
        "minimum bedroom area",
        top_k=3,
        mabhas_part="4",       # optional filter
        rule_type="numeric",   # optional filter
    )
    for h in hits:
        print(h["article_id"], round(h["score"], 3), h["text_en"])

Each hit is a dict with:
    mabhas_part, article_id, heading_fa, text_fa, text_en,
    rule_type, entities, score   (score = cosine similarity in [0, 1])
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from services.embeddings import embed_query

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/compliance"
)


class MabhasRetriever:
    """Cosine-similarity retriever over the mabhas_clauses table."""

    def __init__(self, db_url: str = DEFAULT_DB_URL):
        self.db_url = db_url

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the top_k clauses most similar to `query`, optionally filtered
        by Mabhas part and/or rule_type.
        """
        # Imported here so the module imports cheaply and `--help`-style use of
        # the package doesn't require DB drivers to be installed.
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from pgvector.psycopg2 import register_vector

        if top_k < 1:
            raise ValueError("top_k must be >= 1")

        qvec = embed_query(query)

        # `<=>` is pgvector's cosine DISTANCE. Cosine SIMILARITY = 1 - distance.
        # The same qvec is bound twice (score expression + ORDER BY); pgvector's
        # registered adapter converts the python list to a vector each time.
        sql = [
            "SELECT mabhas_part, article_id, heading_fa, text_fa, text_en,",
            "       rule_type, entities,",
            "       1 - (embedding <=> %s) AS score",
            "FROM mabhas_clauses",
            "WHERE TRUE",
        ]
        params: List[Any] = [qvec]

        if mabhas_part is not None:
            sql.append("AND mabhas_part = %s")
            params.append(mabhas_part)
        if rule_type is not None:
            sql.append("AND rule_type = %s")
            params.append(rule_type)

        sql.append("ORDER BY embedding <=> %s")
        params.append(qvec)
        sql.append("LIMIT %s")
        params.append(top_k)

        query_sql = "\n".join(sql)

        conn = psycopg2.connect(self.db_url)
        try:
            register_vector(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query_sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()

        # RealDictCursor already returns dicts; make score a plain float.
        results = []
        for r in rows:
            r = dict(r)
            r["score"] = float(r["score"])
            results.append(r)
        return results
