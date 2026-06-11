"""
AMICUS — Streamlit app (improved)

This is a drop-in replacement for app.py. UI text, prompts, models for the
rewrite/rerank steps, and the overall flow are preserved. The changes are:

1.  RRF FUSION  — hybrid_score now uses Reciprocal Rank Fusion (rank-based,
    scale-invariant) instead of adding cosine similarity and ts_rank_cd, which
    live on incommensurable scales. See FUSION section.
2.  websearch_to_tsquery instead of plainto_tsquery — plainto_tsquery ANDs every
    term, so long natural-language questions usually returned 0 keyword rows.
3.  FTS_CONFIG constant — currently 'english'. If the corpus is multilingual,
    switch this (or go per-language). Isolated here so it's a one-line change.
4.  CONNECTION POOLING — a single cached psycopg_pool.ConnectionPool is reused
    across reruns/sessions instead of opening a new connection every time.
    Falls back to the original per-call connect if psycopg_pool isn't installed.
    -> add `psycopg-pool` to requirements.txt to enable pooling.
5.  RERANKER FALLBACK — if the rerank model errors or returns invalid JSON, we
    fall back to the RRF order instead of killing the request with st.stop().
6.  ANALYTICS INTEGRITY — response_time_seconds and error_message are now written;
    retrieval_success reflects reality; FAILED queries are logged too (previously
    failures were never recorded, biasing the analytics).
7.  LIVE CORPUS STATS — the "X decisions / Y paragraphs" line is queried live
    (cached) instead of hardcoded, so it self-updates as ingestion runs.
8.  ANSWER_MODEL upgraded to gpt-4.1 for the reliability-critical answer step.
    Revert to "gpt-4.1-mini" if you prefer the lower cost.
"""

import os
import json
import time
from contextlib import contextmanager

from dotenv import load_dotenv
from openai import OpenAI
import psycopg
from pgvector.psycopg import register_vector
import streamlit as st

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

st.set_page_config(page_title="AMICUS", layout="wide")

# --------------------------------------------------------------------------- #
# Configuration (centralised so tuning is a one-line change)
# --------------------------------------------------------------------------- #
EMBED_MODEL = "text-embedding-3-small"
REWRITE_MODEL = "gpt-4.1-mini"
RERANK_MODEL = "gpt-4.1-mini"
ANSWER_MODEL = "gpt-4.1"          # [IMPROVED] upgraded from mini for the final answer
FTS_CONFIG = "english"           # [PENDING] switch if the corpus is multilingual
K_RRF = 60                       # standard RRF damping constant
RERANK_POOL = 50                 # how many fused candidates to send to the reranker
HNSW_EF_SEARCH = 100             # recall/latency knob for the HNSW vector index

# CORPUS SELECTOR — reversible cutover switch. "v2" = the rebuilt 1954-2026 corpus
# (paragraphs_v2/decisions_v2, section-aware: grounds+operative). "legacy" = the
# original cjeu_paragraphs. Flip back to "legacy" for an instant code-only
# rollback; both corpora remain in the database untouched.
CORPUS = os.getenv("AMICUS_CORPUS", "v2")
V2_SECTIONS = ("grounds", "operative")   # section-aware retrieval (excludes summary/catchwords)


# --------------------------------------------------------------------------- #
# Database access — pooled, with graceful fallback to the original behaviour
# --------------------------------------------------------------------------- #
def _build_conninfo():
    """Resolve a single connection string from secrets or environment."""
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT")
    dbname = os.getenv("SUPABASE_DBNAME")
    user = os.getenv("SUPABASE_USER")
    password = os.getenv("SUPABASE_PASSWORD")
    if host and dbname and user:
        return (
            f"host={host} port={port} dbname={dbname} "
            f"user={user} password={password} sslmode=require"
        )
    return None


@st.cache_resource
def get_pool():
    """
    A process-wide connection pool, created once and reused. Each new physical
    connection gets the pgvector type adapter registered via `configure`.
    Returns None if psycopg_pool isn't available, so the app still runs.
    """
    try:
        from psycopg_pool import ConnectionPool
    except Exception:
        return None
    conninfo = _build_conninfo()
    if not conninfo:
        return None

    def _configure(conn):
        register_vector(conn)

    try:
        pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=8,           # keep well under Supabase connection limits
            max_idle=300,
            configure=_configure,
            open=True,
        )
        return pool
    except Exception:
        return None


