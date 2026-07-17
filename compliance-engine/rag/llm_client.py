"""
rag/llm_client.py
=================
Provider seam for the engine's ONE LLM gateway: AgentRouter. Everything that
talks to an LLM (the CRAG query transforms, the interpretive advisory pass)
calls llm_chat(); model choice per role is configuration, not code.

Groq removal (operator decision, 2026-07)
-----------------------------------------
Groq was dropped entirely — no fallback, no rotating key pool, no
reasoning_effort knob. rag/groq_client.py is gone and GROQ_* environment
variables are ignored. The seam is kept (llm_chat, resolve_provider) so a
second provider can be reintroduced later without touching callers; the
``reasoning_effort`` parameter is retained in the signature for caller
compatibility and is dropped (AgentRouter's OpenAI-compatible gateway rejects
unknown fields for routed models).

Selection (resolve_provider)
----------------------------
    LLM_PROVIDER=agentrouter    explicit pin
    LLM_PROVIDER=auto (default) agentrouter if AGENTROUTER_API_KEY /
                                AGENT_ROUTER_TOKEN is set, else None (no LLM
                                configured — the engine's fully-offline mode).

    An unrecognised LLM_PROVIDER value resolves to None with a LOUD warning
    (never a guess): a typo must switch the interpretive pass off visibly,
    not silently fall through to an unintended provider. Deterministic
    PASS/FAIL verdicts never depend on any of this.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_VALID = ("agentrouter",)

# ── Model tiers (operator decision, 2026-07) ────────────────────────────────
# Two AgentRouter-routed roles with different cost/quality needs:
#   search  CRAG query transforms (HyDE / step-back / multi-query). Fires on
#           every low-confidence retrieval → high call volume, short outputs,
#           quality bar is "good query rewrite". Cheap model: GLM.
#   final   the reviewer-facing interpretive advisory pass. Low volume (one
#           call per NEEDS_REVIEW clause), lands verbatim in the municipal
#           report. Strongest model: claude-opus-4-8.
# Resolution per tier: LLM_<TIER>_MODEL env > AGENTROUTER_MODEL (legacy
# global pin) > the defaults below.
_TIER_DEFAULTS = {
    "search": "glm-5.2",
    "final": "claude-opus-4-8",
}


def resolve_model(tier: str) -> str:
    """Resolve the AgentRouter model id for a tier ('search' | 'final')."""
    if tier not in _TIER_DEFAULTS:
        raise ValueError(f"Unknown LLM tier {tier!r}; expected one of "
                         f"{sorted(_TIER_DEFAULTS)}")
    return (os.environ.get(f"LLM_{tier.upper()}_MODEL")
            or os.environ.get("AGENTROUTER_MODEL")
            or _TIER_DEFAULTS[tier])


def _has_agentrouter_key() -> bool:
    return bool(os.environ.get("AGENTROUTER_API_KEY")
                or os.environ.get("AGENT_ROUTER_TOKEN"))


def resolve_provider() -> Optional[str]:
    """Return 'agentrouter' | None per the rules in the module docstring."""
    raw = os.environ.get("LLM_PROVIDER", "auto").strip().lower()
    if raw in _VALID:
        return raw
    if raw != "auto":
        logger.warning(
            "LLM_PROVIDER=%r is not one of %s (or 'auto') — treating as NO "
            "provider so the misconfiguration is visible. Interpretive "
            "clauses will stay NEEDS_REVIEW. (Note: 'groq' was removed "
            "2026-07; AgentRouter is the only provider.)", raw, list(_VALID))
        return None
    if _has_agentrouter_key():
        return "agentrouter"
    return None


def provider_status() -> dict:
    """Small status view (health endpoints / logs)."""
    provider = resolve_provider()
    status = {"provider": provider,
              "agentrouter_key_present": _has_agentrouter_key()}
    if provider == "agentrouter":
        status["model_search"] = resolve_model("search")
        status["model_final"] = resolve_model("final")
    return status


def llm_chat(
    messages: list[dict],
    max_completion_tokens: int = 4096,
    reasoning_effort: str = "default",   # retained for caller compat; dropped
    provider: Optional[str] = None,
    tier: str = "final",
) -> str:
    """Route one chat completion to AgentRouter.

    Call shape matches what the transforms and the interpretive pass need:
    messages + a token budget. ``tier`` selects the model per the two-tier
    policy (search → GLM, final → claude-opus-4-8; see resolve_model).
    Temperature stays the provider's sanctioned default (0.3). ``provider``
    overrides resolution per call ('agentrouter' is the only valid value).
    """
    chosen = provider or resolve_provider()
    if chosen == "agentrouter":
        from rag.agentrouter_client import agentrouter_chat
        return agentrouter_chat(messages,
                                model=resolve_model(tier),
                                max_completion_tokens=max_completion_tokens)
    if chosen == "groq":
        raise RuntimeError(
            "Groq was removed from this engine (2026-07). Set "
            "LLM_PROVIDER=agentrouter (or unset it) and configure "
            "AGENTROUTER_API_KEY.")
    raise RuntimeError(
        "No LLM provider configured. Set AGENTROUTER_API_KEY (or "
        "AGENT_ROUTER_TOKEN), or pin explicitly with "
        "LLM_PROVIDER=agentrouter.")
