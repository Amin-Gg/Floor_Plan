"""
services/contextualize.py
=========================
One-time preprocessing pass (Stage 1 / Step 5): generate a 1-2 sentence
Persian context for every non-skipped Mabhas clause and write the result
to data/mabhas_clauses_contextual.json (the input file is never modified).

The context situates each clause within the regulations — which building
element or occupancy it governs and what kind of requirement it expresses
(numeric limit, spatial rule, definition, or exception) — so that clauses
that are meaningless in isolation (exceptions, cross-references) become
self-contained at index time. See Anthropic, "Introducing Contextual
Retrieval" (2024).

Usage
-----
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m rag.contextualize \
        --input data/mabhas_clauses_normalized.json \
        --output data/mabhas_clauses_contextual.json
        [--prompt-lang fa|en] [--sleep 0.5] [--no-resume] [--limit N]

Robustness
----------
- strictly serial, `--sleep` seconds between calls (default 0.5)
- progress line every 25 processed clauses
- on API failure: one retry after 5 s, then skip and log the article_id
- resume by default: clauses already carrying context_fa in an existing
  output file are not re-requested (safe to re-run after an interruption)
- end-of-run summary: processed / failed / copied-without-call counts,
  token usage, and estimated cost at claude-sonnet-4-20250514 pricing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MODEL = "claude-sonnet-4-20250514"
TEMPERATURE = 0.0
MAX_TOKENS = 200

# Pricing (USD per token), claude-sonnet-4-20250514 public API list price.
PRICE_PER_INPUT_TOKEN = 3.00 / 1_000_000
PRICE_PER_OUTPUT_TOKEN = 15.00 / 1_000_000

RETRY_WAIT_SECONDS = 5.0
PROGRESS_EVERY = 25

# ---------------------------------------------------------------------------
# System prompts — verbatim, reviewed before running.
# The Persian prompt is the default (--prompt-lang fa); the English version
# is provided for review and for --prompt-lang en.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FA = """\
شما دستیار آماده‌سازی متن برای بازیابی اطلاعات هستید. برای بندی از مقررات ملی ساختمان ایران که به شما داده می‌شود، فقط یک تا دو جملهٔ کوتاه به زبان فارسی بنویسید که جایگاه آن بند را در مقررات روشن کند: این بند به کدام عنصر ساختمانی یا نوع تصرف مربوط است و چه نوع الزامی را بیان می‌کند (حد عددی، قاعدهٔ فضایی، تعریف، یا استثنا).

فقط همان یک تا دو جمله را بنویسید. بدون مقدمه، بدون عنوان، بدون توضیح اضافه، بدون عذرخواهی یا سلب مسئولیت، و بدون تکرار متن خود بند."""

SYSTEM_PROMPT_EN = """\
You are a text-preparation assistant for information retrieval. For the clause of the Iranian National Building Regulations (Mabhas) given to you, write only one to two short sentences in Persian that situate the clause within the regulations: which building element or occupancy type it governs, and what kind of requirement it expresses (a numeric limit, a spatial rule, a definition, or an exception).

