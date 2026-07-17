"""
rag/qdrant_retriever.py
=======================
Qdrant retrieval backend — a drop-in sibling of the pgvector MabhasRetriever
(2026-07, operator decision: corpus embeddings now live in a Qdrant
deployment; the engine reads them through the same .retrieve() contract).

Contract preserved
------------------
Hit dicts carry the exact keys the engine reads everywhere
(``mabhas_part, article_id, heading_fa, text_fa, text_en, rule_type,
entities, context_fa`` + ``score``), and the class exposes the same surface
the Stage 2/3 wrappers call on their base:

    retrieve(query, top_k, mabhas_part, rule_type)          Graph seed
    hybrid_retrieve(query, top_k, rerank=True, ...)          CRAG initial leg
    dense_retrieve(query, top_k, ...)

so CorrectiveRetriever and GraphRetriever wrap this class UNCHANGED.

Honest architectural delta vs pgvector (documented, not hidden)
---------------------------------------------------------------
pgvector's hybrid leg fuses dense + a BM25-style tsquery leg with RRF before
the cross-encoder. Qdrant holds only dense vectors here, so
``hybrid_retrieve`` is dense **oversampling** (candidate_k, default 50) +
the SAME shared cross-encoder rerank (rag/reranker.py). The reranker is
where most Stage-1 precision came from, but the lexical leg's exact-token
recall (article numbers, Persian technical terms) has no Qdrant equivalent
in this v1 — run ``scripts/qdrant_audit.py --eval`` to measure the delta
against the pgvector baseline (hit@1 0.907 / recall@5 0.919 / MRR 0.936)
before trusting production to this backend. ``lexical_retrieve`` therefore
raises NotImplementedError instead of silently aliasing dense.

Payload-layout tolerance
------------------------
The collection was loaded by an external embedding pipeline (corpus JSON →
embeddings → Chroma → Qdrant), so the payload schema is discovered at first
use from a sampled point rather than assumed:

    flat        {"article_id": ..., "text_fa": ...}
    metadata    {"page_content"/"text": ..., "metadata": {"article_id": ...}}

Field aliases are resolved once and reused; filters get the correct key
path ("rule_type" vs "metadata.rule_type"). A collection with no
recognizable article-id field fails LOUDLY with the observed keys listed —
never a guess (design principle 1).

Environment
-----------
    QDRANT_URL          https://<cluster>.<region>.cloud.qdrant.io:6333
    QDRANT_API_KEY      cluster key (env only — never an argument, never logged)
    QDRANT_COLLECTION   default "mabhas_clauses"
    QDRANT_VECTOR_NAME  only needed when the collection has MULTIPLE named
                        vectors; single/unnamed vectors are auto-detected
    QDRANT_TIMEOUT_S    default 30
    EMBED_MODEL         query embedder — MUST equal the model that built the
                        collection. The current production collection is
                        BAAI/bge-m3-embedded (operator, 2026-07); bge-m3 and
                        the pgvector corpus's e5-large are BOTH 1024-dim, so
                        the dim check below cannot tell them apart — this env
                        var is the single point of truth.

Everything is lazy (the C2 lesson): the module imports with neither
qdrant-client installed nor env configured; both resolve on first use.
The query embedding reuses rag.embeddings.embed_query — the SAME model and
e5 "query: " prefix convention as ingest, and the collection's vector size
is checked against the query vector's dimension on first call (a dimension
mismatch means the collection was embedded with a DIFFERENT model and every
result would be silent garbage — that fails loudly instead).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from rag.embeddings import embed_query
from rag.rag_retriever import _build_rerank_passage

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "mabhas_clauses"

# Canonical hit keys (mirror of rag_retriever._HIT_COLUMNS) and the payload
# aliases seen in common export pipelines (LangChain, Chroma dumps, custom).
_HIT_KEYS = ("mabhas_part", "article_id", "heading_fa", "text_fa", "text_en",
             "rule_type", "entities", "context_fa")
_ALIASES: Dict[str, tuple] = {
    "article_id": ("article_id", "clause_id", "article", "id"),
    "text_fa": ("text_fa", "page_content", "text", "content", "body"),
    "text_en": ("text_en", "translation_en", "english"),
    "heading_fa": ("heading_fa", "heading", "title"),
    "mabhas_part": ("mabhas_part", "part", "mabhas"),
    "rule_type": ("rule_type", "type"),
    "entities": ("entities",),
    "context_fa": ("context_fa", "context"),
}


class QdrantRetriever:
    """Dense retrieval + shared cross-encoder rerank over a Qdrant collection.

    Plain ``retrieve()`` is dense-only (mirroring MabhasRetriever.retrieve,
    the surface the Graph wrapper seeds from); CRAG calls
    ``hybrid_retrieve(rerank=True)`` exactly as it does on the pgvector base.
    """

    def __init__(self,
                 url: Optional[str] = None,
                 collection: Optional[str] = None) -> None:
        # NOTE: the API key is deliberately NOT a constructor argument — it is
        # read from QDRANT_API_KEY inside _client() only, so it can never leak
        # through reprs, logs, or call sites.
        self.url = url or os.environ.get("QDRANT_URL", "")
        self.collection = (collection
                           or os.environ.get("QDRANT_COLLECTION",
                                             DEFAULT_COLLECTION))
        self._lock = threading.Lock()
        self._cl: Any = None
        self._layout: Optional[str] = None      # "flat" | "metadata"
        self._vector_name: Optional[str] = None  # None → unnamed vector
        self._dim_checked = False
        self.last_rerank_seconds: float = 0.0    # parity with pgvector class

    # ------------------------------------------------------------------
    # Client / schema introspection (lazy, once)
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        if self._cl is not None:
            return self._cl
        with self._lock:
            if self._cl is None:
                if not self.url:
                    raise RuntimeError(
                        "QDRANT_URL is not set — point it at your Qdrant "
                        "endpoint (e.g. https://<cluster>.cloud.qdrant.io:6333) "
                        "and put the cluster key in QDRANT_API_KEY.")
                from qdrant_client import QdrantClient  # lazy (C2 lesson)
                self._cl = QdrantClient(
                    url=self.url,
                    api_key=os.environ.get("QDRANT_API_KEY") or None,
                    timeout=float(os.environ.get("QDRANT_TIMEOUT_S", "30")),
                )
            return self._cl

    # test seam: local-mode tests inject a QdrantClient(":memory:") here
    def _set_client_for_tests(self, client: Any) -> None:
        self._cl = client

    def _introspect(self) -> None:
        """Detect payload layout and vector naming from the live collection."""
        if self._layout is not None:
            return
        with self._lock:
            if self._layout is not None:
                return
            cl = self._client()
            info = cl.get_collection(self.collection)

            # Vector naming: unnamed (VectorParams) or named ({name: params}).
            vectors_cfg = info.config.params.vectors
            if isinstance(vectors_cfg, dict):
                names = sorted(vectors_cfg)
                pinned = os.environ.get("QDRANT_VECTOR_NAME")
                if pinned:
                    if pinned not in names:
                        raise RuntimeError(
                            f"QDRANT_VECTOR_NAME={pinned!r} not in the "
                            f"collection's named vectors {names}")
                    self._vector_name = pinned
                elif len(names) == 1:
                    self._vector_name = names[0]
                else:
                    raise RuntimeError(
                        f"Collection {self.collection!r} has multiple named "
                        f"vectors {names}; set QDRANT_VECTOR_NAME to choose "
                        "one (never a guess).")

            # Payload layout from one sampled point.
            points, _ = cl.scroll(self.collection, limit=1, with_payload=True)
            if not points:
                raise RuntimeError(
                    f"Qdrant collection {self.collection!r} is EMPTY — "
                    "nothing to retrieve. Load the corpus first "
                    "(scripts/qdrant_audit.py reports counts).")
            payload = points[0].payload or {}
            if any(a in payload for a in _ALIASES["article_id"] if a != "id"):
                self._layout = "flat"
            elif isinstance(payload.get("metadata"), dict):
                self._layout = "metadata"
            elif "article_id" in payload or "id" in payload:
                self._layout = "flat"
            else:
                raise RuntimeError(
                    "Cannot locate an article-id field in the Qdrant payload. "
                    f"Observed top-level keys: {sorted(payload)}. The engine "
                    "needs article_id (or one of "
                    f"{list(_ALIASES['article_id'])}) per point — re-ingest "
                    "with ids in the payload, or nest them under 'metadata'.")
            logger.info("Qdrant collection %r: layout=%s vector_name=%s",
                        self.collection, self._layout, self._vector_name)
            # Equal dimensions CANNOT prove the same model (e5-large and
            # bge-m3 are both 1024-dim), so the one honest defence beyond the
            # dim check is making the active query embedder VISIBLE next to
            # the collection it queries. This collection is bge-m3-embedded
            # (operator, 2026-07): EMBED_MODEL must be BAAI/bge-m3.
            active = os.environ.get("EMBED_MODEL",
                                    "intfloat/multilingual-e5-large (default)")
            logger.info("Query embedder for collection %r: %s — this MUST be "
                        "the model that built the collection or results are "
                        "silent garbage", self.collection, active)

    # ------------------------------------------------------------------
    # Payload → canonical hit
    # ------------------------------------------------------------------

    def _merged_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._layout == "metadata":
            merged = dict(payload.get("metadata") or {})
            for k, v in payload.items():   # top level (page_content …) wins nothing it shouldn't
                if k != "metadata" and k not in merged:
                    merged[k] = v
            return merged
        return payload

    def _to_hit(self, point: Any) -> Dict[str, Any]:
        merged = self._merged_payload(point.payload or {})
        hit: Dict[str, Any] = {}
        for key in _HIT_KEYS:
            val = None
            for alias in _ALIASES[key]:
                if alias in merged and merged[alias] not in (None, ""):
                    val = merged[alias]
                    break
            hit[key] = val
        # entities may arrive as a JSON string from export pipelines
        if isinstance(hit.get("entities"), str):
            try:
                hit["entities"] = json.loads(hit["entities"])
            except (ValueError, TypeError):
                pass  # keep the string; downstream .get() access stays safe
        if hit["article_id"] is None:
            hit["article_id"] = str(point.id)   # last resort: the point id
        hit["article_id"] = str(hit["article_id"])
        hit["score"] = float(point.score)
        return hit

    def _payload_key(self, field: str) -> str:
        return f"metadata.{field}" if self._layout == "metadata" else field

    def _filter(self, mabhas_part: Optional[str], rule_type: Optional[str]):
        if mabhas_part is None and rule_type is None:
            return None
        from qdrant_client import models
        must = []
        if mabhas_part is not None:
            must.append(models.FieldCondition(
                key=self._payload_key("mabhas_part"),
                match=models.MatchValue(value=mabhas_part)))
        if rule_type is not None:
            must.append(models.FieldCondition(
                key=self._payload_key("rule_type"),
                match=models.MatchValue(value=rule_type)))
        return models.Filter(must=must)

    # ------------------------------------------------------------------
    # Retrieval legs
    # ------------------------------------------------------------------

    def dense_retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top_k points by vector similarity (same scale as pgvector's
        1 - cosine_distance when the collection uses Cosine distance)."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self._introspect()
        qvec = embed_query(query)   # same model + e5 "query: " prefix as ingest

        if not self._dim_checked:
            info = self._client().get_collection(self.collection)
            cfg = info.config.params.vectors
            size = (cfg[self._vector_name].size
                    if isinstance(cfg, dict) else cfg.size)
            if size != len(qvec):
                raise RuntimeError(
                    f"Embedding dimension mismatch: the query embedder "
                    f"({os.environ.get('EMBED_MODEL', 'project default')}) "
                    f"produces {len(qvec)}-dim vectors but collection "
                    f"{self.collection!r} stores {size}-dim vectors. The "
                    "corpus was embedded with a DIFFERENT model — every "
                    "search result would be garbage. Re-embed the corpus or "
                    "set EMBED_MODEL to the model that built the collection.")
            self._dim_checked = True

        resp = self._client().query_points(
            collection_name=self.collection,
            query=qvec,
            using=self._vector_name,
            limit=top_k,
            query_filter=self._filter(mabhas_part, rule_type),
            with_payload=True,
        )
        return [self._to_hit(p) for p in resp.points]

    def lexical_retrieve(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        raise NotImplementedError(
            "The Qdrant backend has no BM25/tsquery leg (dense-only + "
            "cross-encoder rerank). Use the pgvector backend "
            "(VECTOR_BACKEND=pgvector) for lexical-leg experiments.")

    def hybrid_retrieve(
        self,
        query: str,
        top_k: int = 3,
        candidate_k: int = 50,
        rrf_k: int = 60,          # accepted for signature parity; unused
        rerank: bool = False,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Dense oversampling + the shared cross-encoder rerank.

        Signature-compatible with the pgvector hybrid (CRAG calls it with
        rerank=True). Without rerank this is a plain dense top_k.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if candidate_k < top_k:
            raise ValueError("candidate_k must be >= top_k")
        if not rerank:
            return self.dense_retrieve(query, top_k=top_k,
                                       mabhas_part=mabhas_part,
                                       rule_type=rule_type)

        candidates = self.dense_retrieve(query, top_k=candidate_k,
                                         mabhas_part=mabhas_part,
                                         rule_type=rule_type)
        if not candidates:
            return []

        from rag.reranker import rerank as _ce_rerank  # lazy, shared weights
        passages = [_build_rerank_passage(h) for h in candidates]
        t0 = time.perf_counter()
        ce_scores = _ce_rerank(query, passages)
        self.last_rerank_seconds = time.perf_counter() - t0

        for hit, ce in zip(candidates, ce_scores):
            hit["dense_score"] = hit["score"]
            hit["score"] = float(ce)
        candidates.sort(key=lambda h: (-h["score"], h["article_id"]))
        return candidates[:top_k]

    # Backward-compatible public API — same alias the pgvector class exposes.
    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.dense_retrieve(query, top_k=top_k,
                                   mabhas_part=mabhas_part,
                                   rule_type=rule_type)


class QdrantDefaultRetriever(QdrantRetriever):
    """Qdrant twin of _DefaultRetriever: retrieve() = oversample + rerank.

    Used on the non-CRAG factory path so the production configuration
    (rerank always on) is preserved regardless of backend.
    """

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        mabhas_part: Optional[str] = None,
        rule_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.hybrid_retrieve(
            query, top_k=top_k,
            candidate_k=int(os.environ.get("RERANK_CANDIDATE_K", "50")),
            rerank=True, mabhas_part=mabhas_part, rule_type=rule_type)