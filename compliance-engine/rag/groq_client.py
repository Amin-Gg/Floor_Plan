"""
services/groq_client.py
=======================
Key-rotating Groq client — qwen/qwen3-32b, streaming mode.

Set GROQ_API_KEYS to a comma-separated list of all 9 Groq keys in the
environment before importing this module.  On any 429 (rate-limit or
daily-limit) response the client transparently rotates to the next key,
waits 3 seconds for the new key's per-minute window to settle, then
retries.  All 9 keys are tried before giving up.

Usage (call this instead of constructing Groq() directly):

    from rag.groq_client import groq_chat

    text = groq_chat(
        messages=[{"role": "user", "content": "..."}],
    )
"""

from __future__ import annotations

import os
import time

from groq import Groq

# ---------------------------------------------------------------------------
# Key pool — resolved LAZILY on first use, never at import time.
#
# Review fix (issue: import-time raise): this module is imported transitively
# by rag.query_transforms → rag.rag_retriever, so an import-time RuntimeError
# made even pure vector/hybrid retrieval (which never calls Groq) unimportable
# without keys, silently stripped RAG citations from production reports via
# the orchestrator's best-effort retriever factory, and aborted pytest
# collection for the whole suite. Keys are now validated inside _keys() the
# first time a Groq call is actually attempted.
# ---------------------------------------------------------------------------

_KEYS: list[str] | None = None  # populated by _keys() on first Groq call


def _keys() -> list[str]:
    """Return the key pool, reading the env on first call. Raises only then."""
    global _KEYS
    if _KEYS is None:
        _raw = os.environ.get("GROQ_API_KEYS", os.environ.get("GROQ_API_KEY", ""))
        parsed = [k.strip() for k in _raw.split(",") if k.strip()]
        if not parsed:
            raise RuntimeError(
                "Set GROQ_API_KEYS='key1,key2,...' (or GROQ_API_KEY) before "
                "calling the Groq-backed query transforms."
            )
        _KEYS = parsed
    return _KEYS

# Current key index — module-level mutable state (single process, single thread).
_idx: int = 0

# Cache one Groq client per key so we don't reconstruct on every call.
_clients: dict[str, Groq] = {}


def _current_client() -> Groq:
    keys = _keys()
    key = keys[_idx % len(keys)]
    if key not in _clients:
        _clients[key] = Groq(api_key=key)
    return _clients[key]


def _rotate(reason: str = "") -> None:
    global _idx
    _idx += 1
    keys = _keys()
    active = _idx % len(keys)
    print(f"[groq_client] key rotated → index {active}/{len(keys) - 1}"
          + (f"  ({reason})" if reason else ""))


# ---------------------------------------------------------------------------
# Public call — use everywhere instead of client.chat.completions.create()
# ---------------------------------------------------------------------------

def groq_chat(
    messages: list[dict],
    model: str = "qwen/qwen3-32b",
    temperature: float = 0.3,
    max_completion_tokens: int = 4096,
    top_p: float = 0.95,
    reasoning_effort: str = "default",
) -> str:
    """Call Groq chat completions (streaming) with automatic key rotation on 429.

    Streams the response internally and returns the fully assembled text.
    Returns "" if the model produced an empty completion.
    Raises RuntimeError only after all keys have been tried.
    """
    last_exc: Exception | None = None

    for attempt in range(len(_keys())):
        try:
            stream = _current_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
                top_p=top_p,
                reasoning_effort=reasoning_effort,
                stream=True,
                stop=None,
            )
            # Collect all streamed chunks into a single string.
            text = ""
            for chunk in stream:
                text += chunk.choices[0].delta.content or ""
            return text.strip()

        except Exception as exc:
            err = str(exc).lower()
            is_limit = any(
                token in err
                for token in (
                    "429",
                    "rate_limit",
                    "rate limit",
                    "quota",
                    "daily",
                    "limit exceeded",
                    "too many requests",
                )
            )
            if is_limit:
                last_exc = exc
                _rotate(reason=f"429 on attempt {attempt + 1}")
                time.sleep(3)
                continue
            # Non-limit error (auth, network, model error) — re-raise immediately.
            raise

    raise RuntimeError(
        f"All {len(_keys())} Groq keys returned 429 for this request. "
        "Daily budgets may be exhausted — try again tomorrow."
    ) from last_exc


# ---------------------------------------------------------------------------
# Convenience: reset to key 0 between eval runs (optional)
# ---------------------------------------------------------------------------

def reset_key_index() -> None:
    """Reset rotation to key 0. Call between Phase 6 steps if desired."""
    global _idx
    _idx = 0
    print(f"[groq_client] key index reset to 0 ({len(_keys())} keys available)")