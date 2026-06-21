# Stage 0 — provenance notes

This file documents the two files added to `data/` during Stage 0. It exists so anyone
reading the repo (you in six months, your committee, a reviewer) can see what these files
are without grepping the codebase.

---

## `mabhas_clauses_normalized.json`

**Status:** additive — does not replace `mabhas_clauses.json`. Every original field is
preserved byte-for-byte; two new fields are added per clause.

**Added fields:**
- `heading_fa_normalized` — `heading_fa` after the 6-rule normalization pipeline below
- `text_fa_normalized` — `text_fa` after the same pipeline

**Counts:** 594 clauses, of which 328 are non-skipped (have `rule_type` set,
`skip_category` not set). Same 594 article_ids as `mabhas_clauses.json`.

### Normalization rules applied (in order)

1. **Arabic-to-Persian character substitution:**
   ك→ک, ي→ی, ة→ه, ؤ→و, ئ→ی, ى→ی
2. **Alef normalization:** أ/إ→ا (آ preserved as-is)
3. **Arabic-Indic digit normalization:** ٠–٩→۰–۹ (no-op on this corpus — none present)
4. **ZWNJ / half-space correction:** verbal prefixes می/نمی attached to following verb
   root via U+200C (128 attachments); stray ZWNJ adjacent to spaces or punctuation
   removed; duplicate ZWNJ collapsed
5. **Whitespace cleanup:** double spaces collapsed; spaces before .،؛:؟! removed; one
   space ensured after sentence-ending .؟!؛; leading/trailing whitespace stripped;
   embedded newlines preserved
6. **ASCII → Arabic comma** inside Persian text (40 conversions; numeric and Latin-context
   commas protected)

### Counts of interest after normalization
- 485 `null` `heading_fa` values yield `heading_fa_normalized: null` (preserved)
- All 67 embedded newlines preserved
- 511 آ occurrences intact
- 402 ZWNJ in normalized output (327 pre-existing + 128 added − strays removed)

---

## `mabhas_retrieval_eval.json`

**Status:** new file. No predecessor.

**Schema:** each item has the following keys:
```
item_id, source_article_ids (list[str]), hop_type ("zero_hop" | "multi_hop"),
question_en, question_fa, gold_answer_en, gold_answer_fa,
rule_type ("numeric" | "spatial" | "definition" | "exception"),
expected_verdict ("FAIL" | "PASS" | "NEEDS_REVIEW" | "N/A"),
difficulty_notes
```

**Counts (43 items total):**
- `hop_type`: 32 zero_hop, 11 multi_hop
- `rule_type`: 28 numeric, 6 spatial, 3 definition, 6 exception
- `expected_verdict`: 24 FAIL, 7 PASS, 9 NEEDS_REVIEW, 3 N/A

**Validation already performed:**
- Every `source_article_id` resolves to a clause in `mabhas_clauses_normalized.json`
- No item references a clause with `skip_category` set
- All 6 exception clauses in the corpus are represented as `multi_hop` items

### Authoring note (preserve when citing this dataset)
Numeric answers were derived from `text_fa` rather than `text_en` or `entities`,
because the latter two contain mis-normalized momayyez ("/") values
(e.g. "0.90 m" → "90 m", "2.05 m" → "0.25 m"). The known cases are flagged in
`difficulty_notes` per item.

**Special case:** the basement exception (article `2-5-1-4-4`) was paired with the nearest
in-corpus base rule (`4-2-5-4`) because its true cross-reference (`4-4-1-5-1`) is absent
from the corpus.

---

## Local validation scripts (not in this zip)

Two `validate_and_normalize.py` scripts were used locally on Windows to verify the two
output files. They have hard-coded Windows paths and are not part of the build pipeline,
so they are intentionally excluded from the repo. Keep your local copies if you want to
re-run them after any future re-normalization.
