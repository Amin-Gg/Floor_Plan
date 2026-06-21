-- ============================================================================
-- db/schema.sql  —  v2.0 (scope-aware, forward-compatible)
-- ----------------------------------------------------------------------------
-- Schema for the Mabhas regulation RAG store.
-- Run once:  psql "$DATABASE_URL" -f db/schema.sql
--
-- v2.0 adds:
--   applicable_occupancies   JSONB  — list of occupancy group codes
--   applicable_height_groups JSONB  — list of height group codes
-- These are stored so future scope expansions only need a re-ingest with
-- --scope, not a re-classification with DeepSeek.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS mabhas_clauses (
    id                       SERIAL PRIMARY KEY,
    mabhas_part              TEXT          NOT NULL,
    article_id               TEXT          NOT NULL,
    heading_fa               TEXT,
    text_fa                  TEXT          NOT NULL,
    text_en                  TEXT,
    rule_type                TEXT,           -- numeric|spatial|definition|exception
    entities                 JSONB,
    applicable_occupancies   JSONB,          -- e.g. ["M-4","all_residential"]
    applicable_height_groups JSONB,          -- e.g. ["any"] or ["low_rise"]
    embedding                vector(1024)  NOT NULL,
    created_at               TIMESTAMPTZ   DEFAULT now(),

    CONSTRAINT uq_part_article UNIQUE (mabhas_part, article_id)
);

-- ANN index for fast cosine search
CREATE INDEX IF NOT EXISTS idx_mabhas_embedding
    ON mabhas_clauses
    USING hnsw (embedding vector_cosine_ops);

-- Metadata indexes for agent filters
CREATE INDEX IF NOT EXISTS idx_mabhas_rule_type  ON mabhas_clauses (rule_type);
CREATE INDEX IF NOT EXISTS idx_mabhas_part       ON mabhas_clauses (mabhas_part);
CREATE INDEX IF NOT EXISTS idx_mabhas_occ        ON mabhas_clauses USING gin (applicable_occupancies);

-- ============================================================================
-- Stage 1 / Step 3 — lexical search support
-- ----------------------------------------------------------------------------
-- Generated tsvector over heading_fa + text_fa + text_en using the 'simple'
-- configuration: PostgreSQL has no Persian stemmer, and exact token matching
-- is the correct behaviour for Mabhas article identifiers, technical terms
-- and numbers. STORED generated columns are computed for existing rows when
-- the ALTER TABLE is applied — no re-ingest required.
-- ============================================================================

ALTER TABLE mabhas_clauses
  ADD COLUMN IF NOT EXISTS lexeme tsvector
  GENERATED ALWAYS AS (
    to_tsvector('simple',
      coalesce(heading_fa,'') || ' ' || coalesce(text_fa,'') || ' ' || coalesce(text_en,''))
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_mabhas_lexeme ON mabhas_clauses USING gin (lexeme);

-- ============================================================================
-- Stage 1 / Step 4 — stored passage for cross-encoder reranking
-- ----------------------------------------------------------------------------
-- The exact text that was embedded for each clause (built by rag_index's
-- _build_passage_text: normalized heading + normalized body + text_en, and
-- from Step 5 on, context_fa as well). The reranker re-scores candidates
-- against this stored passage so it sees precisely what was indexed.
-- Nullable: rows ingested before Step 4 fall back to raw clause fields.
-- ============================================================================

ALTER TABLE mabhas_clauses
  ADD COLUMN IF NOT EXISTS passage TEXT;

-- ============================================================================
-- Stage 1 / Step 5 — contextual retrieval
-- ----------------------------------------------------------------------------
-- context_fa stores the LLM-generated situating context per clause (analysis
-- and debugging; the authoritative text_fa stays untouched).
--
-- The lexeme column is REGENERATED over the stored `passage` column, which
-- ingestion builds as: context_fa + heading_fa_normalized +
-- text_fa_normalized + text_en. This supersedes the Step 3 lexeme definition
-- (raw fields): both the dense embedding and the lexical tsvector now index
-- the identical contextual passage. Generated-column expressions cannot be
-- altered in place, so the migration is DROP + ADD (recompute is trivial at
-- 328 rows). Rows ingested before Step 4 have passage = NULL and fall back
-- to the raw-field concatenation.
-- ============================================================================

ALTER TABLE mabhas_clauses
  ADD COLUMN IF NOT EXISTS context_fa TEXT;

ALTER TABLE mabhas_clauses DROP COLUMN IF EXISTS lexeme;
ALTER TABLE mabhas_clauses
  ADD COLUMN lexeme tsvector
  GENERATED ALWAYS AS (
    to_tsvector('simple',
      coalesce(passage,
        coalesce(heading_fa,'') || ' ' || coalesce(text_fa,'') || ' ' ||
        coalesce(text_en,'')))
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_mabhas_lexeme ON mabhas_clauses USING gin (lexeme);
