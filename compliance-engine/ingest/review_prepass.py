"""
ingest/review_prepass.py
========================
WORKSTREAM B2 — confidence / review propagation (the honest-degradation fix).

Two functions, run around `run_compliance`:

1. `apply_review_prepass(bim_data, threshold)` — BEFORE the agents run, annotate
   every reconstructed element with a `review` block and flag the ones that are
   uncertain (`NeedsReview=true` or `Confidence < threshold`). Builds a
   `_review_summary` listing exactly what was flagged and why.

2. `downgrade_flagged_findings(result, bim_data)` — AFTER the agents run, force
   any deterministic PASS/FAIL finding whose `element_id` was flagged to
   NEEDS_REVIEW, surfacing the reason. This is how an uncertain element stops a
   silent (and possibly wrong) verdict: the spec's rule is "any verdict
   depending on an uncertain element resolves to NEEDS_REVIEW, never a guess."

Neither function edits the agents — they read provenance the loader carried and
annotate the dict / findings. The agents stay exactly as they are.

Threshold: pass `threshold=`, else env `REVIEW_CONFIDENCE_THRESHOLD` (default 0.5).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ELEMENT_COLLECTIONS = ("walls", "doors", "windows", "rooms", "stairs", "slabs")


def _default_threshold() -> float:
    try:
        return float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.5"))
    except (TypeError, ValueError):
        return 0.5


def _default_scale_threshold() -> float:
    try:
        return float(os.getenv("SCALE_CONFIDENCE_THRESHOLD", "0.5"))
    except (TypeError, ValueError):
        return 0.5


# ── B2 part 1: pre-pass (annotate + flag) ─────────────────────────────────────
def apply_review_prepass(bim_data: Dict[str, Any],
                         threshold: Optional[float] = None) -> Dict[str, Any]:
    """Annotate `bim_data` in place with review flags and a `_review_summary`."""
    thr = float(threshold if threshold is not None else _default_threshold())
    flagged: List[Dict[str, Any]] = []

    # Issue 4 — global scale gate. If the pixel→mm scale is untrusted, NO
    # dimensional verdict can be trusted, so every element is flagged for review.
    # Only fires when the IFC carried a ScaleConfidence below the threshold;
    # files without scale provenance are unaffected (backward-compatible).
    scale = bim_data.get("scale") or {}
    scale_conf = scale.get("confidence")
    scale_thr = _default_scale_threshold()
    scale_low = scale_conf is not None and float(scale_conf) < scale_thr
    scale_reason = ""
    if scale_low:
        scale_reason = (f"scale confidence {float(scale_conf):.2f} < {scale_thr:.2f} "
                        f"(source: {scale.get('source', '?')}) — dimensional checks "
                        f"unreliable until the plan is re-scaled")

    for coll in _ELEMENT_COLLECTIONS:
        for el in bim_data.get(coll, []) or []:
            prov = el.get("_provenance", {}) or {}
            confidence = float(prov.get("confidence", el.get("confidence", 1.0)))
            source = prov.get("source", "default")
            needs = bool(prov.get("needs_review", el.get("needs_review", False)))
            reason = prov.get("review_reason", "") or ""

            low_conf = confidence < thr
            if low_conf and not reason:
                reason = f"detector confidence {confidence:.2f} < threshold {thr:.2f}"
            if scale_low and not reason:
                reason = scale_reason

            review_needed = needs or low_conf or scale_low
            el["review"] = {
                "needs_review": review_needed,
                "reason":       reason if review_needed else "",
                "confidence":   confidence,
                "source":       source,
            }
            el["needs_review"] = review_needed     # flat flag the agents may read

            if review_needed:
                flagged.append({
                    "collection": coll,
                    "id":         el.get("id"),
                    "reason":     el["review"]["reason"],
                    "confidence": confidence,
                })

    bim_data["_review_summary"] = {
        "threshold":     thr,
        "flagged_count": len(flagged),
        "scale_flagged": scale_low,
        "scale_confidence": scale_conf,
        "flagged":       flagged,
    }
    return bim_data


# ── B2 part 2: post-pass (downgrade dependent verdicts) ───────────────────────
def downgrade_flagged_findings(result: Any, bim_data: Dict[str, Any]) -> Any:
    """Force any PASS/FAIL finding on a flagged element to NEEDS_REVIEW.

    `result` is the ComplianceResult from run_compliance (mutated in place).
    Findings whose `element_id` matches a flagged element are downgraded and the
    review reason is appended to the message; the result summary is recomputed.
    """
    summary = bim_data.get("_review_summary", {}) or {}
    flagged = {f["id"]: (f.get("reason") or "")
               for f in summary.get("flagged", []) if f.get("id")}
    if not flagged:
        summary["downgraded_count"] = 0
        return result

    # Resolve Verdict + summarise from the (already path-bootstrapped) services.
    from numeric_checker import Verdict, summarise

    n_downgraded = 0
    for f in result.findings:
        eid = getattr(f, "element_id", None)
        if eid in flagged and f.verdict in (Verdict.PASS, Verdict.FAIL):
            reason = flagged[eid] or "element flagged uncertain by detector"
            original = f.verdict.value
            # Stage 1 semantics: an untrusted element means the check CANNOT
            # be performed on reliable data — that is a model-quality problem
            # (NOT_EVALUATED, fix the input), not an interpretive judgment
            # call (NEEDS_REVIEW, human decides). CORENET X L2 gating.
            f.verdict = Verdict.NOT_EVALUATED
            f.message = (f"{f.message}  [downgraded {original}→NOT_EVALUATED: "
                         f"{reason}]")
            n_downgraded += 1

    if n_downgraded:
        result.summary = summarise(result.findings)
        logger.info("Honest-degradation: downgraded %d verdict(s) to NOT_EVALUATED "
                    "for flagged elements", n_downgraded)
    # Record the count where the caller/report can surface it, WITHOUT polluting
    # result.summary (which feeds the report's PASS/FAIL/NEEDS_REVIEW counts).
    summary["downgraded_count"] = n_downgraded
    return result
