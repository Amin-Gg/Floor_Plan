"""
Mocked unit tests for RAG/query_transforms (hyde, stepback, multi_query).

The anthropic client is mocked — these tests make NO network calls.
Run from the project root:  python -m pytest eval/test_query_transforms.py -v
"""

import re
from unittest import mock

import pytest

from rag.query_transforms import (
    HYDE_SYSTEM_EN,
    HYDE_SYSTEM_FA,
    STEPBACK_SYSTEM_EN,
    STEPBACK_SYSTEM_FA,
    hyde_transform,
    llm_counters,
    multi_query_transform,
    reset_llm_counters,
    stepback_transform,
)

PERSIAN_CHARS = re.compile(r"[\u0600-\u06FF]")

FAKE_HYDE_FA = (
    "حداقل عرض راهروی خروج در ساختمان‌های مسکونی نباید کمتر از یک متر و ده "
    "سانتی‌متر باشد و رعایت این مقدار در تمام طبقات الزامی است."
)
FAKE_HYDE_EN = (
    "The minimum clear width of an exit corridor in residential occupancies "
    "shall not be less than 1.10 metres, and this requirement applies to all "
    "storeys of the building."
)
FAKE_STEPBACK_FA = "قواعد کلی ارتباط فضایی میان اتاق‌های اقامتی و فضاهای بهداشتی در ساختمان‌های مسکونی چیست؟"
FAKE_STEPBACK_EN = (
    "What are the general rules for spatial connections between habitable "
    "rooms and sanitary spaces in residential buildings?"
)
FAKE_MULTI_FA = (
    "کمینهٔ پهنای کریدور خروج در بناهای مسکونی چقدر است؟\n"
    "عرض مجاز راهروهای فرار در ساختمان مسکونی چه مقدار تعیین شده است؟"
)


def make_fake_response(text):
    block = mock.Mock()
    block.type = "text"
    block.text = text
    response = mock.Mock()
    response.content = [block]
    return response


@pytest.fixture(autouse=True)
def fresh_counters():
    reset_llm_counters()
    yield


@pytest.fixture
def mock_client():
    with mock.patch("rag.query_transforms.anthropic.Anthropic") as cls:
        client = cls.return_value
        yield client


# ===========================================================================
# HyDE (carried over from Step 2)
# ===========================================================================

def test_hyde_persian_nonempty_right_script_min_length(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_FA)
    out = hyde_transform("حداقل عرض راهرو چقدر است؟", language="fa")
    assert out and len(out) >= 50 and PERSIAN_CHARS.search(out)


def test_hyde_english_nonempty_latin_min_length(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_EN)
    out = hyde_transform("What is the minimum corridor width?", language="en")
    assert out and len(out) >= 50 and not PERSIAN_CHARS.search(out)


def test_hyde_prompt_selection(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_FA)
    hyde_transform("سوال", language="fa")
    assert mock_client.messages.create.call_args.kwargs["system"] == HYDE_SYSTEM_FA
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_EN)
    hyde_transform("question", language="en")
    assert mock_client.messages.create.call_args.kwargs["system"] == HYDE_SYSTEM_EN


def test_hyde_model_and_temperature_constraints(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_FA)
    hyde_transform("سوال", language="fa")
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 300
    assert kwargs["messages"] == [{"role": "user", "content": "سوال"}]


def test_hyde_api_failure_returns_original(mock_client, caplog):
    mock_client.messages.create.side_effect = RuntimeError("api down")
    q = "حداقل عرض راهرو چقدر است؟"
    with caplog.at_level("WARNING"):
        assert hyde_transform(q, language="fa") == q


def test_hyde_empty_completion_returns_original(mock_client, caplog):
    mock_client.messages.create.return_value = make_fake_response("   ")
    q = "What is the minimum corridor width?"
    with caplog.at_level("WARNING"):
        assert hyde_transform(q, language="en") == q


# ===========================================================================
# Step-back
# ===========================================================================

def test_stepback_persian_single_question(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_STEPBACK_FA)
    out = stepback_transform("آیا در اتاق خواب می‌تواند مستقیماً به حمام باز شود؟", language="fa")
    assert out == FAKE_STEPBACK_FA
    assert PERSIAN_CHARS.search(out)
    assert "\n" not in out


