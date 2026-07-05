"""
ingest/ifc_pipeline.py
======================
Lane 2 entry point — "ENRICHED IFC → verdicts".

`run_ifc_compliance(ifc_path, clauses, ...)` is the one call that turns a Step-1
`plan.ifc` into a ComplianceResult, with the §B2 confidence handling applied:

    plan.ifc
       │  ifc_to_bim_data           (B1: reconstruct the agents' bim_data dict)
       ▼
    bim_data
       │  apply_review_prepass      (B2: flag uncertain elements)
       ▼
    run_compliance(bim_data, …)     (the four agents, UNCHANGED)
       │  downgrade_flagged_findings(B2: dependent PASS/FAIL → NEEDS_REVIEW)
       ▼
    ComplianceResult  (+ review summary + categories seen, for visibility)

Step 2 consumes only the IFC path — nothing else. The agents, SpatialGraph and
run_compliance are not modified.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# The orchestrator + agents use flat imports (`from orchestrator import ...`,
# `from numeric_checker import ...`) resolved from the sibling services/ dir —
# the same convention api/pipeline.py uses. Put repo-root and services/ on the
# path so this entry point works no matter where it is launched from, without
# touching any agent file.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "services")
for _p in (_ROOT, _SERVICES):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ingest.ifc_to_bim_data import ifc_to_bim_data          # noqa: E402
from ingest.review_prepass import (                          # noqa: E402
    apply_review_prepass, downgrade_flagged_findings,
)
from ingest.category_normalizer import normalize_room_categories  # noqa: E402


def _categories_seen(bim_data: Dict[str, Any]) -> Dict[str, int]:
    """Distinct room categories present in the reconstructed plan (visibility
    aid for the category-vocabulary seam — unmapped categories simply surface as
    NEEDS_REVIEW in the findings)."""
    out: Dict[str, int] = {}
    for r in bim_data.get("rooms", []) or []:
        c = r.get("category", "unknown") or "unknown"
        out[c] = out.get(c, 0) + 1
    return out


def run_ifc_compliance(
    ifc_path: str,
    clauses: List[Dict[str, Any]],
    threshold: Optional[float] = None,
    retriever: Optional[Any] = None,
    llm: Optional[Callable[[str], str]] = None,
    use_langgraph: bool = False,
    corpus_total: Optional[int] = None,
    building_params: Optional[Dict[str, Any]] = None,
):
    """Run the full Lane-2 pipeline on one IFC file.

    Returns ``(result, bim_data)`` where ``result`` is the ComplianceResult
    (flagged verdicts already downgraded) and ``bim_data`` carries
    ``_review_summary`` (flagged + downgraded_count), ``_category_summary``
    (canonical / normalized / unmapped room counts), ``_coverage`` (the honest
    clause-coverage table) and the reconstructed elements.
    """
    if not os.path.isfile(ifc_path):
        raise FileNotFoundError(f"IFC file not found: {ifc_path}")

    logger.info("Loading IFC contract: %s", ifc_path)
    bim_data = ifc_to_bim_data(ifc_path)

    # Issue 2: resolve room categories to the canonical room_* vocabulary the
    # agents match on, BEFORE the pre-pass — so an unmappable category becomes
    # needs_review and can't silently satisfy/fail a rule.
    normalize_room_categories(bim_data)

    # B2 pre-pass: flag uncertain elements before any agent runs.
    apply_review_prepass(bim_data, threshold=threshold)
    bim_data["_categories_seen"] = _categories_seen(bim_data)

    # The agents, unchanged. Imported here so path bootstrap above is in effect.
    from orchestrator import run_compliance
    result = run_compliance(bim_data, clauses, retriever=retriever, llm=llm,
                            use_langgraph=use_langgraph,
                            building_params=building_params)

    # B2 post-pass: any PASS/FAIL that depends on a flagged element → NEEDS_REVIEW.
    downgrade_flagged_findings(result, bim_data)

    # Issue 8: honest clause-coverage accounting (PASS/FAIL/NEEDS_REVIEW/
    # UNSUPPORTED/BLOCKED_BY_MISSING_DATA) computed from the findings + clauses.
    from coverage import build_coverage
    bim_data["_coverage"] = build_coverage(result, clauses, corpus_total=corpus_total)

    rs = bim_data.get("_review_summary", {})
    logger.info("IFC compliance complete: %s | flagged=%s downgraded=%s | "
                "coverage=%s | categories=%s", result.summary,
                rs.get("flagged_count"), rs.get("downgraded_count"),
                {k: bim_data["_coverage"][k] for k in
                 ("checked", "unsupported", "blocked_by_missing_data")},
                bim_data.get("_categories_seen"))
    return result, bim_data