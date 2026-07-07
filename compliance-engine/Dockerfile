"""
api/pipeline.py
===============
The single core function both the Celery task and the in-process fallback call.
Keeping the actual work in ONE place means the API behaves identically whether
or not a Celery/Redis worker is running.
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


# ── Operator building parameters (wire contract) ─────────────────────────────
# Canonical engine names carry an explicit _mm suffix. Step 1 writes the same
# parameters into Pset_SimsysContract as individual typed properties
# (WallHeightMm, DoorHeightMm, WindowHeightMm, WindowSillHeightMm,
# FloorThicknessMm + BuildingParamsProvided) — see export/ifc_exporter.py and
# ingest/ifc_to_bim_data.py. The numeric checker only consumes
# ceiling_height_mm today; the rest are accepted for forward compatibility so
# a Step-1 client can pass its full parameter set unchanged.
ACCEPTED_BUILDING_PARAMS: Dict[str, tuple] = {
    "ceiling_height_mm":     (2000.0, 6000.0),
    "wall_height_mm":        (500.0,  6000.0),
    "door_height_mm":        (1800.0, 3000.0),
    "window_width_mm":       (300.0,  5000.0),
    "window_height_mm":      (200.0,  3000.0),
    "window_sill_height_mm": (0.0,    2000.0),
    "floor_thickness_mm":    (50.0,   600.0),
}

# Section 1's own vocabulary (schemas.BuildingParams / the IFC contract Pset /
# BimDataBuilder) uses the same parameters WITHOUT the _mm suffix — mm is
# implied project-wide there. Accept those spellings as aliases and normalize
# to the canonical _mm names so the web UI can post its parameter dict to
# either service unchanged. One vocabulary inside the engine, two accepted at
# the boundary.
_PARAM_ALIASES: Dict[str, str] = {
    "wall_height":        "wall_height_mm",
    "door_height":        "door_height_mm",
    "window_width":       "window_width_mm",
    "window_height":      "window_height_mm",
    "window_sill_height": "window_sill_height_mm",
    "floor_thickness":    "floor_thickness_mm",
}


class BuildingParamsError(ValueError):
    """Raised when client-supplied building parameters are invalid.

    Deliberately a plain ValueError subclass (not an HTTPException) so this
    module stays framework-free and usable from the Celery worker; the API
    layer converts it to a 400.
    """


def parse_building_params(raw: Any) -> Dict[str, float]:
    """Validate operator-supplied building parameters at the system boundary.

    Accepts a JSON string (as sent in a multipart form field) or an
    already-decoded dict (as embedded in a JSON /analyze body). Returns a
    cleaned ``{key: float}`` dict containing only whitelisted keys within
    their sane ranges. Empty/None input returns ``{}``.

    Raises BuildingParamsError with a client-safe message on any problem —
    unknown keys are rejected (not silently dropped) because a typo like
    ``celing_height_mm`` would otherwise silently leave the engine default
    driving a PASS/FAIL verdict the operator believes they configured.
    """
    if raw is None or raw == "" or raw == {}:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BuildingParamsError(
                f"building_params is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})") from exc
    if not isinstance(raw, dict):
        raise BuildingParamsError(
            f"building_params must be a JSON object, got {type(raw).__name__}")
    cleaned: Dict[str, float] = {}
    for key, val in raw.items():
        key = _PARAM_ALIASES.get(key, key)  # normalize Section-1 spellings
        if key not in ACCEPTED_BUILDING_PARAMS:
            raise BuildingParamsError(
                f"Unknown building parameter '{key}'. "
                f"Accepted keys: {sorted(ACCEPTED_BUILDING_PARAMS)} "
                f"(the unsuffixed Step-1 spellings "
                f"{sorted(_PARAM_ALIASES)} are also accepted)")
        if isinstance(val, bool) or not isinstance(val, (int, float, str)):
            raise BuildingParamsError(
                f"building_params.{key} must be a number, got {type(val).__name__}")
        try:
            fval = float(val)
        except (TypeError, ValueError):
            raise BuildingParamsError(
                f"building_params.{key} must be a number, got {val!r}")
        lo, hi = ACCEPTED_BUILDING_PARAMS[key]
        if not (lo <= fval <= hi):
            raise BuildingParamsError(
                f"building_params.{key} must be between {lo:g} and {hi:g} mm, "
                f"got {fval:g}")
        cleaned[key] = fval

    # Cross-field consistency (mirrors Section 1's schemas.BuildingParams):
    # openings must fit under the wall/ceiling height. Only enforceable when
    # the operator supplied the involved values in the same request — missing
    # ones may legitimately come from the IFC contract Pset downstream.
    _wall = cleaned.get("wall_height_mm", cleaned.get("ceiling_height_mm"))
    if _wall is not None:
        _sill, _winh = (cleaned.get("window_sill_height_mm"),
                        cleaned.get("window_height_mm"))
        if _sill is not None and _winh is not None and _sill + _winh > _wall:
            raise BuildingParamsError(
                f"building_params inconsistent: window_sill_height + "
                f"window_height ({_sill + _winh:g} mm) exceeds the wall/"
                f"ceiling height ({_wall:g} mm) — the window head would be "
                f"above the ceiling.")
        _door = cleaned.get("door_height_mm")
        if _door is not None and _door > _wall:
            raise BuildingParamsError(
                f"building_params inconsistent: door_height ({_door:g} mm) "
                f"exceeds the wall/ceiling height ({_wall:g} mm).")
    return cleaned


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
    # else groq when GROQ keys are present, else None — the pre-C4 offline
    # behaviour (interpretive clauses stay NEEDS_REVIEW, zero LLM calls).
    try:
        from rag.llm_client import llm_chat, provider_status, resolve_provider
    except Exception as exc:  # noqa: BLE001 — rag deps absent → offline mode
        logger.warning("LLM interpretive pass off: llm client unavailable "
                       "(%s)", exc)
        return None
    provider = resolve_provider()
    if provider is None:
        logger.info("LLM interpretive pass off: no provider configured — set "
                    "AGENTROUTER_API_KEY or GROQ_API_KEYS (interpretive "
                    "clauses stay NEEDS_REVIEW, the offline behaviour)")
        return None

    def _llm(prompt: str) -> str:
        return llm_chat(
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=300,     # 1–2 sentence advisory note
            reasoning_effort="none",       # groq/qwen3 only; dropped for
        )                                  # agentrouter (see llm_client)

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


def run_pipeline(
    bim_data: Dict[str, Any],
    clauses: List[Dict[str, Any]],
    out_dir: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run compliance + generate reports. Returns a JSON-serialisable dict with the
    summary and the relative report file names. This is the unit of work a job
    performs.
    """
    _ensure_paths()
    # Imported here so importing this module is cheap (workers import lazily).
    from services.orchestrator import run_compliance
    from services.report_generator import generate_reports

    # Review fix C4 (2026-07): the LLM interpretive pass is now WIRED INTO
    # production. Previously this function built a retriever but passed no
    # llm= callable, so _llm_review_interpretive returned immediately and the
    # entire Stage 1–3 RAG stack (hybrid+rerank, CRAG, Graph) never ran on a
    # real job. The retriever is only built when an LLM is actually
    # configured (no llm → no retriever cost, the old fully-offline
    # behaviour), and both are cached at module level instead of rebuilt per
    # job. Deterministic PASS/FAIL verdicts never depend on either.
    llm = _get_llm()
    retriever = _get_retriever() if llm is not None else None

    result = run_compliance(bim_data, clauses, retriever=retriever, llm=llm,
                            use_langgraph=False)

    # Issue 8 — honest clause-coverage accounting, passed to the report too.
    from services.coverage import build_coverage
    coverage = build_coverage(result, clauses)

    paths = generate_reports(result.to_dict(), meta or {}, out_dir=out_dir,
                             coverage=coverage)

    return {
        "summary": result.summary,
        "coverage": coverage,
        "duration_s": round(result.duration_s, 3),
        "n_findings": len(result.findings),
        "reports": {k: (os.path.basename(v) if v else None) for k, v in paths.items()},
    }