def get_db_connection():
    """Legacy per-call connection (fallback path when pooling is unavailable)."""
    try:
        database_url = st.secrets["DATABASE_URL"]
        conn = psycopg.connect(database_url)
    except Exception:
        conn = psycopg.connect(
            host=os.getenv("SUPABASE_HOST"),
            port=os.getenv("SUPABASE_PORT"),
            dbname=os.getenv("SUPABASE_DBNAME"),
            user=os.getenv("SUPABASE_USER"),
            password=os.getenv("SUPABASE_PASSWORD"),
            sslmode="require",
        )
    register_vector(conn)
    return conn


@contextmanager
def db():
    """
    Yield a DB connection. Uses the pool when available (auto-returned and
    committed on clean exit); otherwise opens and closes a direct connection.
    """
    pool = get_pool()
    if pool is not None:
        with pool.connection() as conn:
            yield conn
    else:
        conn = get_db_connection()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


@st.cache_data(ttl=3600)
def get_corpus_stats():
    """Live corpus counts, cached for an hour so the UI figures stay truthful."""
    table = "paragraphs_v2" if CORPUS == "v2" else "cjeu_paragraphs"
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*) AS paragraphs, "
                    f"count(DISTINCT celex) AS decisions FROM {table};"
                )
                paragraphs, decisions = cur.fetchone()
        return decisions, paragraphs
    except Exception:
        return None, None


@st.cache_data(ttl=3600)
def get_facet_options():
    """Distinct values for the sidebar facets (subject matter, country), cached.
    Only meaningful for the v2 corpus (decisions_v2 metadata). Returns ([], [])
    on legacy or error so the UI simply shows no filters."""
    if CORPUS != "v2":
        return [], []
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s, count(*) FROM decisions_v2, unnest(subject_matters) s "
                    "GROUP BY s ORDER BY count(*) DESC;"
                )
                subjects = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "SELECT country_origin, count(*) FROM decisions_v2 "
                    "WHERE country_origin IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC;"
                )
                countries = [r[0] for r in cur.fetchall()]
        return subjects, countries
    except Exception:
        return [], []


def log_query(
    user_question,
    retrieval_question,
    usage,
    candidate_count,
    source_count,
    retrieval_success,
    answer_length,
    response_time_seconds,
    error_message,
):
    """Insert one analytics row. Logs both successes AND failures."""
    try:
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amicus_queries (
                        user_question,
                        retrieval_question,
                        response_time_seconds,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        candidate_count,
                        source_count,
                        retrieval_success,
                        answer_length,
                        error_message
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_question,
                        retrieval_question,
                        response_time_seconds,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        candidate_count,
                        source_count,
                        retrieval_success,
                        answer_length,
                        error_message,
                    ),
                )
                return cur.fetchone()[0]
    except Exception as e:
        print("Analytics logging failed:", e)
        return None


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar.expander("Data Sources & Disclaimer", expanded=False):
    st.markdown("""
### Data Source
This application uses publicly available case law of the Court of Justice of the European Union (CJEU) obtained from the Publications Office of the European Union through the CELLAR repository.

### Independence and Non-Affiliation
This application is an independent legal research tool developed and operated by a private entity. It is not affiliated with, endorsed by, sponsored by, or otherwise connected to the Court of Justice of the European Union, the Publications Office of the European Union, the European Commission, or any other institution, body, office, or agency of the European Union.

### Disclaimer
This application uses artificial intelligence and automated retrieval technologies to assist with legal research. While reasonable efforts are made to ensure accuracy and relevance, the application may generate incomplete, inaccurate, outdated, or misleading information.

The information provided by this application is for research and informational purposes only and does not constitute legal advice.

Users are solely responsible for independently verifying all information, legal conclusions, citations, and references against the original official sources before relying on them for any legal, professional, academic, or commercial purpose.
""")

