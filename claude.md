# CLAUDE.md — Amicus (cjeu-ai)

Context for Claude Code working in this repository. Read this fully before making changes.

## What this project is
Amicus is a hybrid (semantic + full-text) search engine with RAG over **Court of
Justice of the European Union (CJEU) case law**, indexed at the **paragraph** level.
The objective is to be the best, most reliable CJEU case-law search engine.

- **Frontend / app:** Streamlit (`app.py`).
- **LLM/embeddings:** OpenAI (Responses API + embeddings).
- **Database:** Supabase Postgres with `pgvector` (HNSW) and Postgres full-text search.
- **Ingestion source of truth:** CELLAR SPARQL API (auto-updating with new case law).

## CRITICAL: which files are live
**`app.py` is the entire running application.** It is self-contained and imports
none of the `ask_cjeu*.py` or `query_*.py` files. Those (`ask_cjeu.py`,
`ask_cjeu_paragraph.py`, `ask_cjeu_pgvector.py`, `ask_cjeu_pgvector_rerank.py`,
`ask_cjeu_hybrid_rerank.py`, `query_ai.py`, `query_hybrid.py`, `query_pgvector.py`)
are **dead experiments**. Do not "fix" or wire them in. They can be deleted in a
dedicated cleanup commit, but never as a side effect of another task.

The repo is mid-migration from ChromaDB to pgvector. pgvector is the target;
ChromaDB code is legacy.

## Ingestion architecture
`incremental_index_pgvector.py` embeds new paragraphs from the `cjeu-py` JSONL
output and writes them **straight to Supabase** (production), using the same
`DATABASE_URL` / `SUPABASE_*` env vars as `app.py`. There is one target DB — the
old local `cjeu_ai` staging step is gone. Run with `--dry-run` to preview counts
without writing. The shell scripts (`daily_update*.sh`, `*backfill*.sh`) pull from
CELLAR via `cjeu-py`, then call that indexer.

Schema and indexes are NOT touched by the indexer — they are managed via
`migrations/`; new rows are indexed automatically by the existing HNSW/GIN indexes
on INSERT.

Still local-only / legacy (target a local `cjeu_ai` Postgres; NOT production):
`migrate_to_pgvector.py` (one-off ChromaDB -> pgvector migration), `coverage_report.py`,
`build_index.py`, `build_paragraph_index.py`. The former local -> Supabase push
script `incremental_index_supabase.py` is obsolete and no longer referenced.

## Database schema (Supabase, public schema)
**`cjeu_paragraphs`** (~608k paragraph rows across ~13,954 decisions, ~10 GB; embedding-dominated):
`id` (text, PK) · `celex` (text) · `url` (text) · `language` (text) ·
`paragraph_number` (int) · `paragraph_index` (int) · `text` (text) ·
`embedding` (vector, 1536-dim, text-embedding-3-small) · `search_vector` (tsvector).
Indexes: HNSW `cjeu_embedding_idx (embedding vector_cosine_ops)`,
GIN `cjeu_search_idx (search_vector)`, btree `cjeu_celex_idx (celex)` (applied via
`migrations/0001`). A `language` index is intentionally NOT needed (single-language
corpus; see below).

**`amicus_queries`** (analytics + feedback): `id` · `created_at` · `user_question`
· `retrieval_question` · `response_time_seconds` · `input/output/total_tokens` ·
`candidate_count` · `source_count` · `feedback` (1 / -1) · `retrieval_success` ·
`answer_length` · `error_message`. Currently tiny (~tens of rows) — too little
data for any learning-to-rank/fine-tuning yet. Priority is clean instrumentation
and accumulating honest data (including failures).

**Citation-graph tables** (populated; side tables — never widen `cjeu_paragraphs`):
- **`citation_mentions`** (~121k) — one row per text-parsed citation occurrence:
  `citing_celex` · `citing_paragraph_number` · `cited_celex` (NULL if unresolved) ·
  `cited_paragraph_number` · `relation_type` (cites/see/following/by_analogy/distinguishing)
  · `signal_phrase` · `raw_reference` · `source` ('text'|'cellar') · `confidence`.
- **`citation_edges`** (~57k) — deduplicated decision→decision edges (networkx input):
  `citing_celex` · `cited_celex` · `mention_count` (text occurrences) ·
  `dominant_relation_type` · `from_text` · `from_cellar`. Rebuilt from
  `citation_mentions` aggregating BOTH sources (`rebuild_citation_edges`).
- **`decision_metrics`** (~11.1k) — offline networkx scores per decision:
  `in_degree` · `out_degree` · `pagerank` · `authority` · `hub` · `community_id`.
  Joined at query time for the ranking tie-breaker.

## Pipeline (in app.py)
1. Rewrite latest message into a standalone question (gpt-4.1-mini).
2. Embed (text-embedding-3-small).
3. Hybrid retrieval: vector query + keyword query, fused with **Reciprocal Rank
   Fusion (RRF, k=60)** — NOT raw-score weighting.
