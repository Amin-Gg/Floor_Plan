"""
tests/test_model_tiers.py
=========================
Pins the two-tier AgentRouter model policy (operator decision, 2026-07):

    search tier  → GLM (default glm-5.2)         CRAG query transforms
    final tier   → claude-opus-4-8               reviewer-facing advisory pass

Resolution order per tier: LLM_<TIER>_MODEL env > AGENTROUTER_MODEL (legacy
global pin) > tier default. The Groq path ignores tiers. Purely ADDITIVE.
"""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import rag.llm_client as lc                                           # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LLM_PROVIDER", "LLM_SEARCH_MODEL", "LLM_FINAL_MODEL",
                "AGENTROUTER_MODEL", "AGENTROUTER_API_KEY",
                "AGENT_ROUTER_TOKEN", "GROQ_API_KEYS", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_tier_defaults():
    assert lc.resolve_model("search") == "glm-5.2"
    assert lc.resolve_model("final") == "claude-opus-4-8"


def test_tier_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_SEARCH_MODEL", "glm-4.5-air")
    monkeypatch.setenv("LLM_FINAL_MODEL", "claude-sonnet-4-5-20250929")
    assert lc.resolve_model("search") == "glm-4.5-air"
    assert lc.resolve_model("final") == "claude-sonnet-4-5-20250929"


def test_legacy_global_pin_between_tier_env_and_default(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_MODEL", "gpt-5")
    assert lc.resolve_model("search") == "gpt-5"
    assert lc.resolve_model("final") == "gpt-5"
    monkeypatch.setenv("LLM_FINAL_MODEL", "claude-opus-4-8")
    assert lc.resolve_model("final") == "claude-opus-4-8"   # tier env wins
    assert lc.resolve_model("search") == "gpt-5"


def test_unknown_tier_is_loud():
    with pytest.raises(ValueError, match="tier"):
        lc.resolve_model("premium")


def test_dispatch_sends_tier_model_to_agentrouter(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    seen = {}

    def fake_chat(messages, model=None, max_completion_tokens=4096, **kw):
        seen["model"] = model
        return "ok"

    import rag.agentrouter_client as arc
    monkeypatch.setattr(arc, "agentrouter_chat", fake_chat)
    lc.llm_chat([{"role": "user", "content": "x"}], tier="search")
    assert seen["model"] == "glm-5.2"
    lc.llm_chat([{"role": "user", "content": "x"}], tier="final")
    assert seen["model"] == "claude-opus-4-8"


def test_groq_env_pin_resolves_to_none(monkeypatch):
    """Groq removal (2026-07): the old groq pin must not resolve, and tier
    resolution stays a pure agentrouter concern."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    import rag.llm_client as lc
    assert lc.resolve_provider() is None
