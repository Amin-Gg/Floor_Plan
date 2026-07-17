"""Internal deterministic compliance runner.

This module is not a public ingestion boundary.  Raw IFC, raw ``bim_data`` and
operator-supplied values must enter through
:func:`services.validation_pipeline.run_validation_pipeline`, where Manual
Inputs v1 validation, provenance resolution and Quality checks run first.

The private ``_run_compliance_core`` function consumes only the already
prepared legacy agent seam produced by that authoritative pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from services.numeric_checker import NumericChecker, Finding, Verdict, summarise, assign_finding_ordinals
from domain.findings import FindingStage
from domain.validation import ValidationResult
from services.topology_agent import TopologyAgent
from services.opening_agent import OpeningAgent
from services.safety_agent import SafetyAgent

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

    def as_validation_result(self) -> ValidationResult:
        return ValidationResult(
            stage=FindingStage.COMPLIANCE,
            findings=self.findings,
            checker_version="compliance-stage8-phase1",
            metadata={"summary": dict(self.summary),
                      "duration_s": round(self.duration_s, 3)},
        )

    @property
    def status(self) -> str:
        return self.as_validation_result().status

    def to_dict(self) -> Dict[str, Any]:
        stage = self.as_validation_result().to_dict()
        # Preserve the Stage-8 payload while exposing the shared stage contract.
        stage.update({
            "summary":    self.summary,
            "duration_s": round(self.duration_s, 3),
            "by_agent":   {k: [f.to_dict() for f in v] for k, v in self.by_agent.items()},
            "findings":   [f.to_dict() for f in self.findings],
        })
        return stage


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

def _try_build_default_retriever():
    """Best-effort construction of the production retriever (Step 7 factory).

    Resolves rag_retriever in either repository layout (services.* package
    or flat services/ sys.path) and returns None on ANY failure — the
    orchestrator must keep working fully offline.
    """
    try:
        try:
            from rag.rag_retriever import build_default_retriever
        except ImportError:
            from rag_retriever import build_default_retriever
        return build_default_retriever()
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the run
        logger.warning("default retriever unavailable (%s); "
                       "LLM pass will run without RAG context", exc)
        return None

def _llm_review_interpretive(
    findings: List[Finding],
    clauses_by_id: Dict[str, Dict[str, Any]],
    retriever: Optional[Any],
    llm: Optional[Callable[[str], str]],
) -> None:
    """
    For each NEEDS_REVIEW *clause*, optionally call the LLM with RAG context to
    add an advisory note. Mutates findings in place by appending to .message.
    NEVER changes the verdict away from NEEDS_REVIEW (human queue decides).
    Deterministic PASS/FAIL findings are left untouched.

    Budget discipline (review fix M3, 2026-07): findings are per-ELEMENT, so a
    plan with 5 bedrooms used to trigger 5 identical retrievals + 5 identical
    Groq calls for the same clause. Findings are now grouped by article_id —
    ONE retrieval and ONE LLM call per clause — and the note fans out to every
    element finding of that clause. On the 9-key daily token budget this is
    the difference between a viable pass and an exhausted one.
    """
    if llm is None:
        return  # no LLM configured → interpretive items stay NEEDS_REVIEW

    # Group review findings by clause, preserving first-seen clause order.
    # Stage 7: UNSUPPORTED findings (Finding.unsupported=True — engine
    # limitation: unmapped vocabulary, unhandled relation, unsupported
    # comparator/unit) are EXCLUDED. An advisory note cannot help there: the
    # reviewer must check the clause manually regardless, and on a 328-clause
    # corpus these were the bulk of the pass's spend (~200 of ~300 review
    # findings on the reference fixture). LLM budget now goes only to
    # genuinely interpretive clauses, where a note changes reviewer speed.
    by_clause: Dict[str, List[Finding]] = {}
    n_unsupported_skipped = 0
    for f in findings:
        if f.verdict != Verdict.NEEDS_REVIEW:
            continue
        if getattr(f, "unsupported", False):
            n_unsupported_skipped += 1
            continue
        by_clause.setdefault(f.article_id, []).append(f)
    if n_unsupported_skipped:
        logger.info("LLM pass: skipped %d unsupported finding(s) — advisory "
                    "notes target interpretive clauses only",
                    n_unsupported_skipped)

    for article_id, group in by_clause.items():
        clause = clauses_by_id.get(article_id, {})
        rule_text = clause.get("text_en") or group[0].rule_text_en or ""

        # Pull supporting regulation context ONCE per clause.
        context = ""
        if retriever is not None and rule_text:
            try:
                hits = retriever.retrieve(rule_text[:120], top_k=2)
                context = "\n".join(h.get("text_en", "") for h in hits if h.get("text_en"))
            except Exception as exc:
                logger.warning("RAG retrieval failed for %s: %s", article_id, exc)

        # Element-level reasons: up to three distinct messages so the note can
        # reflect why the elements were flagged without an unbounded prompt.
        reasons = []
        for f in group:
            if f.message not in reasons:
                reasons.append(f.message)
            if len(reasons) == 3:
                break
        why = "\n".join(f"- {r}" for r in reasons)
        if len(group) > len(reasons):
            why += f"\n- (and {len(group) - len(reasons)} more elements flagged for this clause)"

        prompt = (
            "You are a building-code compliance assistant. A deterministic checker "
            "could not automatically verify the following rule and flagged it for "
            "human review. Using ONLY the regulation context provided, give a brief "
            "(1-2 sentence) advisory note on what a human reviewer should check. "
            "Do NOT invent thresholds.\n\n"
            f"Rule: {rule_text}\n"
            f"Why flagged ({len(group)} element(s)):\n{why}\n"
            f"Regulation context:\n{context}\n"
        )
        try:
            advice = llm(prompt)
            if advice:
                note = advice.strip()
                for f in group:  # one call, fanned out to every element finding
                    f.message = f"{f.message}  [AI note: {note}]"
        except Exception as exc:
            logger.warning("LLM review failed for %s: %s", article_id, exc)


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
# Internal prepared-input runner
# ═══════════════════════════════════════════════════════════════════════════

def _run_compliance_core(
    bim_data: Dict[str, Any],
    clauses: List[Dict[str, Any]],
    retriever: Optional[Any] = None,
    llm: Optional[Callable[[str], str]] = None,
    use_langgraph: bool = True,
) -> ComplianceResult:
    """Run deterministic agents on a prepared internal agent seam.

    This private function deliberately has no ``building_params`` argument and
    performs no input merging.  Values in ``bim_data`` must already have been
    validated and resolved by Manual Inputs v1 in the unified pipeline.
    """
    t0 = time.time()

    # Build the spatial graph once and share it across agents.
    from services.spatial_graph import SpatialGraph
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

    # Phase 1 identity migration: enrich every compliance finding with the
    # canonical internal ID, original IFC GlobalId and stable model fingerprint.
    from validation.compliance.adapter import enrich_findings_with_engine_identity
    enrich_findings_with_engine_identity(findings, bim_data)

    # Optional LLM interpretive pass over NEEDS_REVIEW items.
    # Stage 1 / Step 7: when no retriever was injected but an LLM is
    # configured, build the production default (hybrid + rerank) via the
    # factory. Failure to build (no DB, missing deps) degrades to the
    # previous behaviour: the LLM runs without RAG context. The retriever
    # is used ONLY for advisory notes — deterministic verdicts never
    # depend on it.
    if retriever is None and llm is not None:
        retriever = _try_build_default_retriever()
    clauses_by_id = {c.get("article_id"): c for c in clauses}
    _llm_review_interpretive(findings, clauses_by_id, retriever, llm)

    # Stage 2: assign deterministic ordinals so every finding_id (and thus
    # every BCF topic GUID) is unique within the run and stable across runs.
    assign_finding_ordinals(findings)

    result = ComplianceResult(
        findings=findings,
        by_agent=by_agent,
        summary=summarise(findings),
        duration_s=time.time() - t0,
    )
    logger.info("Compliance run complete: %s in %.2fs",
                result.summary, result.duration_s)
    return result