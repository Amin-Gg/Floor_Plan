"""
services/graph_retriever.py
===========================
Stage 3, Step 4 — GraphRetriever: graph-aware retrieval over the
regulation graph, layered on the Stage 1/2 vector stack.

The headline mechanism: pure vector retrieval ranks clauses by textual
similarity, so it structurally misses (a) exception clauses that modify a
retrieved base rule while sharing almost none of its vocabulary, and
(b) clauses that govern an element the query names but describe it in
different words. This wrapper adds both, deterministically and with ZERO
additional LLM calls:

  A: rule-based bilingual element extraction from the query (no LLM)
  B: seed retrieval through the wrapped base retriever (the full Stage 1/2
     stack — hybrid RRF + cross-encoder, optionally CRAG-corrected)
  C: HAS_EXCEPTION expansion of the seed hits via the GraphLinker
  D: GOVERNS-edge candidates for every element the query mentions
  E: RRF fusion of the three ranked lists (seed, expanded, graph)
  F: cross-encoder rerank of the fused candidate pool against the
     original query; final hits carry a 'provenance' tag

Drop-in compatibility: retrieve() keeps the frozen MabhasRetriever
signature — (query, top_k=3, mabhas_part=None, rule_type=None) — and the
established hit-dict shape (mabhas_part, article_id, heading_fa, text_fa,
text_en, rule_type, entities, context_fa, score; additive keys rrf_score
and provenance). NOTE: the Step 4 spec sketch showed top_k=5, but the
frozen contract the four deterministic agents depend on is top_k=3;
the frozen contract wins (documented deviation).

Graph-candidate ranking decision (spec step E): graph candidates have no
intrinsic query-relevance order, but RRF needs one. We rank them by
DESCENDING CLAUSE OUT-DEGREE in the regulation graph (article_id as the
deterministic tiebreak) rather than assigning one uniform low rank.
Rationale: (1) a uniform rank would make RRF contributions depend on an
arbitrary alphabetical order while pretending not to order at all;
(2) out-degree is a cheap, deterministic centrality proxy — a clause that
governs the element AND constrains properties AND carries occupancy edges
encodes a denser checkable requirement than one with a single incidental
edge; (3) the cross-encoder controls the FINAL ordering anyway (spec
step F), so degree ranking only decides which graph candidates survive
the candidate_k cutoff — exactly where a centrality prior is appropriate
and a relevance claim is not.

Filter semantics: agent-supplied mabhas_part / rule_type filters are
honoured on graph-added candidates too (the base applies them at the SQL
level; we apply the same predicate to graph hits). Consequently an
explicit rule_type="numeric" filter excludes rule_type="exception"
expansion hits — by contract, a caller who filters by rule type has asked
for exactly that rule type. The eval harness and the verdict agents'
default calls pass no filters, so exception expansion is active there.

Imports of GraphLinker are type-checking-only (same pattern as
crag_retriever) so the orchestration is unit-testable against fakes.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from rag.rag_retriever import _build_rerank_passage, rrf_fuse

if TYPE_CHECKING:  # pragma: no cover
    from services.graph_linker import GraphLinker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A — rule-based bilingual element extraction
# ---------------------------------------------------------------------------
# Canonical element -> query surface forms (Persian + English), matched with
# word boundaries (Persian: no Persian letter / ZWNJ adjacent; English: \b,
# case-insensitive). High-precision by design:
#   * bare «در» is EXCLUDED — it is the door/"in" homograph resolved in
#     Stage 2 (query_router); only «درب», «درها», «در ورودی» count as door.
#   * bare "room"/«اتاق» and "building"/«ساختمان» are EXCLUDED — they occur
#     in a large share of queries and would dump broad clause sets into
#     every retrieval, hurting precision without adding signal.
#   * «نورگیر» (light well) does not collide with «نورگیری» (lighting):
#     the trailing-letter boundary rejects the longer word.
QUERY_ELEMENT_VOCAB: Dict[str, Dict[str, List[str]]] = {
    "kitchen":      {"fa": ["آشپزخانه"], "en": ["kitchen"]},
    "bedroom":      {"fa": ["اتاق خواب", "اتاق‌خواب", "فضای اقامت",
                            "فضاهای اقامت"],
                     "en": ["bedroom", "bedrooms", "dormitory",
                            "dwelling space", "dwelling spaces"]},
    "living_room":  {"fa": ["نشیمن", "پذیرایی", "اتاق چند منظوره"],
                     "en": ["living room", "multi-purpose room",
                            "multipurpose room"]},
    "bathroom":     {"fa": ["حمام", "سرویس بهداشتی", "فضای بهداشتی",
                            "فضاهای بهداشتی", "توالت", "دستشویی"],
                     "en": ["bathroom", "bathrooms", "toilet", "toilets",
                            "sanitary space", "sanitary spaces", "wc",
                            "washbasin"]},
    "stair":        {"fa": ["پله", "پله‌ها", "پلکان", "راه پله", "راه‌پله",
                            "راه پله‌ها"],
                     "en": ["stair", "stairs", "staircase", "stairway",
                            "stairways", "stairwell"]},
    "ramp":         {"fa": ["شیب راه", "شیب‌راه", "شیبراه", "رمپ"],
                     "en": ["ramp", "ramps"]},
    "landing":      {"fa": ["پاگرد"], "en": ["landing", "landings"]},
    "corridor":     {"fa": ["راهرو", "راهروها", "کریدور"],
                     "en": ["corridor", "corridors", "hallway"]},
    "door":         {"fa": ["درب", "درها", "در ورودی", "درهای"],
                     "en": ["door", "doors"]},
    "window":       {"fa": ["پنجره", "پنجره‌ها", "بازشو", "بازشوها"],
                     "en": ["window", "windows", "glazing", "skylight"]},
    "balcony":      {"fa": ["بالکن", "بالکن‌ها", "تراس", "ایوان", "مهتابی"],
                     "en": ["balcony", "balconies", "terrace", "iwan",
                            "veranda"]},
    "courtyard":    {"fa": ["حیاط", "حیاط‌ها"],
                     "en": ["courtyard", "courtyards"]},
    "light_well":   {"fa": ["پاسیو", "نورگیر"],
                     "en": ["light well", "lightwell", "light wells"]},
    "basement":     {"fa": ["زیرزمین", "زیرزمین‌ها"],
                     "en": ["basement", "basements"]},
    "parking":      {"fa": ["پارکینگ", "توقفگاه", "توقفگاه‌ها"],
                     "en": ["parking", "garage"]},
    "elevator":     {"fa": ["آسانسور"], "en": ["elevator", "elevators",
                                               "lift"]},
    "roof":         {"fa": ["بام", "پشت بام", "پشت‌بام"],
                     "en": ["roof"]},
    "storage":      {"fa": ["انبار"], "en": ["storage", "storeroom"]},
    "entrance":     {"fa": ["ورودی"], "en": ["entrance", "lobby",
                                             "vestibule"]},
    "dwelling_unit": {"fa": ["آپارتمان", "واحد مسکونی"],
                      "en": ["apartment", "apartments"]},
}

_FA_BOUNDARY = r"(?<![\u0600-\u06FF\u200c]){}(?![\u0600-\u06FF\u200c])"


def _compile_vocab() -> Dict[str, List[re.Pattern]]:
    compiled: Dict[str, List[re.Pattern]] = {}
    for element, forms in QUERY_ELEMENT_VOCAB.items():
        pats = [re.compile(_FA_BOUNDARY.format(re.escape(t)))
                for t in forms.get("fa", [])]
        pats += [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
                 for t in forms.get("en", [])]
        compiled[element] = pats
    return compiled


_COMPILED_VOCAB = _compile_vocab()


# ---------------------------------------------------------------------------
# The retriever
# ---------------------------------------------------------------------------

class GraphRetriever:
    """Graph-aware wrapper around a MabhasRetriever or CorrectiveRetriever.

    Parameters
    ----------
    base : the wrapped retriever; its .retrieve() supplies the vector seed.
    linker : services.graph_linker.GraphLinker over the Step 2 GraphML.
    clauses_by_id : article_id -> clause dict (the contextual corpus JSON);
        used to build full, shape-compatible hit dicts for candidates that
        arrive from the graph rather than from the database.
    candidate_k : fused-pool size passed to the cross-encoder (default 20).
    """

    def __init__(self, base: Any, linker: "GraphLinker",
                 clauses_by_id: Dict[str, Dict[str, Any]],
                 candidate_k: int = 20):
        self.base = base
        self.linker = linker
        self.clauses_by_id = clauses_by_id
        self.candidate_k = candidate_k
        self.last_graph_trace: Dict[str, Any] = {}

    # — pass-throughs so existing instrumentation keeps working —————————————
    @property
    def last_trace(self):
        """CRAG trace of the wrapped retriever (when it is the CRAG layer)."""
        return getattr(self.base, "last_trace", None)

    @property
    def last_rerank_seconds(self):
        return getattr(self.base, "last_rerank_seconds", None)

    @last_rerank_seconds.setter
    def last_rerank_seconds(self, value):
        if hasattr(self.base, "last_rerank_seconds"):
            self.base.last_rerank_seconds = value

    # — element extraction ——————————————————————————————————————————————————
    def _extract_elements(self, query: str) -> Set[str]:
        """Canonical element types the query mentions (rule-based, no LLM)."""
        text = query or ""
        return {element for element, patterns in _COMPILED_VOCAB.items()
                if any(p.search(text) for p in patterns)}

    # — filters ——————————————————————————————————————————————————————————————
    def _passes_filters(self, article_id: str,
                        mabhas_part: Optional[str],
                        rule_type: Optional[str]) -> bool:
        clause = self.clauses_by_id.get(article_id)
        if clause is None:
            return False                      # cannot build a shaped hit
        if mabhas_part is not None and \
                str(clause.get("mabhas_part")) != str(mabhas_part):
            return False
        if rule_type is not None and clause.get("rule_type") != rule_type:
            return False
        return True

    def _hit_from_clause(self, article_id: str) -> Dict[str, Any]:
        c = self.clauses_by_id[article_id]
        return {
            "mabhas_part": c.get("mabhas_part"),
            "article_id": article_id,
            "heading_fa": c.get("heading_fa"),
            "text_fa": c.get("text_fa"),
            "text_en": c.get("text_en"),
            "rule_type": c.get("rule_type"),
            "entities": c.get("entities"),
            "context_fa": c.get("context_fa"),
            "text_fa_normalized": c.get("text_fa_normalized"),
            "score": 0.0,
        }

    def _clause_degree(self, article_id: str) -> int:
        node = f"clause:{article_id}"
        G = getattr(self.linker, "G", None)
        if G is None or node not in G:
            return 0
        return G.out_degree(node)

    # — the drop-in entry point ——————————————————————————————————————————————
    def retrieve(self, query: str, top_k: int = 3,
                 mabhas_part: Optional[str] = None,
                 rule_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Frozen MabhasRetriever signature; see module docstring."""
        # A — element extraction (deterministic, zero LLM calls)
        elements = self._extract_elements(query)

        # B — vector seed through the wrapped Stage 1/2 stack
        seed_k = max(top_k, 5)               # same signal floor as CRAG
        seed = self.base.retrieve(query, top_k=seed_k,
                                  mabhas_part=mabhas_part,
                                  rule_type=rule_type)
        seed_ids = [h["article_id"] for h in seed]

        # C — HAS_EXCEPTION expansion of the seed (filter-checked)
        expanded_ids = [
            a for a in self.linker.expand_with_exceptions(seed_ids)
            if a in seed_ids or self._passes_filters(a, mabhas_part, rule_type)
        ]
        added_by_exception = [a for a in expanded_ids if a not in seed_ids]

        # D — graph-side candidates for every mentioned element
        graph_ids: List[str] = []
        seen: Set[str] = set()
        for element in sorted(elements):
            for a in self.linker.clauses_for_element(
                    element, include_exceptions=True):
                if a not in seen and self._passes_filters(
                        a, mabhas_part, rule_type):
                    seen.add(a)
                    graph_ids.append(a)
        # degree-ranked (see module docstring), article_id tiebreak
        graph_ids.sort(key=lambda a: (-self._clause_degree(a), a))

        # E — RRF fusion of the three ranked lists
        fused = rrf_fuse([seed_ids, expanded_ids, graph_ids], rrf_k=60)
        candidate_ids = sorted(fused, key=lambda a: (-fused[a], a))
        candidate_ids = candidate_ids[:max(self.candidate_k, top_k)]

        # build shape-compatible hits + provenance tags
        by_id: Dict[str, Dict[str, Any]] = {h["article_id"]: dict(h)
                                            for h in seed}
        seed_set, exc_set = set(seed_ids), set(added_by_exception)
        candidates: List[Dict[str, Any]] = []
        for a in candidate_ids:
            hit = by_id.get(a)
            if hit is None:
                if a not in self.clauses_by_id:   # defensive; logged
                    logger.warning("graph candidate %s missing from "
                                   "clauses_by_id — dropped", a)
                    continue
                hit = self._hit_from_clause(a)
            hit["score"] = fused[a]
            hit["provenance"] = ("vector" if a in seed_set
                                 else "exception_expansion" if a in exc_set
                                 else "graph_element")
            candidates.append(hit)

        # F — cross-encoder rerank against the ORIGINAL query
        final = self._rerank(query, candidates, top_k)

        self.last_graph_trace = {
            "elements_detected": sorted(elements),
            "seed_n": len(seed_ids),
            "exception_added_n": len(added_by_exception),
            "graph_candidates_n": len(graph_ids),
            "fused_pool_n": len(candidates),
            # full id lists for provenance dumps / thesis figures
            "seed_ids": list(seed_ids),
            "exception_added_ids": list(added_by_exception),
            "graph_candidate_ids": list(graph_ids),
            "fused_pool_ids": [h["article_id"] for h in candidates],
            "final_provenance": {
                p: sum(1 for h in final if h.get("provenance") == p)
                for p in ("vector", "graph_element", "exception_expansion")
            },
            "llm_calls_added": 0,            # the graph layer is LLM-free
        }
        return final

    # — rerank (mirrors MabhasRetriever._rerank_against, DB-free) ————————————
    def _rerank(self, query: str, candidates: List[Dict[str, Any]],
                top_k: int) -> List[Dict[str, Any]]:
        if not candidates:
            return []
        import time

        from rag.reranker import rerank as _ce_rerank

        passages = [_build_rerank_passage(h) for h in candidates]
        t0 = time.perf_counter()
        ce_scores = _ce_rerank(query, passages)
        self.last_rerank_seconds = time.perf_counter() - t0

        for hit, ce in zip(candidates, ce_scores):
            hit["rrf_score"] = hit["score"]
            hit["score"] = float(ce)
        candidates.sort(key=lambda h: (-h["score"], h["article_id"]))
        return candidates[:top_k]


