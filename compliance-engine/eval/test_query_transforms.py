"""
Unit tests for rag/query_transforms (HyDE, step-back, multi-query).

Rewritten after the Anthropic -> Groq migration, and again after the
provider dispatcher was introduced: the transforms now make all completions
through rag.llm_client.llm_chat (Groq qwen/qwen3-32b by default; AgentRouter
when configured — model/temperature enforced by each provider client's own
defaults), so these tests mock ``rag.query_transforms.llm_chat`` — the exact
seam production calls — and make NO network calls. The behavioral contracts under test are unchanged
from the original suite:

  * language routing picks the Persian vs English system prompt
  * per-transform token budgets are respected
  * failures / empty completions fall back to the original query, logged,
    never raised
  * step-back keeps only the first line; multi-query prepends the original,
    strips numbering/bullets, dedupes, and uses exactly ONE LLM call
  * the LLM call counters count successes AND failed attempts

Run from the engine root:  python -m pytest eval/test_query_transforms.py -v
"""

from unittest import mock

import pytest

import rag.query_transforms as qt


PERSIAN_Q = "حداقل ارتفاع سقف در فضای مسکونی چقدر است؟"
ENGLISH_Q = "What is the minimum ceiling height in residential spaces?"

PERSIAN_CLAUSE = "ارتفاع سقف فضاهای مسکونی نباید کمتر از ۲/۴ متر باشد."
ENGLISH_CLAUSE = ("The clear ceiling height of habitable rooms shall not be "
                  "less than 2.4 metres.")


@pytest.fixture()
def mock_chat():
    """Patch the production seam; reset counters around every test."""
    qt.reset_llm_counters()
    with mock.patch("rag.query_transforms.llm_chat") as m:
        yield m
    qt.reset_llm_counters()


def _system_of(call) -> str:
    return call.kwargs["messages"][0]["content"]


def _user_of(call) -> str:
    return call.kwargs["messages"][1]["content"]


# --- HyDE --------------------------------------------------------------------

def test_hyde_persian_nonempty_right_script(mock_chat):
    mock_chat.return_value = PERSIAN_CLAUSE
    out = qt.hyde_transform(PERSIAN_Q)
    assert out == PERSIAN_CLAUSE
    assert any("\u0600" <= ch <= "\u06FF" for ch in out)  # Arabic-script range


def test_hyde_english_nonempty_latin(mock_chat):
    mock_chat.return_value = ENGLISH_CLAUSE
    out = qt.hyde_transform(ENGLISH_Q)
    assert out == ENGLISH_CLAUSE
    assert out.isascii()


def test_hyde_prompt_selection_by_language(mock_chat):
    mock_chat.return_value = PERSIAN_CLAUSE
    qt.hyde_transform(PERSIAN_Q)
    assert _system_of(mock_chat.call_args) == qt.HYDE_SYSTEM_FA
    assert _user_of(mock_chat.call_args) == PERSIAN_Q

    mock_chat.reset_mock()
    mock_chat.return_value = ENGLISH_CLAUSE
    qt.hyde_transform(ENGLISH_Q)
    assert _system_of(mock_chat.call_args) == qt.HYDE_SYSTEM_EN
    assert _user_of(mock_chat.call_args) == ENGLISH_Q


def test_hyde_explicit_language_overrides_detection(mock_chat):
    mock_chat.return_value = ENGLISH_CLAUSE
    qt.hyde_transform(PERSIAN_Q, language="en")
    assert _system_of(mock_chat.call_args) == qt.HYDE_SYSTEM_EN


def test_hyde_token_budget_and_model_constraints(mock_chat):
    """The transforms pass ONLY messages + their token budget: model and
    temperature come from the provider clients' own defaults. This pins the
    constraint the old anthropic-based test asserted at the provider change
    point: AgentRouter's temperature 0.3 (its model comes from the tier
    policy / AGENTROUTER_MODEL config). Groq was removed 2026-07."""
    import inspect
    mock_chat.return_value = PERSIAN_CLAUSE
    qt.hyde_transform(PERSIAN_Q)
    kwargs = mock_chat.call_args.kwargs
    assert kwargs["max_completion_tokens"] == qt.HYDE_MAX_TOKENS
    assert "model" not in kwargs and "temperature" not in kwargs
    from rag.agentrouter_client import agentrouter_chat
    ar_defaults = inspect.signature(agentrouter_chat).parameters
    assert ar_defaults["temperature"].default == 0.3


def test_hyde_api_failure_returns_original(mock_chat, caplog):
    mock_chat.side_effect = RuntimeError("boom")
    with caplog.at_level("WARNING"):
        out = qt.hyde_transform(PERSIAN_Q)
    assert out == PERSIAN_Q
    assert any("hyde" in r.message.lower() or "transform" in r.message.lower()
               for r in caplog.records)


