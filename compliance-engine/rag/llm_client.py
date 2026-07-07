"""
rag/llm_client.py
=================
Provider-agnostic LLM seam: ONE function, llm_chat(), routed to whichever
provider is configured. Everything in the engine that talks to an LLM (the
CRAG query transforms, the interpretive advisory pass) calls this seam;
provider choice is configuration, not code.

Providers
---------
    groq          rag.groq_client.groq_chat — the 9-key rotating pool,
                  qwen/qwen3-32b (the original Stage 2 configuration).
    agentrouter   rag.agentrouter_client.agentrouter_chat — the
                  OpenAI-compatible gateway at agentrouter.org
                  (AGENTROUTER_MODEL, default claude-sonnet-4-5-20250929).

Selection (resolve_provider)
----------------------------
    LLM_PROVIDER=groq | agentrouter    explicit pin
    LLM_PROVIDER=auto (default)        agentrouter if AGENTROUTER_API_KEY /
                                       AGENT_ROUTER_TOKEN is set, else groq
                                       if GROQ_API_KEYS / GROQ_API_KEY is
                                       set, else None (no LLM configured —
                                       the engine's fully-offline mode).

    An unrecognised LLM_PROVIDER value resolves to None with a LOUD warning
    (never a guess): a typo must switch the interpretive pass off visibly,
    not silently fall through to an unintended provider. Deterministic
    PASS/FAIL verdicts never depend on any of this.

reasoning_effort is a Groq/qwen3-specific knob: it is forwarded to the groq
provider and dropped for agentrouter (OpenAI-compatible gateways reject
unknown fields for routed models).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_VALID = ("groq", "agentrouter")


def _has_agentrouter_key() -> bool:
    return bool(os.environ.get("AGENTROUTER_API_KEY")
                or os.environ.get("AGENT_ROUTER_TOKEN"))


def _has_groq_key() -> bool:
    return bool(os.environ.get("GROQ_API_KEYS")
                or os.environ.get("GROQ_API_KEY"))


def resolve_provider() -> Optional[str]:
    """Return 'groq' | 'agentrouter' | None per the rules in the docstring."""
    raw = os.environ.get("LLM_PROVIDER", "auto").strip().lower()
    if raw in _VALID:
        return raw
    if raw != "auto":
        logger.warning(
            "LLM_PROVIDER=%r is not one of %s (or 'auto') — treating as NO "
            "provider so the misconfiguration is visible. Interpretive "
            "clauses will stay NEEDS_REVIEW.", raw, list(_VALID))
        return None
    if _has_agentrouter_key():
        return "agentrouter"
    if _has_groq_key():
        return "groq"
    return None


def provider_status() -> dict:
    """Small status view (health endpoints / logs)."""
    provider = resolve_provider()
    status = {"provider": provider,
              "agentrouter_key_present": _has_agentrouter_key(),
              "groq_key_present": _has_groq_key()}
    if provider == "agentrouter":
        from rag.agentrouter_client import _model
        status["model"] = _model()
    elif provider == "groq":
        status["model"] = "qwen/qwen3-32b"
    return status


def llm_chat(
    messages: list[dict],
    max_completion_tokens: int = 4096,
    reasoning_effort: str = "default",
    provider: Optional[str] = None,
) -> str:
    """Route one chat completion to the configured provider.

    Call shape matches what the transforms and the interpretive pass need:
    messages + a token budget. Model and temperature stay each provider's
    sanctioned defaults (Groq: qwen/qwen3-32b @ 0.3; AgentRouter:
    AGENTROUTER_MODEL @ 0.3) — configuration points, not call-site knobs.
    ``provider`` overrides resolution per call (a hook for split routing,
    e.g. transforms on Groq while advisory notes use AgentRouter).
    """
    chosen = provider or resolve_provider()
    if chosen == "agentrouter":
        from rag.agentrouter_client import agentrouter_chat
        return agentrouter_chat(messages,
                                max_completion_tokens=max_completion_tokens)
    if chosen == "groq":
        from rag.groq_client import groq_chat
        return groq_chat(messages,
                         max_completion_tokens=max_completion_tokens,
                         reasoning_effort=reasoning_effort)
    raise RuntimeError(
        "No LLM provider configured. Set AGENTROUTER_API_KEY (or "
        "GROQ_API_KEYS), or pin one explicitly with "
        "LLM_PROVIDER=agentrouter|groq.")