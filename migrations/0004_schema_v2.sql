-- 0004_schema_v2.sql
-- Corpus rebuild schema (v2). Builds ALONGSIDE the live tables; nothing existing
-- is touched. See docs/schema_v2_design.md. Safe to run in the Supabase SQL
-- Editor: plain DDL, tables start empty. Reversible via DROP TABLE.
--
-- Requires the vector extension (already installed: vector 0.8.0).

-- One row per CJEU decision (judgments only for the rebuild).
CREATE TABLE IF NOT EXISTS decisions_v2 (
    celex                  text PRIMARY KEY,
    ecli                   text,
    cellar_uri             text,
    case_number            text,
    case_year              int,
    decision_date          date,
    doc_type               text,            -- JUDG (rebuild is judgments-only)
    procedure_type         text,            -- resolved EN label
    court_formation        text,            -- resolved EN label
    subject_matters        text[],          -- resolved EN labels
    advocate_general       text,            -- resolved name
    judge_rapporteur       text,            -- resolved name
    country_origin         text,            -- resolved EN label (preliminary refs)
    procedure_language     text,
    title                  text,            -- full EUR-Lex case title
    parties                text[],          -- parsed from title
    cites                  text[],          -- CELEXes cited (cdm:work_cites_work)
    interprets_legislation text[],          -- CELEXes of legislation interpreted
    source_fetch_status    text,            -- ok | placeholder | fallback_used | error
    ingested_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decisions_v2_date_idx       ON decisions_v2 (decision_date);
CREATE INDEX IF NOT EXISTS decisions_v2_year_idx       ON decisions_v2 (case_year);
CREATE INDEX IF NOT EXISTS decisions_v2_formation_idx  ON decisions_v2 (court_formation);
CREATE INDEX IF NOT EXISTS decisions_v2_procedure_idx  ON decisions_v2 (procedure_type);
CREATE INDEX IF NOT EXISTS decisions_v2_country_idx    ON decisions_v2 (country_origin);
CREATE INDEX IF NOT EXISTS decisions_v2_subjects_idx   ON decisions_v2 USING gin (subject_matters);
CREATE INDEX IF NOT EXISTS decisions_v2_cites_idx      ON decisions_v2 USING gin (cites);

-- One row per paragraph, with RELIABLE within-section numbering + section tag.
CREATE TABLE IF NOT EXISTS paragraphs_v2 (
    id                text PRIMARY KEY,                      -- {celex}_{section}_{number}
    celex             text NOT NULL REFERENCES decisions_v2(celex) ON DELETE CASCADE,
    paragraph_number  int,                                   -- from markup, within section
    section           text NOT NULL,                         -- summary | grounds | operative
    seq               int NOT NULL,                          -- global order in the decision
    text              text NOT NULL,
    embedding         vector(1536),
    search_vector     tsvector
);
CREATE INDEX IF NOT EXISTS paragraphs_v2_celex_idx    ON paragraphs_v2 (celex);
CREATE INDEX IF NOT EXISTS paragraphs_v2_section_idx  ON paragraphs_v2 (section);
CREATE INDEX IF NOT EXISTS paragraphs_v2_embed_idx    ON paragraphs_v2 USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS paragraphs_v2_search_idx   ON paragraphs_v2 USING gin (search_vector);