def test_hyde_empty_completion_returns_original(mock_chat, caplog):
    mock_chat.return_value = ""
    with caplog.at_level("WARNING"):
        out = qt.hyde_transform(ENGLISH_Q)
    assert out == ENGLISH_Q


# --- Step-back ----------------------------------------------------------------

def test_stepback_persian_single_question(mock_chat):
    mock_chat.return_value = "قواعد کلی ارتفاع فضاهای مسکونی چیست؟"
    out = qt.stepback_transform(PERSIAN_Q)
    assert out == "قواعد کلی ارتفاع فضاهای مسکونی چیست؟"
    assert "\n" not in out


def test_stepback_english_single_question(mock_chat):
    mock_chat.return_value = "What are the general height rules for rooms?"
    out = qt.stepback_transform(ENGLISH_Q)
    assert out == "What are the general height rules for rooms?"


def test_stepback_multiline_keeps_first_nonempty_line(mock_chat):
    mock_chat.return_value = ("\nWhat governs room heights?\n"
                              "Second stray line the model added.")
    out = qt.stepback_transform(ENGLISH_Q)
    assert out == "What governs room heights?"


def test_stepback_prompt_selection_and_token_budget(mock_chat):
    mock_chat.return_value = "q?"
    qt.stepback_transform(ENGLISH_Q)
    assert _system_of(mock_chat.call_args) == qt.STEPBACK_SYSTEM_EN
    assert (mock_chat.call_args.kwargs["max_completion_tokens"]
            == qt.STEPBACK_MAX_TOKENS)


def test_stepback_failure_returns_original(mock_chat, caplog):
    mock_chat.side_effect = ConnectionError("down")
    with caplog.at_level("WARNING"):
        out = qt.stepback_transform(PERSIAN_Q)
    assert out == PERSIAN_Q


# --- Multi-query ----------------------------------------------------------------

def test_multi_query_item0_is_original_and_length_n(mock_chat):
    mock_chat.return_value = "variant one\nvariant two"
    out = qt.multi_query_transform(ENGLISH_Q, n=3)
    assert out[0] == ENGLISH_Q
    assert out == [ENGLISH_Q, "variant one", "variant two"]


def test_multi_query_one_llm_call_for_all_variants(mock_chat):
    mock_chat.return_value = "v1\nv2\nv3"
    qt.multi_query_transform(ENGLISH_Q, n=4)
    assert mock_chat.call_count == 1
    # The single prompt requests n-1 reformulations.
    assert "3" in _system_of(mock_chat.call_args)


def test_multi_query_strips_numbering_and_bullets(mock_chat):
    mock_chat.return_value = "1. first variant\n- second variant\n• third variant"
    out = qt.multi_query_transform(ENGLISH_Q, n=4)
    assert out == [ENGLISH_Q, "first variant", "second variant", "third variant"]


def test_multi_query_dedupes_against_original_and_itself(mock_chat):
    mock_chat.return_value = f"{ENGLISH_Q}\nfresh variant\nfresh variant"
    out = qt.multi_query_transform(ENGLISH_Q, n=3)
    assert out == [ENGLISH_Q, "fresh variant"]


def test_multi_query_n1_no_llm_call(mock_chat):
    out = qt.multi_query_transform(ENGLISH_Q, n=1)
    assert out == [ENGLISH_Q]
    assert mock_chat.call_count == 0


def test_multi_query_failure_returns_original_only(mock_chat, caplog):
    mock_chat.side_effect = RuntimeError("nope")
    with caplog.at_level("WARNING"):
        out = qt.multi_query_transform(PERSIAN_Q, n=3)
    assert out == [PERSIAN_Q]


# --- Counters --------------------------------------------------------------------

def test_counters_accumulate_across_transforms(mock_chat):
    mock_chat.return_value = "text"
    qt.hyde_transform(ENGLISH_Q)
    qt.stepback_transform(ENGLISH_Q)
    qt.multi_query_transform(ENGLISH_Q, n=2)
    c = qt.llm_counters()
    assert c["llm_calls"] == 3
    assert c["llm_total_seconds"] >= 0.0


def test_counter_increments_on_failure_too(mock_chat):
    mock_chat.side_effect = RuntimeError("attempted = billed")
    qt.hyde_transform(ENGLISH_Q)
    assert qt.llm_counters()["llm_calls"] == 1


def test_reset_counters(mock_chat):
    mock_chat.return_value = "text"
    qt.hyde_transform(ENGLISH_Q)
    qt.reset_llm_counters()
    assert qt.llm_counters()["llm_calls"] == 0