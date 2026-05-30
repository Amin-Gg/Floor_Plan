"""
mabhas_classify.py  —  OpenAI API / gpt-oss-120b edition
=========================================================
Reads a Mabhas Word file (.docx), splits it into small chunks, sends each
chunk to the OpenAI API using gpt-oss-120b, and writes a single combined
JSON output file.

gpt-oss-120b is a reasoning model: it generates chain-of-thought (CoT)
thinking text before the JSON output. The JSON extractor handles this by
searching for the JSON array anywhere in the full response, rather than
assuming the entire output is JSON.

Usage
-----
    pip install python-docx openai
    python mabhas_classify.py
"""

from __future__ import annotations

import json
import os
import re
import time
from docx import Document
from openai import OpenAI, APIError, APITimeoutError

# ── Configuration ─────────────────────────────────────────────────────────────
API_KEY     = "YOUR_OPENAI_API_KEY"          # starts with sk-
BASE_DIR    = r"C:\Users\Asus\Desktop\New folder (2)"
PROMPT_FILE = os.path.join(BASE_DIR, "AI_PROMPT.txt")
WORD_FILE   = os.path.join(BASE_DIR, "4.docx")
OUTPUT_FILE = os.path.join(BASE_DIR, "mabhas_clauses.json")

MODEL       = "gpt-oss-120b"   # OpenAI API model string

# Reasoning models consume extra tokens for CoT thinking before the JSON.
# 30,000 gives plenty of room for both the thinking trace and the JSON output.
MAX_TOKENS  = 30000

# 20 paragraphs ≈ 12–18 articles. Reduce to 12 if you see truncated output.
CHUNK_SIZE  = 6

MAX_RETRIES = 3
RETRY_DELAY = 5    # seconds between retries
# ─────────────────────────────────────────────────────────────────────────────

client = OpenAI(api_key=API_KEY)   # no base_url — uses OpenAI's default endpoint


# ── Document extraction ───────────────────────────────────────────────────────

def extract_paragraphs(file_path: str) -> list[dict]:
    """
    Extract non-empty paragraphs preserving Word heading styles, so the model
    can identify article number boundaries accurately.
    """
    doc = Document(file_path)
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        paragraphs.append({"style": para.style.name, "text": text})
    return paragraphs


def format_chunk_for_api(paras: list[dict]) -> str:
    """
    Format a paragraph chunk as plain text.
    Heading paragraphs are prefixed with [H] so the model identifies article
    boundaries correctly.
    """
    lines = []
    for p in paras:
        if "heading" in p["style"].lower():
            lines.append(f"[H] {p['text']}")
        else:
            lines.append(p["text"])
    return "\n".join(lines)


def make_chunks(paragraphs: list[dict], size: int) -> list[list[dict]]:
    return [paragraphs[i:i + size] for i in range(0, len(paragraphs), size)]


# ── Prompt ────────────────────────────────────────────────────────────────────

def read_prompt(file_path: str) -> str:
    with open(file_path, encoding="utf-8") as f:
        return f.read()


# ── JSON extraction (reasoning-model safe) ────────────────────────────────────

def extract_json_array(raw: str) -> list[dict]:
    """
    gpt-oss-120b is a reasoning model: its output contains chain-of-thought
    thinking text before the JSON array. This function finds the JSON array
    anywhere in the full response using regex, so CoT text before or after
    the array does not cause a parse failure.

    Returns a list of clause dicts, or [] on any parse failure.
    """
    # Find the outermost [...] block in the response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        print("  WARNING: no JSON array found in model output")
        print(f"  Raw output (first 300 chars): {raw[:300]}")
        return []

    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            parsed = [parsed]
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON parse error — {e}")
        print(f"  Extracted text (first 300 chars): {match.group(0)[:300]}")
        return []


# ── API call with retry ───────────────────────────────────────────────────────

