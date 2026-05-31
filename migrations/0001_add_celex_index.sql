-- 0001_add_celex_index.sql
-- Adds a btree index on cjeu_paragraphs(celex).
--
-- WHY: per-case lookups and the upcoming citation graph (keyed on CELEX) currently
-- sequential-scan ~608k rows. A btree on celex turns those into index lookups.
-- An index never changes query RESULTS, only their speed.
--
-- ===========================================================================
-- OPTION A — Supabase SQL Editor (recommended; simplest)
--   The SQL Editor runs statements inside a transaction, so CREATE INDEX
--   CONCURRENTLY is NOT allowed there (fails with "25001: ... cannot run inside
--   a transaction block"). Use the plain CREATE INDEX below — it is the two
--   statements at the bottom of this file; just run this file as-is.
--
--   A plain CREATE INDEX takes a SHARE lock: it briefly blocks WRITES to
--   cjeu_paragraphs (INSERT/UPDATE/DELETE) but NOT reads, so the app's searches
--   keep working. Building a btree on one short text column is fast (seconds to a
--   minute; the 10 GB is embeddings, which this index does not touch). Run it
--   when no ingestion job is writing and it is effectively invisible to users.
--
-- ===========================================================================
-- OPTION B — psql (zero write-lock)
--   To avoid even the brief write-lock, run CONCURRENTLY from psql, which does
--   not force a transaction (cannot be used in the SQL Editor):
--
--     psql "$DATABASE_URL" -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS \
--       cjeu_celex_idx ON public.cjeu_paragraphs USING btree (celex);"
--     psql "$DATABASE_URL" -c "ANALYZE public.cjeu_paragraphs;"
--
--   Safe to re-run (IF NOT EXISTS). If a CONCURRENTLY build is interrupted it can
--   leave an INVALID index — drop and retry:
--     DROP INDEX IF EXISTS public.cjeu_celex_idx;
-- ===========================================================================

-- Default (Option A) — runs in the Supabase SQL Editor:
CREATE INDEX IF NOT EXISTS cjeu_celex_idx
    ON public.cjeu_paragraphs USING btree (celex);

ANALYZE public.cjeu_paragraphs;