4. LLM rerank top ~50 (gpt-4.1-mini), with fallback to RRF order on failure.
5. Tie-break equal rerank scores by citation authority (`decision_metrics.pagerank`):
   legal relevance always wins, `hybrid_score` is the final fallback.
6. Answer (gpt-4.1) using ONLY retrieved sources, with CELEX/paragraph citations.
7. Log to `amicus_queries` (successes AND failures); 👍/👎 feedback.

## Changes already applied (do not regress)
- RRF fusion replaced incommensurable score-weighting (cosine vs ts_rank_cd).
- `websearch_to_tsquery` replaced `plainto_tsquery` (the latter AND-ed every term
  and usually returned 0 keyword rows).
- Connection pooling via `psycopg_pool` cached with `@st.cache_resource`, with
  graceful fallback to per-call connections. (`psycopg-pool` in requirements.)
- Reranker JSON failure now falls back to RRF order instead of `st.stop()`.
- Analytics now write `response_time_seconds` + `error_message`, set real
  `retrieval_success`, and log failed queries too.
- Live (cached) corpus stats instead of hardcoded counts.
- Answer model upgraded mini -> gpt-4.1.
- Config constants centralised at top of app.py (models, FTS_CONFIG, K_RRF, etc.).

## RESOLVED — full-text search language
The corpus is **English-only by design**. A `GROUP BY language` over
`cjeu_paragraphs` returns a single row: `eng` (607,999 paragraphs / 13,954
decisions, as of 2026-05-31). `FTS_CONFIG` stays `"english"` — this is settled,
not provisional. Do NOT add per-language routing or a `language` index (the column
is single-valued). The maintainer does not want non-English case law ingested.

## Citation graph (Phase 1 + 3 LIVE; Phase 2 next)
Goal: surface doctrinal evolution and improve answer reliability. Design (followed):
edges in separate tables (above), two sources (CELLAR metadata + text-parsed),
typed/signed edges from the Court's stereotyped phrasing, networkx metrics offline.

DONE:
- **Phase 1** — `extract_citations.py` parses both OLD-style ("Case C-57/94 …
  [1995] ECR …") and MODERN ECLI-era citations ("Party, C‑202/97, EU:C:2000:75,
  paragraph 51"; handles non-breaking hyphens, no "Case" keyword), resolves each
  to a CELEX verified against the corpus, types it, and writes `citation_mentions`
  + `citation_edges`. Re-runnable full rebuild; pure parser is unit-tested
  (`test_extract_citations.py`, 26 assertions). ~60% resolve — the unresolved are
  mostly General Court `T‑` cases / orders, which the CJ-only corpus doesn't hold.
- **Phase 3** — `compute_citation_metrics.py` loads `citation_edges` into networkx
  and writes `decision_metrics` (weighted PageRank, HITS, Louvain communities).
  `app.py` uses `pagerank` as a PURE tie-breaker among equal rerank scores
  (relevance always wins) and shows "cited by N decisions" per source. Top-PageRank
  nodes are the EU-law canon (Becker, Marshall, Von Colson, Dassonville, Bosman,
  Cassis de Dijon) — a good sanity check. Needs `networkx` + `scipy` (in requirements).
- **Phase 2 (partial)** — `import_cellar_citations.py` imports the existing
  cjeu-py `gc_citations.parquet` (CELLAR CDM metadata) as `source='cellar'`
  mentions, keeping edges whose both endpoints are corpus decisions: 18,328 edges,
  of which 15,871 CONFIRM text edges (`from_text AND from_cellar`) and 2,457 are
  new. Edges now carry both source flags; metrics recomputed. The parquet is a
  capped/partial download (~7.7k of 13.9k citing decisions).

TODO:
- **Phase 2 (full)** — re-run `cjeu-py download-cellar` WITHOUT `--skip-citations`
  across all decisions (no cap) for the complete CELLAR skeleton, then re-import.
- **Recompute fan-out** — run `extract_citations.py` + `import_cellar_citations.py`
  + `compute_citation_metrics.py` after each ingestion so the graph stays current
  (idempotent; each source rebuild merges into `citation_edges`).
- **Answer enrichment** — feed "later cases that *distinguish* this one" into the
  answer for a "still good law?" signal.
- Stay in Postgres (recursive CTEs) at this scale; only consider a graph DB if
  interactive multi-hop becomes core.

## Database safety rules (IMPORTANT)
- Production Supabase holds ~10 GB. Treat it as production.
- Read-only diagnostics are fine. **Never** run destructive SQL (DROP, TRUNCATE,
  mass DELETE/UPDATE) or schema migrations against it without explicit human review.
- `.env` and secrets must stay gitignored; never print, commit, or echo credentials.
- Prefer migrations as reviewable `.sql` files over ad-hoc execution.

## Conventions
- Keep `app.py`'s UI text, disclaimers, and prompt wording stable unless asked.
- Use Plan Mode for multi-file or DB-touching changes.
- Small, reviewable commits; one concern per commit.
