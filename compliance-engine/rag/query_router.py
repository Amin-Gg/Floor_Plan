"""
services/query_router.py
========================
Stage 2 / Step 4 — rule-based query router.

Decides WHICH transformation (none / hyde / stepback / multi_query) to apply
to a query, using ordered deterministic rules over (a) lexical features of
the query and (b) optionally, the Step 1 confidence verdict on an initial
retrieval. No LLM is involved in routing: every decision is explainable,
reproducible, and free per call.

Rules (first match wins):
  R1  initial retrieval already HIGH-confidence          -> none
  R2  exception-lookup keywords                          -> multi_query
  R3  short query + measurable-property keyword          -> hyde
  R4  2+ distinct building-element types                 -> multi_query
  R5  initial retrieval LOW-confidence                   -> hyde
  R6  default                                            -> none

The `fallback` field is consumed by Step 5's CorrectiveRetriever (escalation
when the primary strategy still yields low confidence); a `--transform auto`
evaluation executes only `primary`.

`score_transform` is forwarded to the confidence evaluator: pass "sigmoid"
when `initial_hits` carry cross-encoder logits (hybrid_retrieve(rerank=True)
output in this codebase), "none" for scores already in [0, 1].
"""

from __future__ import annotations

import re

from rag.retrieval_evaluator import evaluate_retrieval

# ===========================================================================
# Vocabularies — edit in ONE place. Persian terms are stored in their
# ZWNJ-free, space-separated form; the matcher normalizes queries to match.
# ===========================================================================

VOCAB: dict = {
    # R2 — exception-lookup phrasings (FA + EN). Apostrophes are PRESERVED by
    # the normalizer (see _PUNCT) so contractions like "doesn't apply" match;
    # both straight (') and typographic (’) variants are listed.
    "exception_keywords": [
        "استثنا",
        "استثنائات",
        "استثناها",
        "موارد استثنا",
        "مستثنی",
        "exception",
        "exceptions",
        "exempt",
        "exempted",
        "doesn't apply",
        "doesn\u2019t apply",
        "does not apply",
        "not applicable",
        "شامل نمی شود",      # "does not include/apply" (ZWNJ-normalized)
        "اعمال نمی شود",
    ],

    # R3 — measurable property keywords (FA + EN)
    "measurable_properties": [
        "مساحت", "عرض", "ارتفاع",           # spec-required Persian
        "طول", "عمق", "فاصله", "ضخامت", "شیب", "زیربنا",
        "area", "width", "height",           # spec-required English
        "length", "depth", "distance", "thickness", "slope", "dimension",
    ],

    # R4 — building-element types. KEYS are canonical categories; a query
    # must hit >= 2 DISTINCT categories (not 2 synonyms of one category).
    # NOTE: bare Persian "در" (door) is deliberately EXCLUDED — it is
    # homographic with the preposition "in" and would fire on nearly every
    # Persian sentence. Use "درب" and door-phrases instead.
    "element_types": {
        "bedroom":    ["bedroom", "اتاق خواب"],
        "bathroom":   ["bathroom", "toilet", "wc", "حمام", "سرویس بهداشتی",
                       "دستشویی", "توالت"],
        "kitchen":    ["kitchen", "آشپزخانه"],
        "corridor":   ["corridor", "hallway", "راهرو", "کریدور"],
        "stair":      ["stair", "stairs", "staircase", "پله", "راه پله"],
        "elevator":   ["elevator", "lift", "آسانسور"],
        "parking":    ["parking", "garage", "پارکینگ"],
        "balcony":    ["balcony", "terrace", "بالکن", "تراس"],
        "door":       ["door", "درب"],
        "window":     ["window", "پنجره"],
        "living":     ["living room", "نشیمن", "پذیرایی"],
        "basement":   ["basement", "زیرزمین"],
        "roof":       ["roof", "بام", "پشت بام"],
        "ramp":       ["ramp", "رمپ"],
        "exit":       ["exit", "خروجی", "خروج"],
        "wall":       ["wall", "دیوار"],
    },

    # R3 — "short query" thresholds (strict less-than)
    "short_query_max_words": {"fa": 6, "en": 8},
}

_PERSIAN_CHARS = re.compile(r"[\u0600-\u06FF]")
# NOTE: the straight apostrophe (') is intentionally NOT stripped, so English
# contractions in the exception vocabulary ("doesn't apply") survive
# normalization. Quotes, brackets, and FA/EN sentence punctuation are removed.
_PUNCT = re.compile(r"[؟?!.,،;:؛«»\"()\[\]]")


def _normalize(query: str) -> str:
    """Lowercase, ZWNJ -> space, strip punctuation, collapse whitespace."""
    text = query.replace("\u200c", " ").lower()
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def _contains_term(text: str, tokens: set, term: str) -> bool:
    """Multi-word terms match as substrings; single words must match a token
    exactly (prevents e.g. 'in' matching inside other words)."""
    if " " in term:
        return term in text
    return term in tokens


def _matched_element_categories(text: str, tokens: set) -> set:
    matched = set()
    for category, terms in VOCAB["element_types"].items():
        if any(_contains_term(text, tokens, t) for t in terms):
            matched.add(category)
    return matched


def route_query(
    query: str,
    initial_hits: list = None,
    score_transform: str = "none",
) -> dict:
    """Route a query to a transformation strategy.

    Returns {primary, fallback, reason, rule}. `rule` ("R1".."R6") is
    additive beyond the spec'd three keys, for harness logging.
    """
    text = _normalize(query)
    tokens = set(text.split())
    word_count = len(text.split())
    is_fa = bool(_PERSIAN_CHARS.search(query))

    confidence = None
    if initial_hits is not None:
        confidence = evaluate_retrieval(
            query, initial_hits, score_transform=score_transform
        )["confidence"]

    # R1 — already confident: do not spend an LLM call.
    if confidence == "HIGH":
        return {"primary": "none", "fallback": "none",
                "reason": "already-confident", "rule": "R1"}

    # R2 — exception lookups: the governing clause and its exception live in
    # different articles with different vocabulary; union of reformulations
    # (with step-back as escalation) recalls both sides.
    if any(_contains_term(text, tokens, t) for t in VOCAB["exception_keywords"]):
        return {"primary": "multi_query", "fallback": "stepback",
                "reason": "exception-lookup", "rule": "R2"}

    # R3 — short numeric queries: too few terms for lexical recall, and the
    # interrogative register mismatches clause register; HyDE rewrites the
    # need into clause register.
    limit = VOCAB["short_query_max_words"]["fa" if is_fa else "en"]
    has_property = any(
        _contains_term(text, tokens, t) for t in VOCAB["measurable_properties"]
    )
    if word_count < limit and has_property:
        return {"primary": "hyde", "fallback": "multi_query",
                "reason": "short-numeric-query", "rule": "R3"}

    # R4 — multi-element (spatial-relation) queries: gold evidence is often
    # split across per-element articles; multi-query unions them.
    if len(_matched_element_categories(text, tokens)) >= 2:
        return {"primary": "multi_query", "fallback": "stepback",
                "reason": "multi-element-query", "rule": "R4"}

    # R5 — lexical rules silent but retrieval is demonstrably unreliable.
    if confidence == "LOW":
        return {"primary": "hyde", "fallback": "multi_query",
                "reason": "low-confidence-fallback", "rule": "R5"}

    # R6 — default: no transform; HyDE is the Step 5 escalation path.
    return {"primary": "none", "fallback": "hyde",
            "reason": "default-no-transform", "rule": "R6"}
