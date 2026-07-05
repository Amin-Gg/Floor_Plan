"""
services/coverage.py
====================
Issue 8 — honest clause-coverage accounting.

The four agents emit only PASS / FAIL / NEEDS_REVIEW (their `Verdict` enum is
unchanged — this module does NOT modify any agent or the orchestrator). But a
flat NEEDS_REVIEW count hides an important distinction the thesis must be honest
about:

    UNSUPPORTED              the engine has no logic to check this clause at all
                             (clause object/relation/unit outside the engine's
                             capability, or a definition clause)
    BLOCKED_BY_MISSING_DATA  the engine COULD check it, but the required element
                             is absent from the plan (e.g. no kitchen detected,
                             no stair element)
    NEEDS_REVIEW             a real interpretive / site-condition rule that a
                             human must judge (conditional, "confirm on site")

This module reclassifies each finding into one of five coverage classes by
reading the agents' own review messages (their stable output), and rolls them up
to a per-clause coverage table. Findings keep their original verdict; coverage is
an additional, side-by-side accounting.

    cov = build_coverage(result, clauses)         # → dict (see build_coverage)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Coverage classes (5), in report severity order.
PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"
UNSUPPORTED = "UNSUPPORTED"
BLOCKED = "BLOCKED_BY_MISSING_DATA"

COVERAGE_CLASSES = [PASS, FAIL, NEEDS_REVIEW, UNSUPPORTED, BLOCKED]

# Clause-level rollup priority (most-severe wins when a clause has several
# element findings). FAIL first, UNSUPPORTED last.
_PRIORITY = {FAIL: 0, NEEDS_REVIEW: 1, PASS: 2, BLOCKED: 3, UNSUPPORTED: 4}

# Substrings (lowercased) in a NEEDS_REVIEW finding's message that mean the
# engine had NO logic for the clause → UNSUPPORTED.
_UNSUPPORTED_MARKERS = (
    "not mapped to a measurable value",
    "not auto-checkable",
    "unsupported comparator",
    "not implemented",
    "not handled by topology agent",
    "not in a directly checkable ratio form",
    "no entities to check",
    "could not map subject",          # subject/object phrase not in vocabulary
    "could not map subject/object",
)

# Substrings meaning the engine COULD check it but the data was absent → BLOCKED.
_BLOCKED_MARKERS = (
    "rooms in plan to check",         # "No 'room_kitchen' rooms in plan to check"
    "nothing measurable for",
    "could not measure",
    "no stair element detected",
)


def classify_finding(verdict_value: str, message: str) -> str:
    """Map one finding to a coverage class. PASS/FAIL pass through; a
    NEEDS_REVIEW is split into UNSUPPORTED / BLOCKED / NEEDS_REVIEW by message."""
    if verdict_value in (PASS, FAIL):
        return verdict_value
    msg = (message or "").lower()
    for m in _UNSUPPORTED_MARKERS:
        if m in msg:
            return UNSUPPORTED
    for m in _BLOCKED_MARKERS:
        if m in msg:
            return BLOCKED
    return NEEDS_REVIEW          # genuine interpretive / site-condition review


def _finding_fields(f: Any) -> tuple:
    """Accept either a Finding dataclass or its to_dict()."""
    if isinstance(f, dict):
        v = f.get("verdict", NEEDS_REVIEW)
        return str(v), str(f.get("message", "")), f.get("article_id", "?")
    v = f.verdict.value if hasattr(f.verdict, "value") else str(f.verdict)
    return v, str(getattr(f, "message", "")), getattr(f, "article_id", "?")


def build_coverage(result: Any,
                   clauses: List[Dict[str, Any]],
                   corpus_total: Optional[int] = None) -> Dict[str, Any]:
    """Build the clause-coverage table from a ComplianceResult (or its dict).

    Returns a dict:
        {
          total_clauses, automatically_checkable, checked,
          passed, failed, needs_review, unsupported, blocked_by_missing_data,
          findings_by_class: {CLASS: n, ...},
          by_clause: {article_id: CLASS, ...},
          corpus_total (optional), not_applicable (optional)
        }
    Counts are at the CLAUSE level (one status per clause); findings_by_class is
    the finer per-finding tally for the report.
    """
    findings = result.get("findings", []) if isinstance(result, dict) else result.findings

    # Per-finding class tally + group statuses by clause.
    findings_by_class = {c: 0 for c in COVERAGE_CLASSES}
    statuses_by_clause: Dict[str, List[str]] = {}
    for f in findings:
        v, msg, art = _finding_fields(f)
        cls = classify_finding(v, msg)
        findings_by_class[cls] += 1
        statuses_by_clause.setdefault(str(art), []).append(cls)

    # Roll up to one status per clause. A clause in the corpus that produced NO
    # finding at all (definitions, rule types no agent handles) is UNSUPPORTED.
    # Coverage is strictly per-clause: aggregate findings whose article_id is not
    # a real clause (e.g. global egress/light checks) are counted in
    # findings_by_class but never inflate the clause total.
    by_clause: Dict[str, str] = {}
    for clause in clauses:
        art = str(clause.get("article_id", "?"))
        statuses = statuses_by_clause.get(art)
        if not statuses:
            by_clause[art] = UNSUPPORTED
        else:
            by_clause[art] = min(statuses, key=lambda s: _PRIORITY[s])

    tally = {c: 0 for c in COVERAGE_CLASSES}
    for status in by_clause.values():
        tally[status] += 1

    total = len(by_clause)
    passed, failed = tally[PASS], tally[FAIL]
    needs_review, unsupported, blocked = (tally[NEEDS_REVIEW],
                                          tally[UNSUPPORTED], tally[BLOCKED])
    checked = passed + failed + needs_review
    automatically_checkable = total - unsupported

    cov = {
        "total_clauses": total,
        "automatically_checkable": automatically_checkable,
        "checked": checked,
        "passed": passed,
        "failed": failed,
        "needs_review": needs_review,
        "unsupported": unsupported,
        "blocked_by_missing_data": blocked,
        "findings_by_class": findings_by_class,
        "by_clause": by_clause,
    }
    if corpus_total is not None:
        cov["corpus_total"] = corpus_total
        cov["not_applicable"] = max(corpus_total - total, 0)  # filtered skip_category
    return cov
