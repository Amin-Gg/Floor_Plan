"""
services/reranker.py
====================
Thin, safe wrapper around the BAAI/bge-reranker-v2-m3 cross-encoder,
mirroring the style of services/embeddings.py.

What reranking does
-------------------
The bi-encoder (multilingual-e5-large) embeds queries and passages
INDEPENDENTLY into one shared vector space; relevance is approximated by
cosine similarity between two vectors computed without ever seeing each
other. That independence is what makes first-stage retrieval fast — all
passage vectors are precomputed and indexed — but it caps accuracy: the
model must guess what aspects of a passage will matter for queries it has
never seen.

A cross-encoder reads the (query, passage) pair JOINTLY through full
attention and outputs one relevance score. It is far more accurate,
because every query token can attend to every passage token, but it cannot
be indexed: each query requires a fresh forward pass per candidate
passage. Hence the two-stage architecture: the cheap bi-encoder narrows
328 clauses to ~50 candidates, and the expensive cross-encoder re-scores
only those 50.

bge-reranker-v2-m3 is trained multilingually (BGE-M3 family), so it
scores Persian regulation text and English questions natively.

GPU acceleration
----------------
At load time the module detects whether a CUDA GPU is available.
  - GPU found  → model loaded on cuda:0 and converted to fp16.
      bge-reranker-v2-m3 in fp16 uses ~600 MB VRAM for weights plus
      ~400 MB for activations at candidate_k=50, max_length=512.
      An RTX 3050 Laptop (4 GB VRAM) handles this comfortably.
  - No GPU     → falls back to CPU fp32 transparently.
fp16 is safe for inference (no gradients, no training instability);
it halves VRAM usage and roughly doubles throughput vs fp32 on CUDA.

Implementation note
-------------------
Uses sentence_transformers.CrossEncoder — same model weights as
FlagEmbedding.FlagReranker but a stable API with no transformers
version pinning required.

The model is loaded lazily and only once per process, guarded by a
lock so concurrent workers don't each trigger a load.
"""

from __future__ import annotations

import threading
from typing import List

MODEL_NAME = r"C:\Users\Asus\Desktop\bge_reranker"

_reranker = None
_reranker_lock = threading.Lock()


def _select_device() -> str:
    """
    Return 'cuda' if an NVIDIA GPU is available, otherwise 'cpu'.
    Prints a one-line diagnostic so the operator can confirm which
    device is active without reading logs further.
    """
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            print(
                f"[reranker] GPU detected: {name}  "
                f"VRAM: {vram_gb:.1f} GB  →  loading on cuda:0 (fp16)"
            )
            return "cuda"
    except Exception:
        pass
    print("[reranker] No CUDA GPU found → loading on CPU (fp32)")
    return "cpu"


def get_reranker():
    """Return the shared CrossEncoder instance, loading it once."""
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:  # double-checked locking
                from sentence_transformers import CrossEncoder

                device = _select_device()
                _reranker = CrossEncoder(
                    MODEL_NAME,
                    max_length=512,
                    device=device,
                )
                if device == "cuda":
                    # Convert weights to fp16: halves VRAM (~600 MB vs ~1.2 GB)
                    # and speeds up inference. Safe for forward-only scoring.
                    _reranker.model.half()
    return _reranker


def rerank(query: str, passages: List[str]) -> List[float]:
    """
    Score each passage's relevance to `query` with the cross-encoder.

    Parameters
    ----------
    query : str
        The search query.
    passages : list[str]
        Candidate passage texts (typically the fused top candidate_k).

    Returns
    -------
    list[float]
        One relevance score per passage, same order as the input.
        Higher = more relevant. Scores are raw logits (unbounded);
        they are comparable within one call, which is all the
        rerank-then-sort stage needs.
    """
    if not passages:
        return []
    if not query or not query.strip():
        raise ValueError("rerank received an empty query string")

    reranker = get_reranker()
    scores = reranker.predict(
        [[query, p] for p in passages],
        batch_size=32,       # 50 candidates → 2 forward passes; safe at 4 GB VRAM
        show_progress_bar=False,
    )
    # CrossEncoder.predict() returns a numpy array; normalize to list[float].
    if hasattr(scores, "tolist"):
        return scores.tolist()
    if isinstance(scores, float):
        return [scores]
    return [float(s) for s in scores]