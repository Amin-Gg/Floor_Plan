"""
tests/test_llm_provider.py
==========================
Tests for the LLM provider integration (2026-07): the provider dispatcher
(rag/llm_client.py) and the AgentRouter client (rag/agentrouter_client.py).
Purely ADDITIVE. No network calls — the OpenAI-SDK construction seam
(_make_client) is monkeypatched with a fake.

Covers:
  * resolve_provider(): auto rules, explicit pin, unknown value → None
    (loud, never a guess), no keys → None
  * llm_chat() dispatch: routes to the right client; reasoning_effort is
    forwarded to Groq and DROPPED for AgentRouter; no provider → clear error
  * agentrouter_chat(): key resolution from BOTH env names, sanctioned
    request parameters on the wire (model, temperature 0.3, max_tokens,
    stream=False), empty completion → "", keyless call → RuntimeError
  * _build_llm() end-to-end with an AgentRouter key present
"""

from __future__ import annotations

import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rag.agentrouter_client as arc                                  # noqa: E402
import rag.llm_client as lc                                           # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with no provider config and no cached client."""
    for var in ("LLM_PROVIDER", "GROQ_API_KEYS", "GROQ_API_KEY",
                "AGENTROUTER_API_KEY", "AGENT_ROUTER_TOKEN",
                "AGENTROUTER_BASE_URL", "AGENTROUTER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    arc.reset_client_for_tests()
    yield
    arc.reset_client_for_tests()


# ═══════════════════════════════════════════════════════════════════════════
# resolve_provider
# ═══════════════════════════════════════════════════════════════════════════

def test_resolve_none_without_keys():
    assert lc.resolve_provider() is None


def test_resolve_auto_prefers_agentrouter(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    assert lc.resolve_provider() == "agentrouter"


def test_resolve_auto_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    assert lc.resolve_provider() == "groq"


def test_resolve_accepts_codex_style_token_name(monkeypatch):
    """AGENT_ROUTER_TOKEN (AgentRouter's own Codex-guide variable) works."""
    monkeypatch.setenv("AGENT_ROUTER_TOKEN", "sk-dummy")
    assert lc.resolve_provider() == "agentrouter"


def test_resolve_explicit_pin_wins(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert lc.resolve_provider() == "groq"


def test_resolve_unknown_value_is_loud_none(monkeypatch, caplog):
    """A typo must switch the pass OFF visibly, never guess a provider."""
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setenv("LLM_PROVIDER", "agentruter")   # typo on purpose
    with caplog.at_level("WARNING"):
        assert lc.resolve_provider() is None
    assert "agentruter" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# llm_chat dispatch
# ═══════════════════════════════════════════════════════════════════════════

_MSGS = [{"role": "user", "content": "hi"}]


def test_dispatch_routes_to_agentrouter(monkeypatch):
    seen = {}

    def fake_ar(messages, max_completion_tokens=4096):
        seen.update(messages=messages, budget=max_completion_tokens)
        return "note"

    monkeypatch.setattr(arc, "agentrouter_chat", fake_ar)
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    out = lc.llm_chat(_MSGS, max_completion_tokens=300,
                      reasoning_effort="none")
    assert out == "note"
    assert seen["budget"] == 300           # reasoning_effort was DROPPED
    assert seen["messages"] == _MSGS


def test_dispatch_routes_to_groq_with_reasoning_effort(monkeypatch):
    import rag.groq_client as gc
    seen = {}

    def fake_groq(messages, max_completion_tokens=4096,
                  reasoning_effort="default"):
        seen.update(budget=max_completion_tokens, effort=reasoning_effort)
        return "note"

    monkeypatch.setattr(gc, "groq_chat", fake_groq)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    out = lc.llm_chat(_MSGS, max_completion_tokens=300,
                      reasoning_effort="none")
    assert out == "note"
    assert seen["effort"] == "none"        # forwarded to groq
    assert seen["budget"] == 300


def test_dispatch_explicit_provider_arg_overrides_env(monkeypatch):
    calls = []
    monkeypatch.setattr(arc, "agentrouter_chat",
                        lambda *a, **k: calls.append("ar") or "x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")   # env would say groq
    lc.llm_chat(_MSGS, provider="agentrouter")
    assert calls == ["ar"]


def test_dispatch_without_provider_raises_clearly():
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        lc.llm_chat(_MSGS)


# ═══════════════════════════════════════════════════════════════════════════
# agentrouter_chat — request shape and key handling (no network; fake client)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeCompletions:
    def __init__(self, log, content="Advisory note."):
        self._log, self._content = log, content

    def create(self, **kwargs):
        self._log.append(kwargs)
        msg = types.SimpleNamespace(content=self._content)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(
            choices=[] if self._content is None else [choice])


class _FakeClient:
    def __init__(self, log, content="Advisory note."):
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(log, content))


def test_agentrouter_sends_sanctioned_request(monkeypatch):
    log = []
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setattr(arc, "_make_client", lambda: _FakeClient(log))
    out = arc.agentrouter_chat(_MSGS, max_completion_tokens=300)
    assert out == "Advisory note."
    req = log[0]
    assert req["model"] == arc.DEFAULT_MODEL
    assert req["temperature"] == 0.3          # the project-wide sanctioned temp
    assert req["max_tokens"] == 300           # wire name is max_tokens
    assert req["stream"] is False
    assert req["messages"] == _MSGS


def test_agentrouter_model_env_override(monkeypatch):
    log = []
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setenv("AGENTROUTER_MODEL", "deepseek-v3")
    monkeypatch.setattr(arc, "_make_client", lambda: _FakeClient(log))
    arc.agentrouter_chat(_MSGS)
    assert log[0]["model"] == "deepseek-v3"


def test_agentrouter_alias_token_env(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTER_TOKEN", "sk-alias")
    assert arc._key() == "sk-alias"


def test_agentrouter_keyless_call_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="AGENTROUTER_API_KEY"):
        arc.agentrouter_chat(_MSGS)


def test_agentrouter_empty_choices_returns_empty_string(monkeypatch):
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")
    monkeypatch.setattr(arc, "_make_client",
                        lambda: _FakeClient([], content=None))
    assert arc.agentrouter_chat(_MSGS) == ""


def test_agentrouter_client_rebuilt_when_key_changes(monkeypatch):
    built = []
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-one")
    monkeypatch.setattr(arc, "_make_client",
                        lambda: built.append(1) or _FakeClient([]))
    arc.agentrouter_chat(_MSGS)
    arc.agentrouter_chat(_MSGS)
    assert len(built) == 1                     # cached for the same key
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-two")
    arc.agentrouter_chat(_MSGS)
    assert len(built) == 2                     # rebuilt on key change


# ═══════════════════════════════════════════════════════════════════════════
# _build_llm end-to-end with an AgentRouter key
# ═══════════════════════════════════════════════════════════════════════════

def test_build_llm_resolves_agentrouter(monkeypatch):
    import api.pipeline as ap
    ap.reset_llm_wiring_for_tests()
    monkeypatch.setenv("LLM_PASS_ENABLED", "1")
    monkeypatch.setenv("AGENTROUTER_API_KEY", "sk-dummy")

    log = []
    monkeypatch.setattr(arc, "_make_client", lambda: _FakeClient(log))
    llm = ap._get_llm()
    assert callable(llm)
    note = llm("What should a reviewer check?")
    assert note == "Advisory note."
    assert log[0]["max_tokens"] == 300         # the advisory-note budget
    assert "reasoning_effort" not in log[0]    # dropped for agentrouter
    ap.reset_llm_wiring_for_tests()