def test_stepback_english_single_question(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_STEPBACK_EN)
    out = stepback_transform("Can a bedroom door open directly into a bathroom?", language="en")
    assert out == FAKE_STEPBACK_EN
    assert not PERSIAN_CHARS.search(out)


def test_stepback_multiline_keeps_first_line(mock_client):
    # Model misbehaves with multiple lines -> defensive parse keeps line 1.
    mock_client.messages.create.return_value = make_fake_response(
        FAKE_STEPBACK_EN + "\nHere is another question?\nAnd a third?"
    )
    out = stepback_transform("Can a bedroom door open into a bathroom?", language="en")
    assert out == FAKE_STEPBACK_EN


def test_stepback_prompt_selection_and_params(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_STEPBACK_FA)
    stepback_transform("سوال", language="fa")
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["system"] == STEPBACK_SYSTEM_FA
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 200

    mock_client.messages.create.return_value = make_fake_response(FAKE_STEPBACK_EN)
    stepback_transform("question", language="en")
    assert mock_client.messages.create.call_args.kwargs["system"] == STEPBACK_SYSTEM_EN


def test_stepback_failure_returns_original(mock_client, caplog):
    mock_client.messages.create.side_effect = RuntimeError("api down")
    q = "Can a bedroom door open directly into a bathroom?"
    with caplog.at_level("WARNING"):
        assert stepback_transform(q, language="en") == q


# ===========================================================================
# Multi-query
# ===========================================================================

def test_multi_query_item0_is_original_and_length_n(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_MULTI_FA)
    q = "حداقل عرض راهروی خروج چقدر است؟"
    out = multi_query_transform(q, n=3, language="fa")
    assert out[0] == q
    assert len(out) == 3
    assert len(set(out)) == 3  # all distinct


def test_multi_query_one_llm_call_for_all_variants(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_MULTI_FA)
    multi_query_transform("سوال اصلی", n=3, language="fa")
    assert mock_client.messages.create.call_count == 1
    assert llm_counters()["llm_calls"] == 1
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 400
    assert "2" in kwargs["system"]  # asked for n-1 = 2 reformulations


def test_multi_query_strips_numbering_and_bullets(mock_client):
    mock_client.messages.create.return_value = make_fake_response(
        "1. First reformulation of the question?\n- Second reformulation here?"
    )
    out = multi_query_transform("Original question?", n=3, language="en")
    assert out[0] == "Original question?"
    assert out[1] == "First reformulation of the question?"
    assert out[2] == "Second reformulation here?"


def test_multi_query_dedupes_against_original(mock_client):
    q = "Original question?"
    mock_client.messages.create.return_value = make_fake_response(
        f"{q}\nA genuinely different phrasing?"
    )
    out = multi_query_transform(q, n=3, language="en")
    # The echoed original is dropped; we get original + 1 distinct variant.
    assert out == [q, "A genuinely different phrasing?"]


def test_multi_query_n1_no_llm_call(mock_client):
    out = multi_query_transform("سوال", n=1, language="fa")
    assert out == ["سوال"]
    assert mock_client.messages.create.call_count == 0
    assert llm_counters()["llm_calls"] == 0


def test_multi_query_failure_returns_original_only(mock_client, caplog):
    mock_client.messages.create.side_effect = RuntimeError("api down")
    with caplog.at_level("WARNING"):
        out = multi_query_transform("سوال", n=3, language="fa")
    assert out == ["سوال"]


# ===========================================================================
# Shared accounting
# ===========================================================================

def test_counters_accumulate_across_transforms(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_FA)
    hyde_transform("سوال", language="fa")
    mock_client.messages.create.return_value = make_fake_response(FAKE_STEPBACK_FA)
    stepback_transform("سوال", language="fa")
    mock_client.messages.create.return_value = make_fake_response(FAKE_MULTI_FA)
    multi_query_transform("سوال", n=3, language="fa")
    assert llm_counters()["llm_calls"] == 3


def test_counter_increments_on_failure_too(mock_client):
    mock_client.messages.create.side_effect = RuntimeError("api down")
    stepback_transform("سوال", language="fa")
    assert llm_counters()["llm_calls"] == 1


def test_reset_counters(mock_client):
    mock_client.messages.create.return_value = make_fake_response(FAKE_HYDE_FA)
    hyde_transform("سوال", language="fa")
    reset_llm_counters()
    assert llm_counters() == {"llm_calls": 0, "llm_total_seconds": 0.0}
