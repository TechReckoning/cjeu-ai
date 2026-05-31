-- 0002_citation_tables.sql
-- Citation-graph storage. THREE side tables; the 10 GB hot table cjeu_paragraphs
-- is NOT modified. Safe to run in the Supabase SQL Editor: plain DDL, instant
-- (the tables start empty). Idempotent (IF NOT EXISTS).

-- 1) Raw, append-only: one row per detected citation occurrence. Text-parsed
--    first; CELLAR metadata can feed the same table later (source='cellar').
--    cited_celex is NULL when a citation could not be resolved to a CELEX in the
--    corpus (e.g. name-only references, orders, or cases not ingested).
CREATE TABLE IF NOT EXISTS citation_mentions (
    id                       bigserial PRIMARY KEY,
    citing_celex             text NOT NULL,
    citing_paragraph_number  integer,
    cited_celex              text,           -- NULL = unresolved
    cited_paragraph_number   integer,        -- pinpoint into the cited decision, if given
    relation_type            text,           -- following | by_analogy | distinguishing | see | cites
    signal_phrase            text,           -- matched cue, for audit
    raw_reference            text,           -- raw cited string, for audit
    source                   text NOT NULL DEFAULT 'text',   -- text | cellar
    confidence               real,
    created_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS citation_mentions_citing_idx ON citation_mentions (citing_celex);
CREATE INDEX IF NOT EXISTS citation_mentions_cited_idx  ON citation_mentions (cited_celex);

-- 2) Deduplicated decision -> decision edges (what networkx consumes). Rebuilt
--    from citation_mentions; resolved edges only (cited_celex NOT NULL).
CREATE TABLE IF NOT EXISTS citation_edges (
    citing_celex            text NOT NULL,
    cited_celex             text NOT NULL,
    mention_count           integer NOT NULL DEFAULT 1,
    dominant_relation_type  text,
    from_text               boolean NOT NULL DEFAULT false,
    from_cellar             boolean NOT NULL DEFAULT false,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (citing_celex, cited_celex)
);
CREATE INDEX IF NOT EXISTS citation_edges_cited_idx ON citation_edges (cited_celex);

-- 3) Per-decision graph metrics, computed offline with networkx. Joined at query
--    time; never widens cjeu_paragraphs.
CREATE TABLE IF NOT EXISTS decision_metrics (
    celex         text PRIMARY KEY,
    in_degree     integer,
    out_degree    integer,
    pagerank      double precision,
    authority     double precision,
    hub           double precision,
    community_id  integer,
    computed_at   timestamptz NOT NULL DEFAULT now()
);
