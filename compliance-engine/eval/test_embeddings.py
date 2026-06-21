"""
eval/test_embeddings.py
=======================
Unit tests for the EMBED_MODEL switch in services/embeddings.py.

No model downloads, no torch inference: sentence_transformers is replaced
with a fake module whose SentenceTransformer records every encode() call.
The embeddings module is loaded FRESH per test (importlib) with a patched
environment, because MODEL_NAME is read from EMBED_MODEL at import time.

Run:
    python -m pytest eval/test_embeddings.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


class _FakeST:
    """Fake SentenceTransformer capturing constructor + encode args."""

    instances: list = []

    def __init__(self, model_name):
        self.model_name = model_name
        self.encode_calls = []
        _FakeST.instances.append(self)

    def encode(self, texts, batch_size=32, normalize_embeddings=False,
               show_progress_bar=False):
        self.encode_calls.append(
            {"texts": list(texts), "normalize": normalize_embeddings}
        )

        class _Vec(list):
            def tolist(self):
                return list(self)

        return [_Vec([0.0] * 1024) for _ in texts]


def _load_embeddings(monkeypatch, embed_model=None):
    """Import RAG/embeddings.py fresh under a controlled environment."""
    if embed_model is None:
        monkeypatch.delenv("EMBED_MODEL", raising=False)
    else:
        monkeypatch.setenv("EMBED_MODEL", embed_model)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    name = "_embeddings_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _ROOT / "rag" / "embeddings.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------

def test_default_model_is_e5_with_prefixes(monkeypatch):
    emb = _load_embeddings(monkeypatch, embed_model=None)
    assert emb.MODEL_NAME == "intfloat/multilingual-e5-large"

    emb.embed_passages(["متن بند"])
    emb.embed_query("سوال")
    model = emb.get_model()
    assert model.model_name == "intfloat/multilingual-e5-large"
    assert model.encode_calls[0]["texts"] == ["passage: متن بند"]
    assert model.encode_calls[1]["texts"] == ["query: سوال"]


def test_bge_m3_no_prefixes_raw_text(monkeypatch):
    emb = _load_embeddings(monkeypatch, embed_model="BAAI/bge-m3")
    assert emb.MODEL_NAME == "BAAI/bge-m3"

    emb.embed_passages(["متن بند"])
    emb.embed_query("سوال")
    model = emb.get_model()
    assert model.model_name == "BAAI/bge-m3"
    assert model.encode_calls[0]["texts"] == ["متن بند"]      # no "passage: "
    assert model.encode_calls[1]["texts"] == ["سوال"]          # no "query: "


def test_normalize_embeddings_true_for_every_model(monkeypatch):
    for name in (None, "BAAI/bge-m3"):
        emb = _load_embeddings(monkeypatch, embed_model=name)
        emb.embed_passages(["a"])
        emb.embed_query("b")
        assert all(c["normalize"] for c in emb.get_model().encode_calls)


def test_query_and_passage_share_one_model_instance(monkeypatch):
    """The consistency guarantee: both paths resolve to the SAME singleton
    loaded from the SAME MODEL_NAME constant."""
    emb = _load_embeddings(monkeypatch, embed_model="BAAI/bge-m3")
    before = len(_FakeST.instances)
    emb.embed_passages(["a"])
    emb.embed_query("b")
    assert len(_FakeST.instances) == before + 1   # exactly one load
    assert emb.get_model() is emb.get_model()      # singleton stable


def test_env_var_respected_verbatim(monkeypatch):
    emb = _load_embeddings(monkeypatch, embed_model="intfloat/multilingual-e5-small")
    emb.embed_query("x")
    # any e5-family name keeps prefixes
    assert emb.get_model().encode_calls[0]["texts"] == ["query: x"]


def test_empty_inputs_behave_as_before(monkeypatch):
    emb = _load_embeddings(monkeypatch, embed_model="BAAI/bge-m3")
    assert emb.embed_passages([]) == []
    with pytest.raises(ValueError):
        emb.embed_query("   ")


def test_vector_dim_constant_unchanged(monkeypatch):
    emb = _load_embeddings(monkeypatch, embed_model="BAAI/bge-m3")
    assert emb.EMBEDDING_DIM == 1024
    assert len(emb.embed_query("x")) == 1024


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
