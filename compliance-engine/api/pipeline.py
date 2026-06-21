"""
app/pipeline.py
===============
The single core function both the Celery task and the in-process fallback call.
Keeping the actual work in ONE place means the API behaves identically whether
or not a Celery/Redis worker is running.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

# The pipeline modules (orchestrator, agents) use flat imports like
# `from numeric_checker import ...`. They live in the sibling `services/`
# package. Add that directory to sys.path so those flat imports resolve
# regardless of where the worker process starts. This keeps every agent file
# unchanged (no package-relative import edits needed).
_SERVICES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


def load_clauses(clauses_path: Optional[str]) -> List[Dict[str, Any]]:
    """
    Load the ingested Mabhas clauses. In production these come from the RAG DB;
    for the service we accept a JSON file path (the mabhas_clauses.json corpus),
    falling back to an empty list so the service still starts without it.
    """
    if clauses_path and os.path.exists(clauses_path):
        with open(clauses_path, encoding="utf-8") as f:
            data = json.load(f)
        return [c for c in data if not c.get("skip_category")]
    return []


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
    # Imported here so importing this module is cheap (workers import lazily).
    from services.orchestrator import run_compliance
    from services.report_generator import generate_reports

    # Stage 1 / Step 7: construct the production retriever via the single
    # factory so the LLM interpretive pass (when an LLM is configured) gets
    # hybrid + reranked context. Construction failure (no DB, missing deps)
    # degrades to retriever=None — identical to pre-Step-7 behaviour, and
    # deterministic verdicts never depend on the retriever either way.
    retriever = None
    try:
        from rag.rag_retriever import build_default_retriever
        retriever = build_default_retriever()
    except Exception:  # noqa: BLE001 — service must start without RAG deps
        retriever = None

    result = run_compliance(bim_data, clauses, retriever=retriever,
                            use_langgraph=False)
    paths = generate_reports(result.to_dict(), meta or {}, out_dir=out_dir)

    return {
        "summary": result.summary,
        "duration_s": round(result.duration_s, 3),
        "n_findings": len(result.findings),
        "reports": {k: (os.path.basename(v) if v else None) for k, v in paths.items()},
    }
