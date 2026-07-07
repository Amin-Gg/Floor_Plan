"""
services/query_transforms.py
============================
Stage 2 — LLM query transformations.

Step 2: hyde_transform        (HyDE, Gao et al. 2022)
Step 3: stepback_transform    (Step-Back Prompting, Zheng et al. 2023)
        multi_query_transform (query expansion via reformulation)

Design notes
------------
- All transforms call rag.llm_client.llm_chat — the provider-agnostic seam
  (Groq qwen/qwen3-32b by default; AgentRouter when configured — see
  rag/llm_client.py for the LLM_PROVIDER resolution rules).
- Every API call increments module-level counters so evaluation runs can
  audit LLM cost (project-wide constraint). multi_query_transform makes ONE
  call for all reformulations, so every transform costs exactly 1 call/query.
- language="auto" (default) detects Persian script vs Latin. This matters
  because the production consumer queries in ENGLISH (the orchestrator's
  LLM interpretive pass retrieves with the clause's English rule text),
  while the eval set and the Persian-facing UI query in Persian — callers
  with a frozen signature (CRAG's retrieve()) cannot plumb a language flag.
- Transforms are advisory pre-processing only: on any failure they fall back
  to the original query and log a warning. They must never crash the
  retrieval path — the deterministic agents and the human-review queue sit
  downstream and must keep working offline.
"""

from __future__ import annotations

import logging
import re
import time

from rag.llm_client import llm_chat

logger = logging.getLogger(__name__)

HYDE_MAX_TOKENS = 300
STEPBACK_MAX_TOKENS = 200
MULTI_QUERY_MAX_TOKENS = 400

_PERSIAN_CHARS = re.compile(r"[\u0600-\u06FF]")


def detect_language(text: str) -> str:
    """'fa' if the text contains Persian/Arabic-script characters, else 'en'."""
    return "fa" if _PERSIAN_CHARS.search(text or "") else "en"


def _resolve_language(query: str, language: str) -> str:
    return detect_language(query) if language == "auto" else language


# --- LLM call accounting (auditable cost per eval run) ---------------------

_llm_call_count: int = 0
_llm_total_seconds: float = 0.0


def reset_llm_counters() -> None:
    global _llm_call_count, _llm_total_seconds
    _llm_call_count = 0
    _llm_total_seconds = 0.0


def llm_counters() -> dict:
    return {
        "llm_calls": _llm_call_count,
        "llm_total_seconds": round(_llm_total_seconds, 3),
    }


def _record_call(elapsed: float) -> None:
    global _llm_call_count, _llm_total_seconds
    _llm_call_count += 1
    _llm_total_seconds += elapsed


def _complete(system: str, user: str, max_tokens: int) -> str:
    """One counted, non-raising completion via llm_chat. Returns "" on any failure.

    Failed attempts are counted too: an attempted call is billed latency
    (and possibly tokens), so cost auditing counts attempts.
    The provider client handles transient 429/5xx internally (Groq: key
    rotation; AgentRouter: SDK backoff); only hard errors (auth, network,
    model error) propagate here and are caught.
    """
    start = time.monotonic()
    try:
        text = llm_chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_completion_tokens=max_tokens,
        )
        _record_call(time.monotonic() - start)
        return text  # provider clients already strip whitespace
    except Exception as exc:  # noqa: BLE001 — must never crash retrieval
        _record_call(time.monotonic() - start)
        logger.warning("LLM transform call failed (%s)", exc)
        return ""


# ===========================================================================
# HyDE (Step 2)
# ===========================================================================

HYDE_SYSTEM_FA = (
    "شما یک متن فرضی به سبک مقررات ملی ساختمان ایران (مباحث مقررات ملی ساختمان) "
    "می‌نویسید. در پاسخ به پرسش کاربر، یک بند مقرراتی فرضی بنویسید که می‌توانست "
    "پاسخ آن پرسش در متن مقررات باشد.\n"
    "الزامات:\n"
    "- فارسی رسمی و اداری به کار ببرید، با واژگان فنی مهندسی ساختمان "
    "(مانند: «الزامی است»، «نباید کمتر از ... باشد»، «حداقل»، «حداکثر»، «مطابق بند»).\n"
    "- ساختار جمله‌ها مشابه بندهای واقعی مقررات باشد.\n"
    "- لازم نیست مقادیر عددی درست باشند؛ فقط سبک و ساختار اهمیت دارد.\n"
    "- فقط همان بند فرضی را بنویسید. هیچ مقدمه، توضیح، سلب مسئولیت یا "
    "علامت‌گذاری اضافه ننویسید."
)

HYDE_SYSTEM_EN = (
    "You write a hypothetical passage in the style of the Iranian National "
    "Building Regulations (Mabhas). Given the user's question, write one "
    "hypothetical regulatory clause that could plausibly answer it.\n"
    "Requirements:\n"
    "- Use formal English in regulatory register, with building-code "
    "phrasing (e.g. \"shall not be less than\", \"it is required that\", "
    "\"a minimum of\", \"in accordance with Article\").\n"
    "- Mirror the sentence structure of real building-code clauses.\n"
    "- Numeric values need not be factually correct; only register and "
    "structure matter.\n"
    "- Output ONLY the hypothetical clause itself — no preamble, no "
    "explanation, no disclaimers, no markdown."
)


def hyde_transform(query: str, language: str = "auto") -> str:
    """Generate a hypothetical Mabhas-style answer paragraph for `query`.

    The hypothetical need not be factually correct — it only needs to be in
    the right register so its embedding lands near real clauses (HyDE,
    Gao et al. 2022).

    Returns the hypothetical text, or the ORIGINAL query unchanged if the
    API call fails or returns empty (logged as a warning, never raises).
    """
    lang = _resolve_language(query, language)
    system = HYDE_SYSTEM_FA if lang == "fa" else HYDE_SYSTEM_EN
    text = _complete(system, query, HYDE_MAX_TOKENS)
    if not text:
        logger.warning("hyde_transform: falling back to original query")
        return query
    return text


