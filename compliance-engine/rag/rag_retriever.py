"""
services/rag_retriever.py
=========================
Retrieval interface over the mabhas_clauses table.

Stage 1 / Step 3 adds lexical (BM25-style, ts_rank_cd) and hybrid (RRF)
retrieval next to the original dense cosine retrieval. The public method
retrieve() is preserved verbatim as a thin alias of dense_retrieve() —
the four deterministic agents depend on its signature and hit shape.

Typical use from an agent (unchanged):

    from rag.rag_retriever import MabhasRetriever

    retriever = MabhasRetriever()            # construct once, reuse
    hits = retriever.retrieve(
        "minimum bedroom area",
        top_k=3,
        mabhas_part="4",       # optional filter
        rule_type="numeric",   # optional filter
    )

Each hit is a dict with:
    mabhas_part, article_id, heading_fa, text_fa, text_en,
    rule_type, entities, score
where score is:
    dense_retrieve   -> cosine similarity in [0, 1]
    lexical_retrieve -> ts_rank_cd value (unbounded, comparable within a query)
    hybrid_retrieve  -> RRF fused score (Σ 1/(rrf_k + rank))
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from rag.embeddings import embed_query

# Stage 2 — query transformations (advisory pre-processing; each falls back
# to the original query on any failure, so importing them adds no fragility).
from rag.query_transforms import (
    hyde_transform,
    multi_query_transform,
    stepback_transform,
)

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/compliance"
)

_HIT_COLUMNS = (
    "mabhas_part, article_id, heading_fa, text_fa, text_en, rule_type, entities, "
    "context_fa"  # Step 5: additive key; NULL for non-contextual ingests
)

# Unicode word tokenizer for building OR-tsqueries. \w covers Persian/Arabic
# letters and both digit blocks; tokens therefore can never contain quotes,
# so embedding them in to_tsquery literals below is injection-safe.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    """Unique word tokens of `text`, first-seen order preserved."""
    seen: set = set()
    out: List[str] = []
    for tok in _WORD_RE.findall(text or ""):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion — pure function, no I/O
# ---------------------------------------------------------------------------

def rrf_fuse(
    list_of_ranked_id_lists: Sequence[Sequence[str]], rrf_k: int = 60
) -> Dict[str, float]:
    """Fuse several ranked id lists with Reciprocal Rank Fusion.

    For each id: score = Σ over lists of 1 / (rrf_k + rank), where rank is
    the 1-based position of the id in that list. Ids absent from a list
    contribute 0 for that list. If an id appears more than once within a
    single list, only its first (best) occurrence counts.

    Reference: Cormack, Clarke & Buettcher (2009), "Reciprocal Rank Fusion
    outperforms Condorcet and individual Rank Learning Methods", SIGIR.

    Parameters
    ----------
    list_of_ranked_id_lists : sequence of ranked id sequences (best first)
    rrf_k : the RRF smoothing constant (60 in the original paper)

    Returns
    -------
    dict mapping id -> fused score (unsorted; caller sorts as needed)
    """
    if rrf_k < 0:
        raise ValueError("rrf_k must be >= 0")
    fused: Dict[str, float] = {}
    for ranked in list_of_ranked_id_lists:
        seen: set = set()
        for rank, _id in enumerate(ranked, start=1):
            if _id in seen:
                continue  # first occurrence only
            seen.add(_id)
            fused[_id] = fused.get(_id, 0.0) + 1.0 / (rrf_k + rank)
    return fused


def _build_rerank_passage(hit: Dict[str, Any]) -> str:
    """Passage text the cross-encoder scores for one hit.

    Mirrors the STRUCTURE of ingestion's _build_passage_text
    (context -> heading -> Persian body -> English) and uses the
    normalized / contextual fields when the hit carries them
    (context_fa, text_fa_normalized arrive with Step 5's ingest).
    Hits coming from today's DB columns fall back to the raw
    heading_fa / text_fa, which is the authoritative clause text.
    """
    parts: List[str] = []
    if hit.get("context_fa"):
        parts.append(str(hit["context_fa"]).strip())
    heading = hit.get("heading_fa_normalized") or hit.get("heading_fa")
    if heading:
        parts.append(str(heading).strip())
    body = hit.get("text_fa_normalized") or hit.get("text_fa")
    if body:
        parts.append(str(body).strip())
    if hit.get("text_en"):
        parts.append(str(hit["text_en"]).strip())
    return "\n".join(p for p in parts if p)


class MabhasRetriever:
    """Dense / lexical / hybrid retriever over the mabhas_clauses table."""

    def __init__(self, db_url: str = DEFAULT_DB_URL):
        self.db_url = db_url
        # wall-clock seconds of the most recent cross-encoder rerank stage
        # (None until hybrid_retrieve(..., rerank=True) has run once)
        self.last_rerank_seconds: Optional[float] = None

    # ------------------------------------------------------------------
    # Shared SQL execution
    # ------------------------------------------------------------------

    def _query_hits(self, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        # Imported here so the module imports cheaply and `--help`-style use
        # of the package doesn't require DB drivers to be installed.
        import psycopg2
        from psycopg2.extras import RealDictCursor
        from pgvector.psycopg2 import register_vector

        conn = psycopg2.connect(self.db_url)
        try:
            register_vector(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()

        results = []
        for r in rows:
            r = dict(r)
            r["score"] = float(r["score"])
            results.append(r)
        return results

    def _fetch_passages(self, article_ids: List[str]) -> Dict[str, str]:
        """Stored ingest-time passages for the given article_ids.

        Returns {article_id: passage} for rows where passage IS NOT NULL;
        callers fall back to raw clause fields for missing ids.
        """
        if not article_ids:
            return {}
        import psycopg2

        conn = psycopg2.connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT article_id, passage FROM mabhas_clauses "
                    "WHERE article_id = ANY(%s) AND passage IS NOT NULL",
                    (article_ids,),
                )
                return {aid: p for aid, p in cur.fetchall()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Dense (cosine) retrieval — the original Step 2 logic
    # ------------------------------------------------------------------

    def dense_retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top_k clauses by cosine similarity of e5 embeddings."""
        import numpy as np

        if top_k < 1:
            raise ValueError("top_k must be >= 1")

        # pgvector's psycopg2 adapter (register_vector) adapts numpy arrays to
        # the `vector` type; a plain Python list would bind as numeric[] and
        # break the `<=>` operator. embed_query returns a list, so cast here.
        qvec = np.asarray(embed_query(query), dtype=np.float32)

        # `<=>` is pgvector's cosine DISTANCE. Cosine SIMILARITY = 1 - distance.
        sql = [
            f"SELECT {_HIT_COLUMNS},",
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

        return self._query_hits("\n".join(sql), params)

    # ------------------------------------------------------------------
    # Lexical (BM25-style) retrieval
    # ------------------------------------------------------------------

    def lexical_retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top_k clauses by ts_rank_cd over the generated lexeme column.

        Uses the 'simple' configuration (exact token matching — correct for
        Persian text, article identifiers, technical terms and numbers).

        The tsquery is an OR over the query's tokens, NOT plainto_tsquery:
        plainto_tsquery ANDs every token, so a natural-language question
        (whose words never all co-occur in one clause) matches zero rows —
        verified empirically on the eval set (0 matches AND vs 315 OR for
        English, 222 for Persian). With OR semantics ts_rank_cd ranks by
        match count, density and proximity, i.e. BM25-style behaviour.

        Rows matching no token at all are excluded, so fewer than top_k hits
        may be returned for full-vocabulary-mismatch queries.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")

        tokens = _tokenize(query)
        if not tokens:
            raise ValueError("lexical_retrieve received an empty query string")
        or_tsquery = " | ".join(f"'{t}'" for t in tokens)

        sql = [
            f"SELECT {_HIT_COLUMNS},",
            "       ts_rank_cd(lexeme, to_tsquery('simple', %s)) AS score",
            "FROM mabhas_clauses",
            "WHERE lexeme @@ to_tsquery('simple', %s)",
        ]
        params: List[Any] = [or_tsquery, or_tsquery]

        if mabhas_part is not None:
            sql.append("AND mabhas_part = %s")
            params.append(mabhas_part)
        if rule_type is not None:
            sql.append("AND rule_type = %s")
            params.append(rule_type)

        sql.append("ORDER BY score DESC, article_id")
        sql.append("LIMIT %s")
        params.append(top_k)

        return self._query_hits("\n".join(sql), params)

    # ------------------------------------------------------------------
    # Hybrid retrieval — RRF fusion of dense + lexical
    # ------------------------------------------------------------------

    def hybrid_retrieve(
        self,
        query: str,
        top_k: int = 3,
        candidate_k: int = 50,
        rrf_k: int = 60,
        rerank: bool = False,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fuse dense and lexical candidate lists with Reciprocal Rank Fusion,
        optionally re-scoring the fused candidates with a cross-encoder.

        Both retrievers contribute their top candidate_k results; the fused
        score for each article is Σ 1/(rrf_k + rank) across the two lists
        (missing from a list -> contributes 0). Optional mabhas_part /
        rule_type filters are forwarded to BOTH legs (Step 7: lets the
        default production retriever honour agent-supplied filters).

        rerank=False: return the top_k articles by fused score;
                      score = RRF score.
        rerank=True : score the top candidate_k fused hits with
                      BAAI/bge-reranker-v2-m3 (loaded lazily on first use,
                      never imported otherwise), sort by cross-encoder score
                      DESC and return top_k; score = cross-encoder score and
                      the fused score is preserved as hit["rrf_score"].
                      The wall-clock duration of the rerank stage is stored
                      in self.last_rerank_seconds for latency reporting.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if candidate_k < top_k:
            raise ValueError("candidate_k must be >= top_k")

        dense_hits = self.dense_retrieve(
            query, top_k=candidate_k,
            mabhas_part=mabhas_part, rule_type=rule_type,
        )
        lexical_hits = self.lexical_retrieve(
            query, top_k=candidate_k,
            mabhas_part=mabhas_part, rule_type=rule_type,
        )

        fused = rrf_fuse(
            [
                [h["article_id"] for h in dense_hits],
                [h["article_id"] for h in lexical_hits],
            ],
            rrf_k=rrf_k,
        )

        # Keep one representative hit dict per article (dense first — both
        # carry identical clause fields; only 'score' differs and is replaced).
        by_id: Dict[str, Dict[str, Any]] = {}
        for h in dense_hits + lexical_hits:
            by_id.setdefault(h["article_id"], h)

        ranked_ids = sorted(fused, key=lambda i: (-fused[i], i))

        if not rerank:
            results = []
            for _id in ranked_ids[:top_k]:
                hit = dict(by_id[_id])
                hit["score"] = fused[_id]
                results.append(hit)
            return results

        # ---- cross-encoder rerank stage (lazy import: only on rerank=True) --
        import time

        from rag.reranker import rerank as _ce_rerank

        candidates = [dict(by_id[_id]) for _id in ranked_ids[:candidate_k]]

        # Prefer the stored passage (the exact text that was embedded,
        # including normalization and — from Step 5 — context_fa). Rows
        # ingested before Step 4 have passage = NULL and fall back to the
        # raw clause fields via _build_rerank_passage.
        stored = self._fetch_passages([h["article_id"] for h in candidates])
        passages = [
            stored.get(h["article_id"]) or _build_rerank_passage(h)
            for h in candidates
        ]

        t0 = time.perf_counter()
        ce_scores = _ce_rerank(query, passages)
        self.last_rerank_seconds = time.perf_counter() - t0

        for hit, ce in zip(candidates, ce_scores):
            hit["rrf_score"] = fused[hit["article_id"]]
            hit["score"] = float(ce)

        candidates.sort(key=lambda h: (-h["score"], h["article_id"]))
        return candidates[:top_k]

    # ------------------------------------------------------------------
    # Stage 2 — transform-augmented retrieval (Steps 2-3)
    # ------------------------------------------------------------------

    def _rerank_against(self, query: str, candidates: List[Dict[str, Any]],
                        top_k: int) -> List[Dict[str, Any]]:
        """Cross-encode `candidates` against `query` and return top_k.

        Mirrors hybrid_retrieve's rerank stage exactly: prefer the stored
        ingest-time passage, fall back to _build_rerank_passage; the fused
        score each candidate arrives with is preserved as 'rrf_score' and
        'score' becomes the cross-encoder logit. The reranker module is
        imported lazily, only when reranking actually runs.
        """
        if not candidates:
            return []
        import time

        from rag.reranker import rerank as _ce_rerank

        stored = self._fetch_passages([h["article_id"] for h in candidates])
        passages = [
            stored.get(h["article_id"]) or _build_rerank_passage(h)
            for h in candidates
        ]

        t0 = time.perf_counter()
        ce_scores = _ce_rerank(query, passages)
        self.last_rerank_seconds = time.perf_counter() - t0

        for hit, ce in zip(candidates, ce_scores):
            hit["rrf_score"] = hit["score"]
            hit["score"] = float(ce)

        candidates.sort(key=lambda h: (-h["score"], h["article_id"]))
        return candidates[:top_k]

    def _fused_candidates(
        self,
        queries: List[str],
        candidate_k: int,
        rrf_k: int,
        mabhas_part: Optional[str],
        rule_type: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Un-reranked hybrid candidates per query, RRF-fused across queries.

        Reuses the module-level rrf_fuse (Stage 1, Cormack et al. 2009) and
        the same by_id representative-hit pattern as hybrid_retrieve. Each
        returned hit carries the cross-query fused score in 'score'.
        """
        ranked_lists = [
            self.hybrid_retrieve(
                q, top_k=candidate_k, candidate_k=candidate_k, rrf_k=rrf_k,
                rerank=False, mabhas_part=mabhas_part, rule_type=rule_type,
            )
            for q in queries
        ]
        if len(ranked_lists) == 1:
            return [dict(h) for h in ranked_lists[0]]

        fused = rrf_fuse(
            [[h["article_id"] for h in ranked] for ranked in ranked_lists],
            rrf_k=rrf_k,
        )
        by_id: Dict[str, Dict[str, Any]] = {}
        for ranked in ranked_lists:
            for h in ranked:
                by_id.setdefault(h["article_id"], h)

        out = []
        for _id in sorted(fused, key=lambda i: (-fused[i], i)):
            hit = dict(by_id[_id])
            hit["score"] = fused[_id]
            out.append(hit)
        return out

    def hyde_retrieve(
        self,
        query: str,
        top_k: int = 3,
        candidate_k: int = 50,
        rrf_k: int = 60,
        rerank: bool = True,
        language: str = "auto",
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """HyDE retrieval (Gao et al. 2022): generate a hypothetical
        Mabhas-style answer, then run standard hybrid retrieval on it.

        Degrades to plain hybrid retrieval automatically when the transform
        fails (hyde_transform returns the original query on any API error).
        Filters are forwarded to both hybrid legs at the SQL level.
        """
        hypothetical = hyde_transform(query, language)
        return self.hybrid_retrieve(
            hypothetical,
            top_k=top_k,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            rerank=rerank,
            mabhas_part=mabhas_part,
            rule_type=rule_type,
        )

    def stepback_retrieve(
        self,
        query: str,
        top_k: int = 3,
        candidate_k: int = 50,
        rrf_k: int = 60,
        rerank: bool = True,
        language: str = "auto",
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Step-back retrieval (Zheng et al. 2023).

        Retrieve with BOTH the original query and an LLM-generated broader
        question, RRF-fuse the two candidate lists, then rerank the fused
        candidates against the ORIGINAL query — the broader question recalls
        principle-level clauses the specific phrasing misses, while final
        ordering stays anchored to the user's actual information need.
        """
        broader = stepback_transform(query, language)
        # If the transform fell back (broader == query), skip the duplicate
        # retrieval — RRF of two identical lists is a no-op anyway.
        queries = [query] if broader.strip() == query.strip() else [query, broader]
        fused = self._fused_candidates(
            queries, candidate_k, rrf_k, mabhas_part, rule_type
        )
        if rerank:
            return self._rerank_against(query, fused, top_k)
        return fused[:top_k]

    def multi_query_retrieve(
        self,
        query: str,
        top_k: int = 3,
        n: int = 3,
        candidate_k: int = 50,
        rrf_k: int = 60,
        rerank: bool = True,
        language: str = "auto",
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Multi-query retrieval: retrieve for n reformulations (item 0 is
        always the original), RRF-fuse all ranked lists, then rerank the
        union against the ORIGINAL query.
        """
        queries = multi_query_transform(query, n=n, language=language)
        fused = self._fused_candidates(
            queries, candidate_k, rrf_k, mabhas_part, rule_type
        )
        if rerank:
            return self._rerank_against(query, fused, top_k)
        return fused[:top_k]

    # ------------------------------------------------------------------
    # Backward-compatible public API — used by the four agents. DO NOT CHANGE.
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the top_k clauses most similar to `query`, optionally filtered
        by Mabhas part and/or rule_type. Thin alias of dense_retrieve().
        """
        return self.dense_retrieve(
            query, top_k=top_k, mabhas_part=mabhas_part, rule_type=rule_type
        )


# ---------------------------------------------------------------------------
# Stage 1 / Step 7 — production factory
# ---------------------------------------------------------------------------

class _DefaultRetriever(MabhasRetriever):
    """MabhasRetriever whose retrieve() routes to the Stage 1 winning
    configuration: hybrid RRF fusion + cross-encoder rerank.

    The public signature and hit-dict shape are identical to the base
    class (rerank adds the extra 'rrf_score' key, which is additive);
    agent-supplied mabhas_part / rule_type filters are honoured by
    forwarding them into both retrieval legs.
    """

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.hybrid_retrieve(
            query,
            top_k=top_k,
            rerank=True,
            mabhas_part=mabhas_part,
            rule_type=rule_type,
        )


def build_default_retriever() -> Any:
    """
    Returns a retriever configured to use hybrid_retrieve(rerank=True) as its
    default retrieval call. Reads DATABASE_URL and EMBED_MODEL from env.

    DATABASE_URL is read here (via DEFAULT_DB_URL); EMBED_MODEL is read by
    rag.embeddings at import time — the same module that embeds both
    queries and passages, so the query-side model always matches whatever
    EMBED_MODEL the index was ingested with, provided the operator exports
    the same value for both (the ingest banner and eval run_config both log
    the active model for verification).

    This factory is the ONLY place production code should construct a
    retriever: retrieval configuration evolves here, while agents and the
    LLM interpretive pass keep calling .retrieve() with an unchanged
    signature and hit-dict shape.

    Stage 2: CRAG_ENABLED (default "1") wraps the retriever in the
    corrective layer — confidence-gated query transformations with the
    human-review queue as final fallback. CRAG_ENABLED=0 returns the bare
    Stage 1 winner (hybrid + rerank); unit tests pin 0 via conftest.py so
    Stage 1 behavior stays regression-guarded.

    Stage 3: GRAPH_ENABLED (default "1" — production default since
    Stage 3 / Step 5) wraps the configured retriever in
    the graph-aware layer (exception expansion + element-governed
    candidates; zero added LLM calls). The wrapping order is fixed:
    graph wraps CRAG, never the reverse — the graph layer expands and
    re-fuses whatever the corrective loop finally settled on. Paths are
    configurable via REGULATION_GRAPH (default data/regulation_graph.graphml)
    and MABHAS_CLAUSES_FILE (default data/mabhas_clauses_contextual.json).
    """
    db_url = os.environ.get("DATABASE_URL", DEFAULT_DB_URL)
    if os.environ.get("CRAG_ENABLED", "1") == "1":
        # Local import: crag_retriever type-hints against this module.
        from rag.crag_retriever import CorrectiveRetriever

        retriever: Any = CorrectiveRetriever(MabhasRetriever(db_url))
    else:
        retriever = _DefaultRetriever(db_url)

    if os.environ.get("GRAPH_ENABLED", "1") == "1":
        # Local imports: keep the graph stack out of the import path of
        # deployments that never enable it.
        from services.graph_linker import GraphLinker
        from rag.graph_retriever import (
            GraphRetriever, load_clauses_by_id, resolve_clauses_file)

        retriever = GraphRetriever(
            base=retriever,
            linker=GraphLinker(os.environ.get(
                "REGULATION_GRAPH", "data/regulation_graph.graphml")),
            clauses_by_id=load_clauses_by_id(os.environ.get(
                "MABHAS_CLAUSES_FILE") or resolve_clauses_file()),
        )
    return retriever
