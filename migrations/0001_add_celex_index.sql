-- 0001_add_celex_index.sql
-- Adds a btree index on cjeu_paragraphs(celex).
--
-- WHY: every per-case operation ("all paragraphs of case X") and the upcoming
-- citation graph (which is keyed on CELEX) currently sequential-scans ~608k rows.
-- A btree on celex turns those into index lookups.
--
-- SAFETY / HOW TO RUN  (production Supabase, ~10 GB table — treat as production):
--   * Uses CREATE INDEX CONCURRENTLY, so it does NOT block reads or writes while
--     building. Build may take a few minutes.
--   * CONCURRENTLY cannot run inside a transaction block. Run this statement on
--     its own. The Supabase SQL editor runs single statements without an explicit
--     transaction, so it is fine there; if you hit a "cannot run inside a
--     transaction block" error, run it via psql instead:
--         psql "$DATABASE_URL" -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS \
--           cjeu_celex_idx ON public.cjeu_paragraphs USING btree (celex);"
--   * Safe to re-run (IF NOT EXISTS). If a prior attempt was interrupted it can
--     leave an INVALID index — drop it first, then re-run:
--         DROP INDEX IF EXISTS public.cjeu_celex_idx;

CREATE INDEX CONCURRENTLY IF NOT EXISTS cjeu_celex_idx
    ON public.cjeu_paragraphs USING btree (celex);

-- Refresh planner statistics now that the index exists.
ANALYZE public.cjeu_paragraphs;