def run_pipeline_from_ifc(
    ifc_path: str,
    clauses: List[Dict[str, Any]],
    out_dir: str,
    meta: Optional[Dict[str, Any]] = None,
    threshold: Optional[float] = None,
    corpus_total: Optional[int] = None,
    building_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Same unit of work as run_pipeline(), but the input is a Step-1 enriched
    **IFC file** instead of a pre-built bim_data dict. Reconstructs bim_data from
    the IFC (B1), normalizes room categories, applies the confidence pre-pass +
    honest-degradation (B2), runs compliance, builds the coverage table, then
    generates reports.

    Returns a JSON-serialisable dict with the summary, coverage table, review/
    flag counts, room-category summary, and the report file names.
    """
    _ensure_paths()
    from ingest.ifc_pipeline import run_ifc_compliance
    from services.report_generator import generate_reports

    # Review fix C4 (2026-07): the primary production path now carries the
    # LLM interpretive pass too (it previously passed neither retriever nor
    # llm, so the RAG stack never ran on an uploaded IFC). Same gating and
    # caching as run_pipeline; no llm → no retriever cost → old behaviour.
    llm = _get_llm()
    retriever = _get_retriever() if llm is not None else None

    result, bim_data = run_ifc_compliance(ifc_path, clauses, threshold=threshold,
                                          retriever=retriever, llm=llm,
                                          corpus_total=corpus_total,
                                          building_params=building_params)
    coverage = bim_data.get("_coverage", {})
    paths = generate_reports(result.to_dict(), meta or {}, out_dir=out_dir,
                             coverage=coverage)

    review = bim_data.get("_review_summary", {})
    return {
        "summary": result.summary,
        "coverage": coverage,
        "duration_s": round(result.duration_s, 3),
        "n_findings": len(result.findings),
        "flagged_count": review.get("flagged_count", 0),
        "downgraded_count": review.get("downgraded_count", 0),
        "category_summary": bim_data.get("_category_summary", {}),
        "categories_seen": bim_data.get("_categories_seen", {}),
        "schema_version": bim_data.get("schema_version"),
        "reports": {k: (os.path.basename(v) if v else None) for k, v in paths.items()},
    }