"""
rag/agentrouter_client.py
=========================
AgentRouter chat client — the OpenAI-compatible gateway at agentrouter.org.

AgentRouter routes one API key to many upstream models (Claude, GPT,
DeepSeek, GLM, …) behind an OpenAI-compatible surface:

    base URL   https://agentrouter.org/v1
    endpoint   POST /v1/chat/completions
    auth       Authorization: Bearer sk-...      (key from /console/token)

Environment
-----------
    AGENTROUTER_API_KEY   the sk-... token. AGENT_ROUTER_TOKEN is accepted
                          as an alias — that is the variable name
                          AgentRouter's own Codex guide tells users to set,
                          so a key configured for their CLI tools is reused
                          here unchanged.
    AGENTROUTER_BASE_URL  default https://agentrouter.org/v1
    AGENTROUTER_MODEL     default claude-sonnet-4-5-20250929 (the model
                          AgentRouter's guides recommend as the balance of
                          speed and reasoning; override freely — the gateway
                          is a passthrough router)

Design notes
------------
* Uses the ``openai`` SDK, which is ALREADY a project dependency (the
  offline Mabhas classification step) — no new requirement.
* Everything is lazy (the C2 lesson): this module imports cleanly with
  neither the SDK installed nor a key present; both are resolved on the
  first actual call.
* No custom retry loop: unlike Groq we hold ONE key (nothing to rotate),
  and the OpenAI SDK already retries 429/5xx with exponential backoff.
  AGENTROUTER_MAX_RETRIES (default 3) and AGENTROUTER_TIMEOUT_S
  (default 60) tune that.
* ``max_completion_tokens`` is kept as the parameter NAME for seam
  compatibility with groq_chat, but is sent on the wire as ``max_tokens``
  — the classic field every OpenAI-compatible gateway accepts for every
  routed model family.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://agentrouter.org/v1"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

_LOCK = threading.Lock()
_CLIENT: Any = None
_CLIENT_SIG: Optional[tuple] = None  # (key, base_url) the cached client was built with


def _key() -> str:
    """Resolve the API key lazily. Raises only when a call is attempted."""
    key = (os.environ.get("AGENTROUTER_API_KEY")
           or os.environ.get("AGENT_ROUTER_TOKEN") or "").strip()
    if not key:
        raise RuntimeError(
            "Set AGENTROUTER_API_KEY (or AGENT_ROUTER_TOKEN) to your "
            "AgentRouter sk-... token before calling agentrouter_chat. "
            "Get one at https://agentrouter.org/console/token")
    return key


def _base_url() -> str:
    return os.environ.get("AGENTROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("AGENTROUTER_MODEL", DEFAULT_MODEL)


def _make_client() -> Any:
    """Construct the OpenAI-SDK client (test seam — monkeypatched in tests)."""
    from openai import OpenAI  # lazy: SDK only required when a call is made
    return OpenAI(
        api_key=_key(),
        base_url=_base_url(),
        timeout=float(os.environ.get("AGENTROUTER_TIMEOUT_S", "60")),
        max_retries=int(os.environ.get("AGENTROUTER_MAX_RETRIES", "3")),
    )


def _client() -> Any:
    """One cached client per (key, base_url); rebuilt if either changes."""
    global _CLIENT, _CLIENT_SIG
    sig = (_key(), _base_url())
    with _LOCK:
        if _CLIENT is None or _CLIENT_SIG != sig:
            _CLIENT = _make_client()
            _CLIENT_SIG = sig
        return _CLIENT


def agentrouter_chat(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_completion_tokens: int = 4096,
    top_p: float = 0.95,
) -> str:
    """Call AgentRouter chat completions. Returns the completion text.

    Same call shape as rag.groq_client.groq_chat: pass messages and a token
    budget; model/temperature default to the sanctioned project config
    (AGENTROUTER_MODEL @ temperature 0.3). Returns "" on an empty
    completion. Network/auth errors propagate (the SDK has already retried
    429/5xx internally) — callers that must not raise (query transforms,
    the interpretive pass) already catch per call site.
    """
    resp = _client().chat.completions.create(
        model=model or _model(),
        messages=messages,
        temperature=temperature,
        max_tokens=max_completion_tokens,   # universal OpenAI-compat field
        top_p=top_p,
        stream=False,
    )
    choices = getattr(resp, "choices", None) or []
    if not choices:
        logger.warning("AgentRouter returned no choices (model=%s)",
                       model or _model())
        return ""
    return (choices[0].message.content or "").strip()


def reset_client_for_tests() -> None:
    """Drop the cached client so tests can vary env between cases."""
    global _CLIENT, _CLIENT_SIG
    with _LOCK:
        _CLIENT, _CLIENT_SIG = None, None