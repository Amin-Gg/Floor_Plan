"""
eval/test_contextualize.py
==========================
Unit tests for services/contextualize.py — no API key, no network:
the anthropic client is mocked and sleeps are no-ops.

Run:
    python -m pytest eval/test_contextualize.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Load RAG/contextualize.py under the services namespace without importing
# the embeddings stack.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import services  # noqa: E402

if "rag.contextualize" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "rag.contextualize", _ROOT / "rag" / "contextualize.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["rag.contextualize"] = _mod
    _spec.loader.exec_module(_mod)

from rag.contextualize import (  # noqa: E402
    MODEL,
    PRICE_PER_INPUT_TOKEN,
    PRICE_PER_OUTPUT_TOKEN,
    build_user_message,
    process_clauses,
)


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _Resp:
    def __init__(self, text, i=100, o=20):
        self.content = [_Block(text)]
        self.usage = _Usage(i, o)


class MockClient:
    """Scriptable client: `behaviors` maps article_id -> list of outcomes,
    one per successive call ('ok' or an Exception instance)."""

    def __init__(self, behaviors=None, default_text="بافت: متن زمینه."):
        self.behaviors = behaviors or {}
        self.default_text = default_text
        self.calls = []  # (article_id, system, user_message)
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, model, max_tokens, temperature, system, messages):
        user = messages[0]["content"]
        # article_id is not in the user message; track call order by content
        self.calls.append((system, user))
        key = None
        for aid in self.behaviors:
            if f"\u200c{aid}\u200c" in user or aid in user:
                key = aid
                break
        if key is not None and self.behaviors[key]:
            outcome = self.behaviors[key].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _Resp(outcome)
        return _Resp(self.default_text)


def _clause(aid, skip=None, **extra):
    c = {
        "mabhas_part": "4",
        "article_id": aid,
        "heading_fa_normalized": f"عنوان {aid}",
        "text_fa_normalized": f"متن بند {aid}",
        "text_fa": f"متن خام {aid}",
        "rule_type": "numeric",
        "skip_category": skip,
    }
    c.update(extra)
    return c


_NO_SLEEP = lambda s: None  # noqa: E731


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path_attaches_context_and_counts_cost():
    clauses = [_clause("1-1-1"), _clause("1-1-2")]
    client = MockClient()
    out, stats = process_clauses(clauses, client, sleep_fn=_NO_SLEEP, log=lambda s: None)

    assert all(c["context_fa"] == "بافت: متن زمینه." for c in out)
    assert stats["processed"] == 2
    assert stats["failed_ids"] == []
    assert stats["input_tokens"] == 200 and stats["output_tokens"] == 40
    expected_cost = round(200 * PRICE_PER_INPUT_TOKEN + 40 * PRICE_PER_OUTPUT_TOKEN, 4)
    assert stats["cost_usd"] == expected_cost


def test_input_clauses_never_mutated():
    clauses = [_clause("1-1-1")]
    process_clauses(clauses, MockClient(), sleep_fn=_NO_SLEEP, log=lambda s: None)
    assert "context_fa" not in clauses[0]


def test_skip_category_copied_without_api_call():
    clauses = [_clause("adm-1", skip="administrative"), _clause("1-1-1")]
    client = MockClient()
    out, stats = process_clauses(clauses, client, sleep_fn=_NO_SLEEP, log=lambda s: None)

    assert "context_fa" not in out[0]            # skipped clause untouched
    assert out[1]["context_fa"]                  # eligible clause contextualized
    assert stats["copied_no_call"] == 1
    assert len(client.calls) == 1                # exactly one API call made


def test_retry_once_then_success():
    boom = RuntimeError("rate limited")
    client = MockClient(behaviors={"1-1-1": [boom, "زمینه پس از تلاش مجدد."]})
    slept = []
    out, stats = process_clauses(
        [_clause("1-1-1")], client,
        sleep_fn=lambda s: slept.append(s), log=lambda s: None,
    )
    assert out[0]["context_fa"] == "زمینه پس از تلاش مجدد."
    assert stats["processed"] == 1 and stats["failed_ids"] == []
    assert 5.0 in slept                          # the retry wait happened


def test_double_failure_skips_and_logs_article_id():
    client = MockClient(behaviors={"1-1-1": [RuntimeError("x"), RuntimeError("y")]})
    out, stats = process_clauses(
        [_clause("1-1-1"), _clause("1-1-2")], client,
        sleep_fn=_NO_SLEEP, log=lambda s: None,
    )
    assert "context_fa" not in out[0]            # failed clause has no context
    assert stats["failed_ids"] == ["1-1-1"]
    assert out[1]["context_fa"]                  # later clauses unaffected
    assert stats["processed"] == 1


def test_resume_skips_existing_contexts_without_calls():
    client = MockClient()
    out, stats = process_clauses(
        [_clause("1-1-1"), _clause("1-1-2")], client,
        existing_contexts={"1-1-1": "زمینه قبلی"},
        sleep_fn=_NO_SLEEP, log=lambda s: None,
    )
    assert out[0]["context_fa"] == "زمینه قبلی"
    assert stats["resumed"] == 1 and stats["processed"] == 1
    assert len(client.calls) == 1


def test_empty_model_output_triggers_retry_path():
    client = MockClient(behaviors={"1-1-1": ["", ""]})  # empty -> ValueError twice
    out, stats = process_clauses(
        [_clause("1-1-1")], client, sleep_fn=_NO_SLEEP, log=lambda s: None,
    )
    assert stats["failed_ids"] == ["1-1-1"]


def test_user_message_contains_exactly_the_spec_fields():
    msg = build_user_message(_clause("4-5-2-1"))
    assert "مبحث: 4" in msg
    assert "عنوان 4-5-2-1" in msg
    assert "numeric" in msg
    assert "متن بند 4-5-2-1" in msg
    assert "متن خام" not in msg                  # raw text must NOT be sent


def test_limit_caps_api_calls():
    clauses = [_clause(f"1-1-{i}") for i in range(5)]
    client = MockClient()
    out, stats = process_clauses(
        clauses, client, limit=2, sleep_fn=_NO_SLEEP, log=lambda s: None,
    )
    assert stats["processed"] == 2
    assert sum(1 for c in out if "context_fa" in c) == 2
    assert len(out) == 5                          # all clauses still in output


def test_model_constant_matches_spec():
    assert MODEL == "claude-sonnet-4-20250514"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