if st.sidebar.button("Clear Conversation"):
    st.session_state.messages = []
    st.rerun()

st.title("AMICUS")
st.subheader("Leave the Junior Alone. Ask Amicus.")
st.caption(
    "Independent AI-powered legal research across Court of Justice of the European Union case law. "
    "Not affiliated with the CJEU or any EU institution. "
    "AI-generated results may contain errors and must be independently verified."
)
st.caption("Hybrid semantic + full-text search with GPT reranking over CJEU case-law")

# [IMPROVED] live, self-updating corpus stats (was hardcoded)
_decisions, _paragraphs = get_corpus_stats()
if _decisions and _paragraphs:
    st.info(
        f"Amicus currently searches across {_decisions:,} CJEU decisions "
        f"and {_paragraphs:,} indexed case-law paragraphs."
    )
else:
    st.info("Amicus searches across CJEU case-law paragraphs.")

final_limit = st.sidebar.slider("Final sources", 3, 12, 8)
candidate_limit = st.sidebar.slider("Candidate pool per method", 20, 80, 40)

# --------------------------------------------------------------------------- #
# Faceted filters (v2 only) — optional, composable WHERE constraints on the
# decisions_v2 metadata. Empty = unfiltered (default behaviour unchanged).
# --------------------------------------------------------------------------- #
filter_subjects, filter_countries, filter_year_from, filter_year_to = [], [], None, None
if CORPUS == "v2":
    _subjects, _countries = get_facet_options()
    if _subjects or _countries:
        with st.sidebar.expander("Filters (optional)", expanded=False):
            st.caption(
                "Narrow the search by case metadata. Note: a filter excludes "
                "decisions where that field is missing."
            )
            filter_subjects = st.multiselect("Subject matter", _subjects, default=[])
            filter_countries = st.multiselect("Country of origin (referring State)",
                                              _countries, default=[])
            yr = st.slider("Decision year", 1954, 2026, (1954, 2026))
            # Only treat as an active filter if the user narrowed the full range.
            if yr != (1954, 2026):
                filter_year_from, filter_year_to = yr[0], yr[1]

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("Ask Amicus a legal research question...")

# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
if question and question.strip():
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    recent_messages = st.session_state.messages[-10:]
    conversation_context = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in recent_messages
    )

    # State carried through to logging regardless of success/failure
    t0 = time.perf_counter()
    retrieval_question = question
    rerank_candidates = []
    top_results = []
    assistant_answer = None
    answer_usage = None
    error_message = None

    try:
        # ----------------------------------------------------------------- #
        # 1. Contextualise into a standalone question
        # ----------------------------------------------------------------- #
        with st.spinner("Understanding the follow-up question..."):
            standalone_prompt = f"""
You are assisting with EU law legal research.

Rewrite the user's latest message into a standalone legal research question, using the recent conversation context if needed.

Recent conversation:
{conversation_context}

Latest user message:
{question}

Return only the standalone question. No markdown.
"""
            standalone_response = client.responses.create(
                model=REWRITE_MODEL,
                input=standalone_prompt,
            )
            retrieval_question = standalone_response.output_text.strip()

        # ----------------------------------------------------------------- #
        # 2. Hybrid retrieval (vector + keyword), fused with RRF
        # ----------------------------------------------------------------- #
        with st.spinner("Running hybrid retrieval..."):
            embedding_response = client.embeddings.create(
                model=EMBED_MODEL,
                input=[retrieval_question],
            )
            query_embedding = embedding_response.data[0].embedding

            # Corpus-aware retrieval SQL. v2: query paragraphs_v2 restricted to
            # grounds+operative (section-aware — the clean fix for the headnote
            # problem); url is derived (decisions_v2 has it, but the EUR-Lex link
            # is reconstructed from CELEX below anyway). legacy: original
            # cjeu_paragraphs. Both return the same column shape so the row
            # handling stays identical.
            if CORPUS == "v2":
                # Optional faceted filters -> extra WHERE clauses + params, applied
                # to a decisions_v2 join. Unset facets add nothing (unfiltered).
                facet_join = ""
                facet_where = ""
                facet_params = []
                if filter_subjects or filter_countries or filter_year_from:
                    facet_join = "JOIN decisions_v2 d ON d.celex = p.celex"
                    if filter_subjects:
                        facet_where += " AND d.subject_matters && %s"
                        facet_params.append(list(filter_subjects))
                    if filter_countries:
                        facet_where += " AND d.country_origin = ANY(%s)"
                        facet_params.append(list(filter_countries))
                    if filter_year_from:
                        facet_where += " AND d.decision_date >= %s AND d.decision_date <= %s"
                        facet_params.append(f"{filter_year_from}-01-01")
                        facet_params.append(f"{filter_year_to}-12-31")

                vector_sql = f"""
                    SELECT p.id, p.celex, NULL::text AS url, p.paragraph_number, p.seq AS paragraph_index, p.text,
                           1 - (p.embedding <=> %s::vector) AS vector_score, 0::float AS keyword_score
                    FROM paragraphs_v2 p {facet_join}
                    WHERE p.embedding IS NOT NULL AND p.section = ANY(%s){facet_where}
                    ORDER BY p.embedding <=> %s::vector LIMIT %s;
                """
                vector_params = (query_embedding, list(V2_SECTIONS), *facet_params,
                                 query_embedding, candidate_limit)
                keyword_sql = f"""
                    SELECT p.id, p.celex, NULL::text AS url, p.paragraph_number, p.seq AS paragraph_index, p.text,
                           0::float AS vector_score,
                           ts_rank_cd(p.search_vector, websearch_to_tsquery(%s::regconfig, %s)) AS keyword_score
                    FROM paragraphs_v2 p {facet_join}
                    WHERE p.section = ANY(%s) AND p.search_vector @@ websearch_to_tsquery(%s::regconfig, %s){facet_where}
                    ORDER BY keyword_score DESC LIMIT %s;
                """
                keyword_params = (FTS_CONFIG, retrieval_question, list(V2_SECTIONS),
                                  FTS_CONFIG, retrieval_question, *facet_params, candidate_limit)
            else:
                vector_sql = """
                    SELECT id, celex, url, paragraph_number, paragraph_index, text,
                           1 - (embedding <=> %s::vector) AS vector_score, 0::float AS keyword_score
                    FROM cjeu_paragraphs ORDER BY embedding <=> %s::vector LIMIT %s;
                """
                vector_params = (query_embedding, query_embedding, candidate_limit)
                keyword_sql = """
                    SELECT id, celex, url, paragraph_number, paragraph_index, text,
                           0::float AS vector_score,
                           ts_rank_cd(search_vector, websearch_to_tsquery(%s::regconfig, %s)) AS keyword_score
                    FROM cjeu_paragraphs
                    WHERE search_vector @@ websearch_to_tsquery(%s::regconfig, %s)
                    ORDER BY keyword_score DESC LIMIT %s;
                """
                keyword_params = (FTS_CONFIG, retrieval_question,
                                  FTS_CONFIG, retrieval_question, candidate_limit)

            results = {}
            with db() as conn:
                with conn.cursor() as cur:
                    # Vector arm — fetch order == vector rank
                    cur.execute(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)};")
                    cur.execute(vector_sql, vector_params)
                    for rank, row in enumerate(cur.fetchall(), start=1):
                        (row_id, celex, url, paragraph_number,
                         paragraph_index, text, vector_score, keyword_score) = row
                        results[row_id] = {
                            "id": row_id,
                            "celex": celex,
                            "url": url,
                            "paragraph_number": paragraph_number,
                            "paragraph_index": paragraph_index,
                            "text": text,
                            "vector_score": float(vector_score),
                            "keyword_score": float(keyword_score),
                            "vector_rank": rank,
                            "keyword_rank": None,
                            "retrieval_source": "vector",
                        }

                    # Keyword arm — fetch order == keyword rank
                    # websearch_to_tsquery: supports OR/phrases (plainto AND-ed everything)
                    cur.execute(keyword_sql, keyword_params)
                    for rank, row in enumerate(cur.fetchall(), start=1):
                        (row_id, celex, url, paragraph_number,
                         paragraph_index, text, vector_score, keyword_score) = row
                        if row_id in results:
                            results[row_id]["keyword_score"] = float(keyword_score)
                            results[row_id]["keyword_rank"] = rank
                            results[row_id]["retrieval_source"] = "both"
                        else:
                            results[row_id] = {
                                "id": row_id,
                                "celex": celex,
                                "url": url,
                                "paragraph_number": paragraph_number,
                                "paragraph_index": paragraph_index,
                                "text": text,
                                "vector_score": float(vector_score),
                                "keyword_score": float(keyword_score),
                                "vector_rank": None,
                                "keyword_rank": rank,
                                "retrieval_source": "keyword",
                            }

            # ---- FUSION: Reciprocal Rank Fusion -------------------------- #
            for item in results.values():
                vr = item.get("vector_rank")
                kr = item.get("keyword_rank")
                item["hybrid_score"] = (
                    (1.0 / (K_RRF + vr) if vr else 0.0)
                    + (1.0 / (K_RRF + kr) if kr else 0.0)
                )

            candidates = list(results.values())
            candidates.sort(key=lambda x: x["hybrid_score"], reverse=True)
            rerank_candidates = candidates[:RERANK_POOL]

            # Citation-graph authority (PageRank) for the candidate decisions,
            # used ONLY as a tie-breaker among equally-relevant rerank scores.
            # Resilient: any failure leaves authority at 0 (no ranking change).
            authority_by_celex = {}
            cand_celexes = list({item["celex"] for item in rerank_candidates})
            if cand_celexes:
                try:
                    with db() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT celex, pagerank, in_degree "
                                "FROM decision_metrics WHERE celex = ANY(%s);",
                                (cand_celexes,),
                            )
                            for celex, pagerank, in_degree in cur.fetchall():
                                authority_by_celex[celex] = (
                                    float(pagerank or 0.0), int(in_degree or 0)
                                )
                except Exception as auth_err:
                    print("Authority lookup failed (continuing without it):", auth_err)
            for item in rerank_candidates:
                pr, indeg = authority_by_celex.get(item["celex"], (0.0, 0))
                item["authority_pagerank"] = pr
                item["citation_in_degree"] = indeg

        # ----------------------------------------------------------------- #
        # 3. LLM rerank (with graceful fallback to RRF order)
        # ----------------------------------------------------------------- #
        with st.spinner("Reranking legally relevant sources..."):
            candidate_blocks = []
            for i, item in enumerate(rerank_candidates, start=1):
                candidate_blocks.append({
                    "id": i,
                    "celex": item["celex"],
                    "paragraph_number": item["paragraph_number"],
                    "paragraph_index": item["paragraph_index"],
                    "retrieval_source": item["retrieval_source"],
                    "vector_score": item["vector_score"],
                    "keyword_score": item["keyword_score"],
                    "hybrid_score": item["hybrid_score"],
                    "text": item["text"][:2000],
                })

            rerank_prompt = f"""
You are a legal relevance reranker for EU Court of Justice case-law.

User's standalone research question:
{retrieval_question}

Original latest user message:
{question}

Recent conversation:
{conversation_context}

Task:
Evaluate each candidate paragraph for legal relevance to the standalone research question.

Scoring:
10 = directly answers the legal question
8-9 = highly relevant legal principle
5-7 = somewhat relevant background
1-4 = tangentially related
0 = irrelevant or wrong legal concept

Important:
- Prefer paragraphs that directly address the specific legal concept in the question.
- Prefer authoritative legal tests, definitions, criteria, conditions, and standards.
- Penalize paragraphs that merely share similar words but concern a different legal concept.
- Penalize procedural fragments unless the user's question is procedural.
- Do not favor keyword matches if the legal concept is wrong.
- Return ONLY valid JSON.
- No markdown.

JSON format:
[
{{"id": 1, "score": 9, "reason": "short reason"}},
{{"id": 2, "score": 3, "reason": "short reason"}}
]

Candidates:
{json.dumps(candidate_blocks, ensure_ascii=False)}
"""
            score_map = {}
            try:
                rerank_response = client.responses.create(
                    model=RERANK_MODEL,
                    input=rerank_prompt,
                )
                scores = json.loads(rerank_response.output_text.strip())
                score_map = {s["id"]: s for s in scores}
            except Exception as rerank_err:
                # Fallback: keep the RRF order rather than failing the request.
                print("Reranker unavailable, falling back to hybrid order:", rerank_err)
                score_map = {}

            if score_map:
                ranked = []
                for i, item in enumerate(rerank_candidates, start=1):
                    s = score_map.get(i, {"score": 0, "reason": "No score returned"})
                    ranked.append({
                        **item,
                        "rerank_score": float(s.get("score", 0)),
                        "rerank_reason": s.get("reason", ""),
                    })
                ranked.sort(
                    key=lambda x: (x["rerank_score"], x["authority_pagerank"], x["hybrid_score"]),
                    reverse=True,
                )
            else:
                ranked = [
                    {
                        **item,
                        "rerank_score": 0.0,
                        "rerank_reason": "Reranker unavailable — using hybrid rank",
                    }
                    for item in rerank_candidates
                ]

            top_results = ranked[:final_limit]

        # ----------------------------------------------------------------- #
        # 4. Answer generation
        # ----------------------------------------------------------------- #
        with st.spinner("Generating answer..."):
            context_blocks = []
            for i, item in enumerate(top_results, start=1):
                context_blocks.append(
                    f"[Source {i}] CELEX: {item['celex']}, paragraph: {item['paragraph_number']}, "
                    f"retrieval_source: {item['retrieval_source']}, "
                    f"rerank_score: {item['rerank_score']}, "
                    f"hybrid_score: {item['hybrid_score']:.4f}\n"
                    f"{item['text']}"
                )
            context = "\n\n".join(context_blocks)

            answer_prompt = f"""
You are Amicus, a careful EU law research assistant.

Answer the user's latest message using ONLY the sources below and the recent conversation context.
If the sources are insufficient, say that the current database is insufficient.
Do not invent cases, principles, citations, or legal rules.

For every legal proposition, cite the source as:
(CELEX [number], para. [paragraph_number]).

Recent conversation:
{conversation_context}

Standalone research question used for retrieval:
{retrieval_question}

Latest user message:
{question}

Sources:
{context}
"""
            answer = client.responses.create(
                model=ANSWER_MODEL,
                input=answer_prompt,
            )
            assistant_answer = answer.output_text
            answer_usage = answer.usage

    except Exception as e:
        error_message = str(e)
        assistant_answer = (
            "Sorry — something went wrong while researching this question. "
            "Please try again. If it persists, the database or model service may be unavailable."
        )
        st.error("Retrieval failed. The error has been logged.")

    # --------------------------------------------------------------------- #
    # 5. Analytics logging — runs for BOTH success and failure
    # --------------------------------------------------------------------- #
    elapsed = round(time.perf_counter() - t0, 3)
    retrieval_success = bool(top_results) and error_message is None
    query_id = log_query(
        user_question=question,
        retrieval_question=retrieval_question,
        usage=answer_usage,
        candidate_count=len(rerank_candidates),
        source_count=len(top_results),
        retrieval_success=retrieval_success,
        answer_length=len(assistant_answer or ""),
        response_time_seconds=elapsed,
        error_message=error_message,
    )
    if query_id:
        st.session_state.last_query_id = query_id

    # --------------------------------------------------------------------- #
    # 6. Render
    # --------------------------------------------------------------------- #
    st.session_state.messages.append({
        "role": "assistant",
        "content": assistant_answer,
    })
    st.session_state.messages = st.session_state.messages[-10:]

    with st.chat_message("assistant"):
        st.markdown(assistant_answer)

    if top_results:
        st.subheader("Sources")
        for i, item in enumerate(top_results, start=1):
            celex = item["celex"]
            eurlex_url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
            with st.expander(
                f"Source {i} — CELEX {celex}, para. {item['paragraph_number']} | "
                f"{item['retrieval_source']} | rerank {item['rerank_score']} | "
                f"hybrid {item['hybrid_score']:.4f}"
            ):
                st.markdown(f"[Open on EUR-Lex]({eurlex_url})")
                st.write(f"Rerank reason: {item['rerank_reason']}")
                st.write(f"Vector score: {item['vector_score']:.4f}")
                st.write(f"Keyword score: {item['keyword_score']:.4f}")
                if item.get("citation_in_degree"):
                    st.write(f"Citation authority: cited by {item['citation_in_degree']} decisions")
                st.write(item["text"])

        # ----------------------------------------------------------------- #
        # Doctrinal context (citation-graph derived; NOT part of the grounded
        # answer). Deterministic: no LLM, so no hallucination risk. Surfaces
        # foundational authorities the sources rely on + later "distinguishing"
        # treatment. Degrades to nothing if the graph is unavailable.
        # ----------------------------------------------------------------- #
        try:
            # Foundational authorities describe the TOPIC, so compute them from
            # the broader retrieved pool (rerank candidates), not just the final
            # few shown sources — a wider citing set surfaces the doctrinal roots
            # the topic's case law relies on. Later-treatment targets still
            # include the shown sources.
            pool_celexes = list({item["celex"] for item in rerank_candidates})
            shown_celexes = list({item["celex"] for item in top_results})
            foundational = []
            distinguished = []
            if pool_celexes:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT e.cited_celex,
                                   count(DISTINCT e.citing_celex) AS support,
                                   m.in_degree
                            FROM citation_edges e
                            JOIN decision_metrics m ON m.celex = e.cited_celex
                            WHERE e.citing_celex = ANY(%s)
                              AND e.cited_celex <> ALL(%s)
                            GROUP BY e.cited_celex, m.in_degree
                            HAVING count(DISTINCT e.citing_celex) >= 2
                            ORDER BY support DESC, m.in_degree DESC NULLS LAST
                            LIMIT 5;
                            """,
                            (pool_celexes, pool_celexes),
                        )
                        foundational = cur.fetchall()

                        targets = shown_celexes + [row[0] for row in foundational]
                        cur.execute(
                            """
                            SELECT cited_celex, count(*) AS n
                            FROM citation_edges
                            WHERE cited_celex = ANY(%s)
                              AND dominant_relation_type = 'distinguishing'
                            GROUP BY cited_celex
                            ORDER BY n DESC;
                            """,
                            (targets,),
                        )
                        distinguished = cur.fetchall()

            if foundational or distinguished:
                st.subheader("Doctrinal context")
                st.caption(
                    "Derived from the CJEU citation graph — navigational context, "
                    "not part of the cited answer above."
                )
                if foundational:
                    st.markdown("**Foundational authorities for this topic**")
                    for celex, _support, in_degree in foundational:
                        url = (
                            "https://eur-lex.europa.eu/legal-content/EN/TXT/"
                            f"?uri=CELEX:{celex}"
                        )
                        st.markdown(
                            f"- [{celex}]({url}) — cited by {in_degree or 0} decisions"
                        )
                if distinguished:
                    st.markdown("**Later treatment**")
                    for celex, n in distinguished:
                        url = (
                            "https://eur-lex.europa.eu/legal-content/EN/TXT/"
                            f"?uri=CELEX:{celex}"
                        )
                        st.markdown(
                            f"- ⚠️ [{celex}]({url}) has been distinguished "
                            f"by {n} later decision(s)"
                        )
        except Exception as doctrinal_err:
            print("Doctrinal context unavailable:", doctrinal_err)

# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #
if "last_query_id" in st.session_state:
    st.markdown("---")
    st.caption("Was this answer helpful?")
    feedback_query_id = st.session_state.last_query_id
    col1, col2 = st.columns(2)
    with col1:
        if st.button("👍 Helpful", key=f"helpful_{feedback_query_id}"):
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE amicus_queries SET feedback = 1 WHERE id = %s",
                        (feedback_query_id,),
                    )
            st.session_state.feedback_given = "helpful"
            st.success("Thank you for your feedback.")
    with col2:
        if st.button("👎 Not Helpful", key=f"not_helpful_{feedback_query_id}"):
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE amicus_queries SET feedback = -1 WHERE id = %s",
                        (feedback_query_id,),
                    )
            st.session_state.feedback_given = "not_helpful"
            st.success("Thank you for your feedback.")