# ===========================================================================
# Step-back (Step 3) — Zheng et al. 2023
# ===========================================================================

STEPBACK_SYSTEM_FA = (
    "پرسش کاربر دربارهٔ مقررات ملی ساختمان ایران (مباحث مقررات ملی ساختمان) است.\n"
    "وظیفهٔ شما: یک پرسش کلی‌تر («گام به عقب») بنویسید که دربارهٔ اصل یا قاعدهٔ "
    "حاکم بر همان موضوع باشد، نه دربارهٔ مورد خاص مطرح‌شده.\n"
    "الزامات:\n"
    "- خروجی فقط یک پرسش واحد باشد، نه پاسخ، نه فهرست، نه چند پرسش.\n"
    "- پرسش کلی‌تر باید همان حوزهٔ مقرراتی را هدف بگیرد (مثلاً از یک مورد خاص "
    "دربارهٔ یک در، به قواعد کلی ارتباط فضاها برسد).\n"
    "- فارسی رسمی به کار ببرید.\n"
    "- هیچ مقدمه یا توضیحی ننویسید؛ فقط خود پرسش را بنویسید."
)

STEPBACK_SYSTEM_EN = (
    "The user's question is about the Iranian National Building Regulations "
    "(Mabhas).\n"
    "Your task: write ONE broader \"step-back\" question about the governing "
    "principle or general rule behind the user's specific question.\n"
    "Requirements:\n"
    "- Output exactly one single question — not an answer, not a list, not "
    "multiple questions.\n"
    "- The broader question must target the same regulatory domain (e.g. "
    "from a specific case about one door, step back to the general rules "
    "for spatial connections between room types).\n"
    "- Use formal English.\n"
    "- No preamble, no explanation — output only the question itself."
)


def stepback_transform(query: str, language: str = "auto") -> str:
    """Generate a BROADER question about the governing principle.

    Example:
      in:  "Can a bedroom door open directly into a bathroom?"
      out: "What are the general rules for spatial connections between
            habitable rooms and sanitary spaces in residential buildings?"

    Returns the broader question, or the ORIGINAL query on failure.
    """
    lang = _resolve_language(query, language)
    system = STEPBACK_SYSTEM_FA if lang == "fa" else STEPBACK_SYSTEM_EN
    text = _complete(system, query, STEPBACK_MAX_TOKENS)
    if not text:
        logger.warning("stepback_transform: falling back to original query")
        return query
    # Defensive: if the model returned several lines despite instructions,
    # keep only the first non-empty line (the single question we asked for).
    first_line = next(
        (ln.strip() for ln in text.splitlines() if ln.strip()), ""
    )
    return first_line or query


# ===========================================================================
# Multi-query (Step 3)
# ===========================================================================

MULTI_QUERY_SYSTEM_FA = (
    "پرسش کاربر دربارهٔ مقررات ملی ساختمان ایران (مباحث مقررات ملی ساختمان) است.\n"
    "وظیفهٔ شما: {n} بازنویسی متفاوت از همین پرسش بنویسید.\n"
    "الزامات:\n"
    "- معنا و هدف پرسش باید دقیقاً حفظ شود؛ فقط واژگان و ساختار جمله را "
    "متنوع کنید (مترادف‌ها، اصطلاحات فنی جایگزین، ترتیب متفاوت اجزای جمله).\n"
    "- هر بازنویسی در یک سطر جداگانه نوشته شود.\n"
    "- دقیقاً {n} سطر خروجی بدهید؛ بدون شماره‌گذاری، بدون علامت فهرست، "
    "بدون مقدمه و بدون توضیح."
)

MULTI_QUERY_SYSTEM_EN = (
    "The user's question is about the Iranian National Building Regulations "
    "(Mabhas).\n"
    "Your task: write {n} different reformulations of this same question.\n"
    "Requirements:\n"
    "- Preserve the meaning and intent exactly; vary only the vocabulary "
    "and sentence structure (synonyms, alternative technical terms, "
    "different word order).\n"
    "- Write each reformulation on its own separate line.\n"
    "- Output exactly {n} lines — no numbering, no bullet markers, no "
    "preamble, no explanation."
)


def multi_query_transform(query: str, n: int = 3, language: str = "auto") -> list:
    """Generate n query variants. Item 0 is ALWAYS the original query.

    Makes ONE LLM call requesting n-1 reformulations; the original is
    prepended in code so the guarantee does not depend on model behavior.
    On failure returns [query] (the original alone) so callers degrade to
    plain retrieval gracefully.
    """
    if n <= 1:
        return [query]

    lang = _resolve_language(query, language)
    wanted = n - 1
    template = MULTI_QUERY_SYSTEM_FA if lang == "fa" else MULTI_QUERY_SYSTEM_EN
    system = template.replace("{n}", str(wanted))
    text = _complete(system, query, MULTI_QUERY_MAX_TOKENS)
    if not text:
        logger.warning("multi_query_transform: falling back to original only")
        return [query]

    # Parse: one reformulation per line; strip stray numbering/bullets the
    # model might add despite instructions; dedupe against the original.
    seen = {query.strip()}
    variants: list = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-•*0123456789.) ").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            variants.append(cleaned)
        if len(variants) == wanted:
            break

    if not variants:
        logger.warning("multi_query_transform: no usable variants; original only")
        return [query]
    return [query] + variants