def call_openai(system_prompt: str, user_text: str) -> list[dict]:
    """
    Send one chunk to gpt-oss-120b and return classified clause dicts.
    Retries up to MAX_RETRIES times on network or API errors.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_text},
                ],
                temperature=0.1,
                max_tokens=MAX_TOKENS,
                # "low" reasoning effort is sufficient for structured
                # classification — saves tokens and cost vs "medium" or "high"
                reasoning_effort="low",
            )
            raw = response.choices[0].message.content
            return extract_json_array(raw)

        except (APIError, APITimeoutError) as e:
            print(f"  Attempt {attempt}/{MAX_RETRIES}: API error — {e}")
        except Exception as e:
            print(f"  Attempt {attempt}/{MAX_RETRIES}: unexpected error — {e}")

        if attempt < MAX_RETRIES:
            print(f"  Retrying in {RETRY_DELAY}s…")
            time.sleep(RETRY_DELAY)

    print("  All retries exhausted. Chunk skipped.")
    return []


# ── Output helpers ────────────────────────────────────────────────────────────

def load_existing(output_file: str) -> tuple[list[dict], set[str]]:
    """Load prior progress so a restart never re-processes finished chunks."""
    if not os.path.exists(output_file):
        return [], set()
    with open(output_file, encoding="utf-8") as f:
        existing = json.load(f)
    seen = {str(c.get("article_id", "")) for c in existing if c.get("article_id")}
    print(f"Resuming: {len(existing)} clauses already saved, "
          f"{len(seen)} article_ids seen.")
    return existing, seen


def validate_clause(clause: dict, chunk_index: int) -> bool:
    required = ("mabhas_part", "article_id", "text_fa")
    missing = [f for f in required if not clause.get(f)]
    if missing:
        print(f"  WARNING chunk {chunk_index}: missing fields {missing} — "
              f"article_id={clause.get('article_id', '?')}")
        return False
    return True


def save(clauses: list[dict], output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(clauses, f, ensure_ascii=False, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    for path, label in [(PROMPT_FILE, "prompt file"), (WORD_FILE, "Word file")]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found at {path}")
            return

    system_prompt = read_prompt(PROMPT_FILE)
    print(f"Prompt loaded ({len(system_prompt):,} chars)")
    print(f"Model: {MODEL}  |  reasoning_effort: low  |  max_tokens: {MAX_TOKENS}")

    print(f"\nExtracting text from {WORD_FILE}…")
    paragraphs = extract_paragraphs(WORD_FILE)
    print(f"Extracted {len(paragraphs)} non-empty paragraphs")

    chunks = make_chunks(paragraphs, CHUNK_SIZE)
    print(f"Split into {len(chunks)} chunks of up to {CHUNK_SIZE} paragraphs each")

    all_clauses, seen_ids = load_existing(OUTPUT_FILE)

    for i, chunk in enumerate(chunks, start=1):
        user_text = format_chunk_for_api(chunk)
        print(f"\nChunk {i}/{len(chunks)} ({len(chunk)} paragraphs)…")

        results = call_openai(system_prompt, user_text)
        if not results:
            print(f"  No results for chunk {i} — continuing")
            continue

        new_count = 0
        for clause in results:
            if not validate_clause(clause, i):
                continue
            art_id = str(clause.get("article_id", ""))
            if art_id in seen_ids:
                print(f"  SKIP duplicate: {art_id}")
                continue
            seen_ids.add(art_id)
            all_clauses.append(clause)
            new_count += 1

        print(f"  +{new_count} new clauses (total: {len(all_clauses)})")
        save(all_clauses, OUTPUT_FILE)   # save after every chunk

        if i < len(chunks):
            time.sleep(0.5)

    print(f"\nDone. {len(all_clauses)} clauses saved to {OUTPUT_FILE}")

    ids = [c.get("article_id") for c in all_clauses]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        print(f"WARNING: duplicate article_ids: {dupes}")
    else:
        print("No duplicates — output is clean.")


if __name__ == "__main__":
    main()
