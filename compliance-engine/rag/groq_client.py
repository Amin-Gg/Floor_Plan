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
# Key pool — read once at import time
# ---------------------------------------------------------------------------

_raw = os.environ.get("GROQ_API_KEYS", os.environ.get("GROQ_API_KEY", ""))
_KEYS: list[str] = [k.strip() for k in _raw.split(",") if k.strip()]

if not _KEYS:
    raise RuntimeError(
        "Set GROQ_API_KEYS='key1,key2,...,key9' before running the eval."
    )

# Current key index — module-level mutable state (single process, single thread).
_idx: int = 0

# Cache one Groq client per key so we don't reconstruct on every call.
_clients: dict[str, Groq] = {}


def _current_client() -> Groq:
    key = _KEYS[_idx % len(_KEYS)]
    if key not in _clients:
        _clients[key] = Groq(api_key=key)
    return _clients[key]


def _rotate(reason: str = "") -> None:
    global _idx
    _idx += 1
    active = _idx % len(_KEYS)
    print(f"[groq_client] key rotated → index {active}/{len(_KEYS) - 1}"
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

    for attempt in range(len(_KEYS)):
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
        f"All {len(_KEYS)} Groq keys returned 429 for this request. "
        "Daily budgets may be exhausted — try again tomorrow."
    ) from last_exc


# ---------------------------------------------------------------------------
# Convenience: reset to key 0 between eval runs (optional)
# ---------------------------------------------------------------------------

def reset_key_index() -> None:
    """Reset rotation to key 0. Call between Phase 6 steps if desired."""
    global _idx
    _idx = 0
    print(f"[groq_client] key index reset to 0 ({len(_KEYS)} keys available)")