"""
api/pipeline.py
===============
API-boundary helpers for clause loading and optional advisory wiring.

The authoritative validation orchestrator is
``services.validation_pipeline.run_validation_pipeline``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# The pipeline modules (orchestrator, agents) use flat imports like
# `from numeric_checker import ...`. They live in the sibling `services/`
# package. Add that directory to sys.path so those flat imports resolve
# regardless of where the worker process starts. This keeps every agent file
# unchanged (no package-relative import edits needed).
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES_DIR = os.path.join(_ENGINE_ROOT, "services")


def _ensure_paths() -> None:
    """(Re-)assert import paths at CALL time, not just import time.

    Celery loads `api.tasks` via import_from_cwd(), which puts the cwd
    (the engine root) on sys.path only TEMPORARILY and removes it after the
    app module is loaded. Any module-level `if path not in sys.path` guard
    is therefore skipped during import and the root is gone by task time.
    Calling this at the top of each pipeline entry point makes package
    imports (services.*, ingest.*, rag.*) and flat agent imports work in
    every execution mode: uvicorn, pytest, and Celery prefork workers.
    """
    for _p in (_ENGINE_ROOT, _SERVICES_DIR):
        if _p not in sys.path:
            sys.path.insert(0, _p)


_ensure_paths()


def load_clauses(clauses_path: Optional[str],
                 required: bool = False) -> List[Dict[str, Any]]:
    """
    Load the ingested Mabhas clauses (the mabhas_clauses.json corpus), dropping
    `skip_category` entries.

    Issue 9 — fail-fast: with ``required=True`` (production), a missing/empty/
    invalid corpus raises instead of silently returning an empty list, so the
    service can never run compliance against zero regulation clauses and call it
    a success. In dev (``required=False``) it returns ``[]`` and the caller can
    expose a degraded health state.
    """
    data: List[Dict[str, Any]] = []
    if clauses_path and os.path.exists(clauses_path):
        try:
            with open(clauses_path, encoding="utf-8") as f:
                raw = json.load(f)
            data = [c for c in raw if not c.get("skip_category")]
        except (json.JSONDecodeError, OSError) as exc:
            if required:
                raise RuntimeError(
                    f"Clause corpus at {clauses_path!r} is unreadable: {exc}")
            data = []
    if required and not data:
        raise RuntimeError(
            f"Clause corpus is empty or missing (path={clauses_path!r}). "
            f"Refusing to run compliance with zero clauses. Set CLAUSES_PATH to "
            f"a valid mabhas_clauses.json, or run with required=False in dev.")
    return data


def clause_health(clauses_path: Optional[str]) -> Dict[str, Any]:
    """Health view of the clause corpus for the /health endpoint."""
    exists = bool(clauses_path) and os.path.exists(clauses_path)
    count = 0
    if exists:
        try:
            count = len(load_clauses(clauses_path, required=False))
        except Exception:
            count = 0
    return {
        "clause_source": clauses_path or None,
        "clause_count": count,
        "clause_status": "ok" if count > 0 else "degraded",
    }


# ── LLM interpretive pass — production wiring (review fix C4, 2026-07) ───────
# Design decisions, made explicit:
#
#   * GATING     LLM_PASS_ENABLED (default "1") turns the pass on; the
#                provider then comes from rag/llm_client.resolve_provider():
#                LLM_PROVIDER pin, else AgentRouter when its key is present,
#                else Groq when its keys are present, else None → the exact
#                pre-C4 offline behaviour (interpretive clauses stay
#                NEEDS_REVIEW, zero LLM calls, zero retriever cost).
#   * CACHING    Retriever and llm callable are built ONCE per process and
#                reused across jobs. The retriever loads the regulation
#                graph + clause map (~1.4 MB JSON + graphml) and lazily the
#                embedding/reranker models; rebuilding per job was pure
#                waste. A lock guards thread-mode (no-broker) concurrency.
#   * FAILURE    Retriever construction failure degrades LOUDLY to None
#                (logged warning — the old bare except was silent) and the
#                LLM then runs without RAG context; llm-call failures are
#                already caught per clause inside the orchestrator. The
#                deterministic verdict path never depends on either.
#   * BUDGET     max 300 completion tokens: the advisory note is 1–2
#                sentences. On Groq, reasoning_effort="none" additionally
#                suppresses Qwen3's thinking tokens (9-key daily budget);
#                on AgentRouter that knob is dropped (OpenAI-compatible
#                surface) and the SDK's own 429/5xx backoff applies.
# ─────────────────────────────────────────────────────────────────────────────

_RT_LOCK = threading.Lock()
_RETRIEVER: Any = None
_RETRIEVER_READY = False
_LLM: Optional[Callable[[str], str]] = None
_LLM_READY = False


def _get_retriever() -> Any:
    """Build the production retriever once per process (Stage 1–3 stack)."""
    global _RETRIEVER, _RETRIEVER_READY
    if _RETRIEVER_READY:
        return _RETRIEVER
    with _RT_LOCK:
        if not _RETRIEVER_READY:
            try:
                from rag.rag_retriever import build_default_retriever
                _RETRIEVER = build_default_retriever()
                logger.info("RAG retriever ready (Graph/CRAG per env flags)")
            except Exception as exc:  # noqa: BLE001 — degrade loudly, never crash
                logger.warning(
                    "RAG retriever unavailable (%s) — the LLM interpretive "
                    "pass will run WITHOUT regulation context. Check "
                    "DATABASE_URL / rag dependencies.", exc)
                _RETRIEVER = None
            _RETRIEVER_READY = True
    return _RETRIEVER


def _get_llm() -> Optional[Callable[[str], str]]:
    """Return the cached llm callable, or None when the pass is off/unkeyed."""
    global _LLM, _LLM_READY
    if _LLM_READY:
        return _LLM
    with _RT_LOCK:
        if not _LLM_READY:
            _LLM = _build_llm()
            _LLM_READY = True
    return _LLM


def _build_llm() -> Optional[Callable[[str], str]]:
    if os.environ.get("LLM_PASS_ENABLED", "1") != "1":
        logger.info("LLM interpretive pass disabled (LLM_PASS_ENABLED != 1)")
        return None
    # Provider resolution (rag/llm_client.py): explicit LLM_PROVIDER, else
    # agentrouter when AGENTROUTER_API_KEY / AGENT_ROUTER_TOKEN is present,
    # else None — the offline behaviour (interpretive clauses stay
    # NEEDS_REVIEW, zero LLM calls). Groq was removed 2026-07.
    try:
        from rag.llm_client import llm_chat, provider_status, resolve_provider
    except Exception as exc:  # noqa: BLE001 — rag deps absent → offline mode
        logger.warning("LLM interpretive pass off: llm client unavailable "
                       "(%s)", exc)
        return None
    provider = resolve_provider()
    if provider is None:
        logger.info("LLM interpretive pass off: no provider configured — set "
                    "AGENTROUTER_API_KEY (interpretive clauses stay "
                    "NEEDS_REVIEW, the offline behaviour)")
        return None

    def _llm(prompt: str) -> str:
        return llm_chat(
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=300,     # 1–2 sentence advisory note
        )

    logger.info("LLM interpretive pass ENABLED via %s (advisory notes only — "
                "deterministic verdicts are never LLM-touched)",
                provider_status())
    return _llm


def reset_llm_wiring_for_tests() -> None:
    """Reset the cached retriever/llm so tests can exercise both branches."""
    global _RETRIEVER, _RETRIEVER_READY, _LLM, _LLM_READY
    with _RT_LOCK:
        _RETRIEVER, _RETRIEVER_READY = None, False
        _LLM, _LLM_READY = None, False



def configure_advisory(request):
    """Attach the process-cached advisory retriever/LLM to a PipelineRequest.

    Deterministic verdict logic remains inside the canonical validation
    orchestrator; these dependencies only enrich eligible review findings.
    """
    llm = _get_llm()
    request.llm = llm
    request.retriever = _get_retriever() if llm is not None else None
    request.use_langgraph = False
    return request