Write only those one to two sentences. No preamble, no headings, no extra explanation, no disclaimers, and do not repeat the clause text itself."""

SYSTEM_PROMPTS = {"fa": SYSTEM_PROMPT_FA, "en": SYSTEM_PROMPT_EN}


def build_user_message(clause: Dict[str, Any]) -> str:
    """The per-clause user message: part, normalized heading, rule type,
    normalized body — exactly the fields the spec allows the model to see."""
    heading = clause.get("heading_fa_normalized") or clause.get("heading_fa") or "—"
    body = clause.get("text_fa_normalized") or clause.get("text_fa") or ""
    return (
        f"مبحث: {clause.get('mabhas_part')}\n"
        f"عنوان بند: {heading}\n"
        f"نوع قاعده: {clause.get('rule_type')}\n"
        f"متن بند:\n{body}"
    )


# ---------------------------------------------------------------------------
# Core loop — client and sleep function injected for unit testing.
# ---------------------------------------------------------------------------

def generate_context(client, clause: Dict[str, Any], prompt_lang: str = "fa"
                     ) -> Tuple[str, int, int]:
    """One API call. Returns (context_text, input_tokens, output_tokens)."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=SYSTEM_PROMPTS[prompt_lang],
        messages=[{"role": "user", "content": build_user_message(clause)}],
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    ).strip()
    if not text:
        raise ValueError("model returned empty context")
    usage = getattr(resp, "usage", None)
    return (
        text,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def process_clauses(
    clauses: List[Dict[str, Any]],
    client,
    prompt_lang: str = "fa",
    sleep_seconds: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
    existing_contexts: Optional[Dict[str, str]] = None,
    limit: Optional[int] = None,
    log: Callable[[str], None] = print,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Generate context_fa for every non-skipped clause.

    Returns (output_clauses, stats). Output preserves ALL input clauses
    (skipped ones are copied through unchanged) and never mutates the input.

    stats = {processed, resumed, failed_ids, copied_no_call,
             input_tokens, output_tokens, cost_usd}
    """
    existing_contexts = existing_contexts or {}
    out: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "processed": 0, "resumed": 0, "failed_ids": [],
        "copied_no_call": 0, "input_tokens": 0, "output_tokens": 0,
    }
    eligible_seen = 0

    for clause in clauses:
        clause = dict(clause)  # never mutate the caller's objects

        if clause.get("skip_category"):
            stats["copied_no_call"] += 1
            out.append(clause)
            continue

        aid = str(clause["article_id"])

        if aid in existing_contexts:
            clause["context_fa"] = existing_contexts[aid]
            stats["resumed"] += 1
            out.append(clause)
            continue

        if limit is not None and eligible_seen >= limit:
            out.append(clause)
            continue
        eligible_seen += 1

        context: Optional[str] = None
        for attempt in (1, 2):
            try:
                context, tok_in, tok_out = generate_context(
                    client, clause, prompt_lang
                )
                stats["input_tokens"] += tok_in
                stats["output_tokens"] += tok_out
                break
            except Exception as exc:  # noqa: BLE001 — any API error counts
                if attempt == 1:
                    log(f"  [retry] {aid}: {exc} — retrying in "
                        f"{RETRY_WAIT_SECONDS:.0f} s")
                    sleep_fn(RETRY_WAIT_SECONDS)
                else:
                    log(f"  [FAILED] {aid}: {exc} — skipped")
                    stats["failed_ids"].append(aid)

        if context is not None:
            clause["context_fa"] = context
            stats["processed"] += 1
            if stats["processed"] % PROGRESS_EVERY == 0:
                log(f"  progress: {stats['processed']} clauses contextualized")

        out.append(clause)
        sleep_fn(sleep_seconds)

    stats["cost_usd"] = round(
        stats["input_tokens"] * PRICE_PER_INPUT_TOKEN
        + stats["output_tokens"] * PRICE_PER_OUTPUT_TOKEN,
        4,
    )
    return out, stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate LLM context_fa per clause (Stage 1 / Step 5)."
    )
    parser.add_argument("--input", default="data/mabhas_clauses_normalized.json")
    parser.add_argument("--output", default="data/mabhas_clauses_contextual.json")
    parser.add_argument("--prompt-lang", choices=["fa", "en"], default="fa")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="Seconds between API calls (default 0.5)")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore context_fa already present in --output")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N eligible clauses (pilot runs)")
    args = parser.parse_args(argv)

    in_path, out_path = Path(args.input), Path(args.output)
    if in_path.resolve() == out_path.resolve():
        raise SystemExit("--output must differ from --input "
                         "(the normalized file is never overwritten).")

    with open(in_path, encoding="utf-8") as f:
        clauses = json.load(f)
    n_eligible = sum(1 for c in clauses if not c.get("skip_category"))
    print(f"Loaded {len(clauses)} clauses ({n_eligible} eligible for context).")

    existing: Dict[str, str] = {}
    if out_path.exists() and not args.no_resume:
        with open(out_path, encoding="utf-8") as f:
            for c in json.load(f):
                if c.get("context_fa"):
                    existing[str(c["article_id"])] = c["context_fa"]
        if existing:
            print(f"Resuming: {len(existing)} clauses already have context_fa.")

    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    t0 = time.time()
    out, stats = process_clauses(
        clauses, client,
        prompt_lang=args.prompt_lang,
        sleep_seconds=args.sleep,
        existing_contexts=existing,
        limit=args.limit,
    )
    elapsed = time.time() - t0

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n──────── summary ────────")
    print(f"contextualized this run : {stats['processed']}")
    print(f"resumed from output     : {stats['resumed']}")
    print(f"API failures (skipped)  : {len(stats['failed_ids'])}"
          + (f"  -> {stats['failed_ids']}" if stats["failed_ids"] else ""))
    print(f"copied without call     : {stats['copied_no_call']} (skip_category set)")
    print(f"tokens in/out           : {stats['input_tokens']} / {stats['output_tokens']}")
    print(f"estimated cost          : ${stats['cost_usd']:.4f} "
          f"({MODEL}: $3/M in, $15/M out)")
    print(f"elapsed                 : {elapsed/60:.1f} min")
    print(f"written -> {out_path}")
    if stats["failed_ids"]:
        print("\nRe-run the same command to retry ONLY the failed clauses "
              "(resume skips completed ones).")
        sys.exit(1)


if __name__ == "__main__":
    main()
