# Stage 0 → Stage 1 integration notes

This zip is the original `Compliance Engine` codebase **plus** the two artifacts produced
by Stage 0 of the thesis pipeline. It is the input you (or Claude.ai) start from for
Stage 1.

## What's new in this zip vs. the original

Two new files in `data/`, nothing else:

| Path | What it is | Read by |
|---|---|---|
| `data/mabhas_clauses_normalized.json` | The 594 clauses with Persian-normalized fields added | Stage 1 Step 2 ingestion |
| `data/mabhas_retrieval_eval.json` | 43-item retrieval evaluation set | Stage 1 Step 1 harness onward |
| `data/STAGE0_NOTES.md` | Provenance notes (commit context preserved here) | humans |

No existing files were edited or deleted. The original `data/mabhas_clauses.json` stays
in place — the classification pipeline, API, and notebook still read it unchanged.

## File-path note for Stage 1 / 2 / 3 prompts

When I wrote the Stage 1 / 2 / 3 pipeline and chat-prompt documents earlier in our
conversation, I referred to the evaluation set as `data/mabhas_eval_set.json`. The actual
filename is **`data/mabhas_retrieval_eval.json`**.

Before pasting any of those prompts into Claude.ai, do a find-and-replace in the prompt:

```
mabhas_eval_set.json   →   mabhas_retrieval_eval.json
```

Everything else in those prompts is accurate against this zip.

## What Stage 1 will modify

Stage 1's seven steps will add new files and edit some existing ones. None of those
changes overlap with the Stage 0 additions above. After Stage 1, the layout looks like:

```
Compliance Engine/
├── rag/
│   ├── embeddings.py            (Step 6 may edit: EMBED_MODEL env var)
│   ├── rag_index.py             (Step 2 edits: use text_fa_normalized + Step 5 context_fa)
│   ├── rag_retriever.py         (Step 3, 4, 7 add: hybrid_retrieve, rerank, factory)
│   ├── reranker.py              (Step 4 adds — new file)
│   ├── schema.sql               (Step 3 edits: add lexeme column + GIN index)
│   └── ...
├── data/
│   ├── mabhas_clauses.json                (untouched)
│   ├── mabhas_clauses_normalized.json     ← from Stage 0
│   ├── mabhas_clauses_contextual.json     (Step 5 produces this)
│   ├── mabhas_retrieval_eval.json         ← from Stage 0
│   └── sample_mabhas_clauses.json         (untouched)
├── eval/                                  (new folder in Step 1)
│   ├── retrieval_eval.py
│   ├── metrics.py
│   └── results/
│       ├── stage1_step0_baseline.json
│       ├── ...
│       └── SUMMARY.md
└── (everything else untouched)
```

## Sanity check before Stage 1

Run this quick check after unzipping to confirm everything is in place:

```bash
cd "Compliance Engine"
python3 -c "
import json
norm = json.load(open('data/mabhas_clauses_normalized.json', encoding='utf-8'))
ev   = json.load(open('data/mabhas_retrieval_eval.json',   encoding='utf-8'))
print(f'normalized clauses: {len(norm)} (expect 594)')
print(f'evaluation items:   {len(ev)} (expect 43)')
assert all('text_fa_normalized' in c for c in norm), 'normalization fields missing'
print('OK — ready for Stage 1.')
"
```

If you see `OK — ready for Stage 1.` you're good to go. Start by pasting the Stage 1
pipeline doc into Claude.ai (or use Claude Code with the pipeline as a single agent prompt).

## Provenance

- Original codebase: `Compliance_Engine.zip` (unchanged in this output)
- Stage 0 outputs: extracted from `Stage_0.zip` → `Stage_0/S0_P1/` and `Stage_0/S0_P2/`
- Local validation scripts (`validate_and_normalize.py` × 2) are intentionally not
  bundled — they have Windows-hardcoded paths and are not part of the build pipeline.
  Keep them in your local Stage 0 folder for re-validation if needed.

See `data/STAGE0_NOTES.md` for the full normalization-rule list, eval-set schema, and
the original commit messages from your two Stage 0 commits.
