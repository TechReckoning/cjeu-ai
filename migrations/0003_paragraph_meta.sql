-- 0003_paragraph_meta.sql
-- Per-paragraph document classification, in a SIDE table keyed on the paragraph
-- id (never widens the 10 GB cjeu_paragraphs hot table). Populated by
-- classify_paragraphs.py from the CELEX suffix.
--
-- doc_kind:
--   'judgment'    — a real judgment/order paragraph (base CELEX, no suffix)
--   'summary'     — official Summary / headnote / catchwords (CELEX _SUM)
--   'resolution'  — operative/resolution extract (CELEX _RES)
--   'info'        — information / abstract notice (CELEX _INF)
-- Retrieval down-ranks non-'judgment' rows so judgment text wins, while keeping
-- summaries retrievable (some have no full judgment in the corpus).
--
-- Safe to run in the Supabase SQL Editor: plain DDL, table starts empty.

CREATE TABLE IF NOT EXISTS paragraph_meta (
    id          text PRIMARY KEY REFERENCES cjeu_paragraphs(id) ON DELETE CASCADE,
    base_celex  text NOT NULL,   -- CELEX with any _SUM/_RES/_INF suffix stripped
    doc_kind    text NOT NULL    -- judgment | summary | resolution | info
);
CREATE INDEX IF NOT EXISTS paragraph_meta_kind_idx ON paragraph_meta (doc_kind);
CREATE INDEX IF NOT EXISTS paragraph_meta_base_celex_idx ON paragraph_meta (base_celex);
