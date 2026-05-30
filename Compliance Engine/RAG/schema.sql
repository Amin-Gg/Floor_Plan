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