# ---------------------------------------------------------------------------
# Loader convenience for the factory and the eval harness
# ---------------------------------------------------------------------------

def resolve_clauses_file(preferred: str = "data/mabhas_clauses_contextual.json",
                         fallback: str = "data/mabhas_clauses_normalized.json",
                         ) -> str:
    """
    Pick the clause corpus file for graph-layer hit reconstruction.

    The contextual corpus (Stage 1 / Step 5 output of RAG/contextualize.py)
    is preferred because its context_fa improves the cross-encoder passages,
    but it is generated locally with an API key and may be absent from a
    fresh checkout. The normalized corpus is a strict subset of the same
    clauses (same article_ids, no context_fa), so falling back to it keeps
    the graph layer fully functional — only the rerank passages lose the
    prepended context. A warning is logged so the degradation is visible.
    """
    import logging
    import os.path

    if os.path.exists(preferred):
        return preferred
    if os.path.exists(fallback):
        logging.getLogger(__name__).warning(
            "%s not found — falling back to %s (run RAG/contextualize.py to "
            "regenerate the contextual corpus; rerank passages will lack "
            "context_fa until then)", preferred, fallback)
        return fallback
    # Neither exists: return the preferred path so open() raises a clear
    # FileNotFoundError naming the canonical expected file.
    return preferred


def load_clauses_by_id(path: str) -> Dict[str, Dict[str, Any]]:
    """article_id -> clause dict for the ingestable corpus subset."""
    import json

    with open(path, encoding="utf-8") as fh:
        clauses = json.load(fh)
    return {c["article_id"]: c for c in clauses if not c.get("skip_category")}
