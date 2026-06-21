"""
services/embeddings.py
======================
Thin, safe wrapper around the sentence-embedding model.

Stage 1 / Step 6: the model is selectable via the EMBED_MODEL environment
variable so the e5 vs BGE-m3 ablation needs no code changes:

    EMBED_MODEL unset / "intfloat/multilingual-e5-large"  (default)
    EMBED_MODEL="BAAI/bge-m3"

TWO BACKEND MODES
-----------------
If HF_TOKEN is set in the environment, embeddings are computed via the
HuggingFace Inference API (no 2 GB model download required).
If HF_TOKEN is not set, the original local SentenceTransformer path is used.

Both modes produce identical 1024-dim L2-normalised vectors and expose the
same public API: MODEL_NAME, EMBEDDING_DIM, embed_passages(), embed_query().

HF API notes
------------
- feature_extraction may return token-level (n, seq_len, dim) or sentence-
  level (n, dim) embeddings depending on the model configuration.  We
  detect the shape and mean-pool when needed, then L2-normalise, so the
  output is always a flat 1024-float vector regardless of what the API
  returns.
- e5 models REQUIRE "passage: " / "query: " prefixes; BGE-m3 must NOT
  receive them.  The decision is derived from MODEL_NAME in one place
  (_E5_PREFIXES) and applied identically in both backends.
- Batching: the HF API is called in chunks of HF_BATCH_SIZE (default 8)
  to stay within request-size limits.  Adjust with HF_BATCH_SIZE env var.
- Rate-limit retry: on HTTP 429 the code sleeps 30 s and retries up to 3
  times before re-raising.

QUERY/PASSAGE CONSISTENCY GUARANTEE
------------------------------------
MODEL_NAME is read exactly once at import.  Both ingest and the retriever
import this same module, so within one process the query-side and passage-
side model can never diverge.  Across processes, export the same EMBED_MODEL
for ingest and for retrieval — an index embedded with model A and queried
with model B returns silent garbage.  The eval harness records MODEL_NAME
in every run_config and rag_index prints it at the start of every ingest.
"""

from __future__ import annotations

import os
import threading
import time
from typing import List

# ---------------------------------------------------------------------------
# Constants — derived once at import from environment variables
# ---------------------------------------------------------------------------

MODEL_NAME: str = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")
EMBEDDING_DIM: int = 1024

# e5-family models need "query: "/"passage: " prefixes; BGE-m3 must NOT.
_E5_PREFIXES: bool = "e5" in MODEL_NAME.lower()

# Use HF Inference API when HF_TOKEN is present; local model otherwise.
_USE_HF_API: bool = bool(os.environ.get("HF_TOKEN"))

# Batch size for HF API calls (keep small to avoid request-size limits).
_HF_BATCH_SIZE: int = int(os.environ.get("HF_BATCH_SIZE", "8"))

# ---------------------------------------------------------------------------
# Prefix helpers (identical for both backends)
# ---------------------------------------------------------------------------

def _passage_input(text: str) -> str:
    return f"passage: {text}" if _E5_PREFIXES else text


def _query_input(text: str) -> str:
    return f"query: {text}" if _E5_PREFIXES else text


# ---------------------------------------------------------------------------
# HF Inference API backend
# ---------------------------------------------------------------------------

