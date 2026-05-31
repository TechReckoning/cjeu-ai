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

## Database schema (Supabase, public schema)
**`cjeu_paragraphs`** (~601k rows, ~10 GB; embedding-dominated):
`id` (text, PK) · `celex` (text) · `url` (text) · `language` (text) ·
`paragraph_number` (int) · `paragraph_index` (int) · `text` (text) ·
`embedding` (vector, 1536-dim, text-embedding-3-small) · `search_vector` (tsvector).
Indexes: HNSW `cjeu_embedding_idx (embedding vector_cosine_ops)`,
GIN `cjeu_search_idx (search_vector)`. No index on `language` or `celex` yet.

**`amicus_queries`** (analytics + feedback): `id` · `created_at` · `user_question`
· `retrieval_question` · `response_time_seconds` · `input/output/total_tokens` ·
`candidate_count` · `source_count` · `feedback` (1 / -1) · `retrieval_success` ·
`answer_length` · `error_message`. Currently tiny (~tens of rows) — too little
data for any learning-to-rank/fine-tuning yet. Priority is clean instrumentation
and accumulating honest data (including failures).

## Pipeline (in app.py)
1. Rewrite latest message into a standalone question (gpt-4.1-mini).
2. Embed (text-embedding-3-small).
3. Hybrid retrieval: vector query + keyword query, fused with **Reciprocal Rank
   Fusion (RRF, k=60)** — NOT raw-score weighting.
4. LLM rerank top ~50 (gpt-4.1-mini), with fallback to RRF order on failure.
5. Answer (gpt-4.1) using ONLY retrieved sources, with CELEX/paragraph citations.
6. Log to `amicus_queries` (successes AND failures); 👍/👎 feedback.

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

## OPEN DECISION — full-text search language
`FTS_CONFIG` is currently `"english"`. The corpus has a `language` column and may
be multilingual (French is the Court's working language). Pending the language
distribution: if overwhelmingly English, leave as-is; if multilingual, route each
query to a per-language config (and add a `language` filter + supporting index),
or move to a neutral config. Do NOT silently change this without the data.

## Roadmap (next major feature)
**Citation graph** of CJEU decisions, to surface doctrinal evolution and improve
answer reliability. Design constraints:
- Store edges in **separate tables**, NOT by widening `cjeu_paragraphs` (hot table).
- Two edge sources: CELLAR CDM citation metadata (clean skeleton) + citations
  parsed from judgment text (paragraph-precise, the valuable part).
- Make edges **typed/signed** (following / by analogy / distinguishing /
  consolidating) by detecting the Court's stereotyped phrasing.
- Compute graph metrics (authority/PageRank, communities) offline in batch with
  `networkx`; write scores back as columns. Stay in Postgres (recursive CTEs) at
  this scale; only consider a graph DB if interactive multi-hop becomes core.
- Ingestion must fan out idempotently: embeddings + citation edges + metric recompute.

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
