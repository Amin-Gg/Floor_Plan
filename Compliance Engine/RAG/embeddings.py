"""
services/embeddings.py
======================
Thin, safe wrapper around the multilingual-e5-large sentence embedding model.

Why this file exists
--------------------
Two things about e5 models cause silent quality bugs if you get them wrong,
so they are centralised here and never duplicated elsewhere:

  1. e5 REQUIRES a task prefix on every input:
         passages (documents) must be prefixed with "passage: "
         queries  must be prefixed with "query: "
     Forgetting the prefix does not crash — it just quietly halves retrieval
     quality. We hide it inside embed_passages() / embed_query() so callers
     can never forget it.

  2. For cosine similarity (which our pgvector index uses) embeddings MUST be
     L2-normalized. We pass normalize_embeddings=True everywhere.

The model is loaded lazily and only once per process (it is ~2 GB), guarded by
a lock so concurrent web workers don't each trigger a load.
"""

from __future__ import annotations

import threading
from typing import List

# intfloat/multilingual-e5-large -> 1024-dim embeddings, strong Persian + English.
MODEL_NAME: str = "intfloat/multilingual-e5-large"
EMBEDDING_DIM: int = 1024

_model = None
_model_lock = threading.Lock()


def get_model():
    """Return the shared SentenceTransformer instance, loading it once."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # double-checked locking
                # Imported here so importing this module is cheap and does not
                # pull in torch until embeddings are actually needed.
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_passages(texts: List[str], batch_size: int = 16) -> List[List[float]]:
    """
    Embed a list of regulation passages (documents to be stored).

    Parameters
    ----------
    texts : list[str]
        Raw passage texts. The "passage: " prefix is added automatically.
    batch_size : int
        Encoding batch size. Lower it if you hit GPU/CPU memory limits.

    Returns
    -------
    list[list[float]]
        One 1024-float vector per input, L2-normalized.
    """
    if not texts:
        return []
    model = get_model()
    prefixed = [f"passage: {t}" for t in texts]
    vectors = model.encode(
        prefixed,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


def embed_query(text: str) -> List[float]:
    """
    Embed a single search query (e.g. "minimum bedroom area").

    The "query: " prefix is added automatically.
    """
    if not text or not text.strip():
        raise ValueError("embed_query received an empty query string")
    model = get_model()
    vector = model.encode(
        [f"query: {text}"],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vector[0].tolist()