def _hf_embed_batch(texts: List[str]) -> List[List[float]]:
    """
    Call HF feature_extraction for one batch of texts.

    Handles all shape variants the API may return:
      (n, dim)          — sentence-level, use directly
      (n, seq_len, dim) — token-level, mean-pool over seq_len
      (seq_len, dim)    — single-text token-level, mean-pool
      (dim,)            — single-text sentence-level, wrap

    Always L2-normalises before returning.
    """
    import numpy as np
    from huggingface_hub import InferenceClient

    client = InferenceClient(
        provider="hf-inference",
        api_key=os.environ["HF_TOKEN"],
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            raw = client.feature_extraction(texts, model=MODEL_NAME)
            break
        except Exception as exc:
            err = str(exc)
            is_rate_limit = (
                "429" in err
                or "rate limit" in err.lower()
                or "too many" in err.lower()
            )
            if is_rate_limit and attempt < max_retries - 1:
                wait = 30 * (attempt + 1)
                print(
                    f"  [HF rate limit] sleeping {wait}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
            else:
                raise

    n = len(texts)
    arr = np.array(raw, dtype=float)

    # Normalise shape to (n, dim)
    if arr.ndim == 1:
        # Single sentence-level vector → (1, dim)
        arr = arr.reshape(1, -1)
    elif arr.ndim == 2:
        if arr.shape[0] == n:
            # (n, dim) — already sentence-level
            pass
        else:
            # (seq_len, dim) — single text, token-level → mean-pool → (1, dim)
            arr = arr.mean(axis=0, keepdims=True)
    elif arr.ndim == 3:
        # (n, seq_len, dim) — token-level batch → mean-pool → (n, dim)
        arr = arr.mean(axis=1)
    else:
        raise ValueError(
            f"Unexpected embedding shape from HF API: {arr.shape} "
            f"for {n} input texts."
        )

    if arr.shape[0] != n:
        raise ValueError(
            f"HF API returned {arr.shape[0]} vectors for {n} inputs."
        )

    # L2-normalise every row
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    arr = arr / norms

    return [row.tolist() for row in arr]


def _hf_embed_passages(texts: List[str]) -> List[List[float]]:
    """Batch embed passages via HF API with e5 prefix handling."""
    if not texts:
        return []
    prepared = [_passage_input(t) for t in texts]
    results: List[List[float]] = []
    for i in range(0, len(prepared), _HF_BATCH_SIZE):
        chunk = prepared[i: i + _HF_BATCH_SIZE]
        results.extend(_hf_embed_batch(chunk))
        if i + _HF_BATCH_SIZE < len(prepared):
            time.sleep(0.5)  # polite pause between batches
    return results


def _hf_embed_query(text: str) -> List[float]:
    """Embed a single query via HF API with e5 prefix handling."""
    vecs = _hf_embed_batch([_query_input(text)])
    return vecs[0]


# ---------------------------------------------------------------------------
# Local SentenceTransformer backend (original implementation)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()


def get_model():
    """Return the shared SentenceTransformer instance, loading it once."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def _local_embed_passages(texts: List[str], batch_size: int = 16) -> List[List[float]]:
    if not texts:
        return []
    model = get_model()
    prepared = [_passage_input(t) for t in texts]
    vectors = model.encode(
        prepared,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


def _local_embed_query(text: str) -> List[float]:
    model = get_model()
    vector = model.encode(
        [_query_input(text)],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vector[0].tolist()


# ---------------------------------------------------------------------------
# Public API — identical regardless of backend
# ---------------------------------------------------------------------------

def embed_passages(texts: List[str], batch_size: int = 16) -> List[List[float]]:
    """
    Embed a list of regulation passages (documents to be stored).

    Parameters
    ----------
    texts : list[str]
        Raw passage texts. The model-appropriate convention (e5 "passage: "
        prefix, or raw text for BGE-m3) is applied automatically.
    batch_size : int
        Encoding batch size. Used by the local backend only; the HF API
        backend uses HF_BATCH_SIZE (default 8).

    Returns
    -------
    list[list[float]]
        One 1024-float vector per input, L2-normalised.
    """
    if _USE_HF_API:
        return _hf_embed_passages(texts)
    return _local_embed_passages(texts, batch_size=batch_size)


def embed_query(text: str) -> List[float]:
    """
    Embed a single search query (e.g. "minimum bedroom area").

    The model-appropriate convention (e5 "query: " prefix, or raw text
    for BGE-m3) is applied automatically.
    """
    if not text or not text.strip():
        raise ValueError("embed_query received an empty query string")
    if _USE_HF_API:
        return _hf_embed_query(text)
    return _local_embed_query(text)