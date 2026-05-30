"""
services/orchestrator.py
========================
Step 8 — Compliance Orchestrator

Ties the four deterministic agents together into a single pipeline call, then
runs an optional LLM pass over the interpretive (NEEDS_REVIEW) clauses using
RAG context. One entry point:

    result = run_compliance(bim_data, clauses, retriever=..., llm=...)

`result` is a ComplianceResult with:
    .findings   list[Finding]      — every finding from every agent
    .summary    dict               — PASS / FAIL / NEEDS_REVIEW counts
    .by_agent   dict[str, list]    — findings grouped by agent
    .duration_s float

Design notes
------------
* Uses LangGraph when installed (parallel agent fan-out + merge), but falls
  back to plain sequential Python if LangGraph is absent — so the pipeline runs
  in ANY environment and the tests never depend on a heavy library.
* The LLM step is OPTIONAL. With no llm callable supplied, interpretive clauses
  simply stay NEEDS_REVIEW (the safe default). When an llm callable IS given,
  it reasons over each interpretive clause WITH the RAG-retrieved regulation
  text, and may downgrade NEEDS_REVIEW → a suggested verdict — but the result
  is always tagged llm_suggested=True and the verdict stays NEEDS_REVIEW for
  the human queue. The LLM never overrides a deterministic PASS/FAIL.
* Deterministic verdicts are sacred: the orchestrator never lets the LLM change
  a numeric or spatial PASS/FAIL. The LLM only adds advisory context to items
  the deterministic agents already could not resolve.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from numeric_checker import NumericChecker, Finding, Verdict, summarise
from topology_agent import TopologyAgent
from opening_agent import OpeningAgent
from safety_agent import SafetyAgent

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Result container
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ComplianceResult:
    findings:   List[Finding] = field(default_factory=list)
    by_agent:   Dict[str, List[Finding]] = field(default_factory=dict)
    summary:    Dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary":    self.summary,
            "duration_s": round(self.duration_s, 3),
            "by_agent":   {k: [f.to_dict() for f in v] for k, v in self.by_agent.items()},
            "findings":   [f.to_dict() for f in self.findings],
        }


# ═══════════════════════════════════════════════════════════════════════════
# Agent runners (each returns a list[Finding] tagged with its agent name)
# ═══════════════════════════════════════════════════════════════════════════

def _run_numeric(bim_data, clauses, sg) -> List[Finding]:
    numeric = [c for c in clauses if c.get("rule_type") == "numeric"]
    return NumericChecker(bim_data).check_all(numeric)


def _run_topology(bim_data, clauses, sg) -> List[Finding]:
    spatial = [c for c in clauses if c.get("rule_type") == "spatial"]
    return TopologyAgent(sg).check_all(spatial)


def _run_opening(bim_data, clauses, sg) -> List[Finding]:
    oa = OpeningAgent(sg)
    return oa.check_all(clauses) + oa.check_light_presence()


def _run_safety(bim_data, clauses, sg) -> List[Finding]:
    sa = SafetyAgent(sg, bim_data)
    return sa.check_all(clauses) + sa.check_egress_all_rooms()


AGENT_RUNNERS: Dict[str, Callable] = {
    "numeric":  _run_numeric,
    "topology": _run_topology,
    "opening":  _run_opening,
    "safety":   _run_safety,
}


# ═══════════════════════════════════════════════════════════════════════════
# LLM interpretive pass (optional)
# ═══════════════════════════════════════════════════════════════════════════

def _llm_review_interpretive(
    findings: List[Finding],
    clauses_by_id: Dict[str, Dict[str, Any]],
    retriever: Optional[Any],
    llm: Optional[Callable[[str], str]],
) -> None:
    """
    For each NEEDS_REVIEW finding, optionally call the LLM with RAG context to
    add an advisory note. Mutates findings in place by appending to .message.
    NEVER changes the verdict away from NEEDS_REVIEW (human queue decides).
    Deterministic PASS/FAIL findings are left untouched.
    """
    if llm is None:
        return  # no LLM configured → interpretive items stay NEEDS_REVIEW

    for f in findings:
        if f.verdict != Verdict.NEEDS_REVIEW:
            continue
        clause = clauses_by_id.get(f.article_id, {})
        rule_text = clause.get("text_en") or f.rule_text_en or ""

        # Pull supporting regulation context if a retriever is available
        context = ""
        if retriever is not None and rule_text:
            try:
                hits = retriever.retrieve(rule_text[:120], top_k=2)
                context = "\n".join(h.get("text_en", "") for h in hits if h.get("text_en"))
            except Exception as exc:
                logger.warning("RAG retrieval failed for %s: %s", f.article_id, exc)

        prompt = (
            "You are a building-code compliance assistant. A deterministic checker "
            "could not automatically verify the following rule and flagged it for "
            "human review. Using ONLY the regulation context provided, give a brief "
            "(1-2 sentence) advisory note on what a human reviewer should check. "
            "Do NOT invent thresholds.\n\n"
            f"Rule: {rule_text}\n"
            f"Why flagged: {f.message}\n"
            f"Regulation context:\n{context}\n"
        )
        try:
            advice = llm(prompt)
            if advice:
                f.message = f"{f.message}  [AI note: {advice.strip()}]"
        except Exception as exc:
            logger.warning("LLM review failed for %s: %s", f.article_id, exc)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration — LangGraph if available, else sequential fallback
# ═══════════════════════════════════════════════════════════════════════════

def _run_agents_langgraph(bim_data, clauses, sg) -> Dict[str, List[Finding]]:
    """Run the four agents as a LangGraph fan-out/merge. Falls back on error."""
    from langgraph.graph import StateGraph, END
    from typing import TypedDict

    class State(TypedDict, total=False):
        numeric:  List[Finding]
        topology: List[Finding]
        opening:  List[Finding]
        safety:   List[Finding]

    g = StateGraph(State)

    def make_node(name):
        def node(state):
            return {name: AGENT_RUNNERS[name](bim_data, clauses, sg)}
        return node

    for name in AGENT_RUNNERS:
        g.add_node(name, make_node(name))
        g.add_edge("__start__", name)   # all four start in parallel
        g.add_edge(name, END)

    app = g.compile()
    out = app.invoke({})
    return {k: out.get(k, []) for k in AGENT_RUNNERS}


def _run_agents_sequential(bim_data, clauses, sg) -> Dict[str, List[Finding]]:
    """Plain-Python fallback: run each agent in turn."""
    return {name: runner(bim_data, clauses, sg)
            for name, runner in AGENT_RUNNERS.items()}


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def run_compliance(
    bim_data: Dict[str, Any],
    clauses: List[Dict[str, Any]],
    retriever: Optional[Any] = None,
    llm: Optional[Callable[[str], str]] = None,
    use_langgraph: bool = True,
) -> ComplianceResult:
    """
    Run the full compliance pipeline on one floor plan.

    Parameters
    ----------
    bim_data : dict
        The bim_data dict from your model (or synthetic for testing).
    clauses : list[dict]
        The ingested Mabhas clauses (numeric + spatial). Definitions/exceptions
        are ignored by the agents automatically.
    retriever : MabhasRetriever, optional
        For pulling RAG context during the LLM interpretive pass.
    llm : callable(str) -> str, optional
        Any function that takes a prompt and returns text. If None, interpretive
        clauses stay NEEDS_REVIEW (safe default — fully offline).
    use_langgraph : bool
        Use LangGraph fan-out if installed; otherwise sequential.

    Returns
    -------
    ComplianceResult
    """
    t0 = time.time()

    # Build the spatial graph once and share it across agents.
    from spatial_graph import SpatialGraph
    sg = SpatialGraph(bim_data)

    # Run the four agents (parallel via LangGraph, or sequential fallback).
    by_agent: Dict[str, List[Finding]] = {}
    if use_langgraph:
        try:
            by_agent = _run_agents_langgraph(bim_data, clauses, sg)
        except Exception as exc:
            logger.warning("LangGraph path failed (%s); using sequential fallback", exc)
            by_agent = _run_agents_sequential(bim_data, clauses, sg)
    else:
        by_agent = _run_agents_sequential(bim_data, clauses, sg)

    # Merge all findings.
    findings: List[Finding] = []
    for agent_findings in by_agent.values():
        findings.extend(agent_findings)

    # Optional LLM interpretive pass over NEEDS_REVIEW items.
    clauses_by_id = {c.get("article_id"): c for c in clauses}
    _llm_review_interpretive(findings, clauses_by_id, retriever, llm)

    result = ComplianceResult(
        findings=findings,
        by_agent=by_agent,
        summary=summarise(findings),
        duration_s=time.time() - t0,
    )
    logger.info("Compliance run complete: %s in %.2fs",
                result.summary, result.duration_s)
    return result
