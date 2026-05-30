import os
import json
from dotenv import load_dotenv
from openai import OpenAI

import psycopg
from pgvector.psycopg import register_vector

import streamlit as st

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

st.set_page_config(page_title="AMICUS", layout="wide")


def get_db_connection():
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

final_limit = st.sidebar.slider("Final sources", 3, 12, 8)
candidate_limit = st.sidebar.slider("Candidate pool per method", 20, 80, 40)

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

question = st.chat_input("Ask Amicus a legal research question...")

if question and question.strip():
    st.session_state.messages.append({"role": "user", "content": question})

    with st.chat_message("user"):
        st.markdown(question)

    recent_messages = st.session_state.messages[-10:]

    conversation_context = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in recent_messages
    )

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
            model="gpt-4.1-mini",
            input=standalone_prompt
        )

        retrieval_question = standalone_response.output_text.strip()

    with st.spinner("Running hybrid retrieval..."):
        embedding_response = client.embeddings.create(
            model="text-embedding-3-small",
            input=[retrieval_question]
        )

        query_embedding = embedding_response.data[0].embedding

        conn = get_db_connection()
        results = {}

        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    celex,
                    url,
                    paragraph_number,
                    paragraph_index,
                    text,
                    1 - (embedding <=> %s::vector) AS vector_score,
                    0::float AS keyword_score
                FROM cjeu_paragraphs
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """, (query_embedding, query_embedding, candidate_limit))

            for row in cur.fetchall():
                (
                    row_id,
                    celex,
                    url,
                    paragraph_number,
                    paragraph_index,
                    text,
                    vector_score,
                    keyword_score
                ) = row

                results[row_id] = {
                    "id": row_id,
                    "celex": celex,
                    "url": url,
                    "paragraph_number": paragraph_number,
                    "paragraph_index": paragraph_index,
                    "text": text,
                    "vector_score": float(vector_score),
                    "keyword_score": float(keyword_score),
                    "retrieval_source": "vector"
                }

            cur.execute("""
                SELECT
                    id,
                    celex,
                    url,
                    paragraph_number,
                    paragraph_index,
                    text,
                    0::float AS vector_score,
                    ts_rank_cd(search_vector, plainto_tsquery('english', %s)) AS keyword_score
                FROM cjeu_paragraphs
                WHERE search_vector @@ plainto_tsquery('english', %s)
                ORDER BY keyword_score DESC
                LIMIT %s;
            """, (retrieval_question, retrieval_question, candidate_limit))

            for row in cur.fetchall():
                (
                    row_id,
                    celex,
                    url,
                    paragraph_number,
                    paragraph_index,
                    text,
                    vector_score,
                    keyword_score
                ) = row

                if row_id in results:
                    results[row_id]["keyword_score"] = float(keyword_score)
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
                        "retrieval_source": "keyword"
                    }

        conn.close()

        candidates = list(results.values())

        for item in candidates:
            item["hybrid_score"] = (
                item["vector_score"] * 0.7
                + item["keyword_score"] * 0.3
            )

        candidates.sort(key=lambda x: x["hybrid_score"], reverse=True)
        rerank_candidates = candidates[:50]

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
                "text": item["text"][:2000]
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

        rerank_response = client.responses.create(
            model="gpt-4.1-mini",
            input=rerank_prompt
        )

        raw = rerank_response.output_text.strip()

        try:
            scores = json.loads(raw)
        except json.JSONDecodeError:
            st.error("Reranker did not return valid JSON.")
            st.code(raw)
            st.stop()

        score_map = {item["id"]: item for item in scores}

        ranked = []

        for i, item in enumerate(rerank_candidates, start=1):
            score_item = score_map.get(i, {"score": 0, "reason": "No score returned"})

            ranked.append({
                **item,
                "rerank_score": float(score_item.get("score", 0)),
                "rerank_reason": score_item.get("reason", "")
            })

        ranked.sort(
            key=lambda x: (x["rerank_score"], x["hybrid_score"]),
            reverse=True
        )

        top_results = ranked[:final_limit]

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
            model="gpt-4.1-mini",
            input=answer_prompt
        )

        assistant_answer = answer.output_text

    st.session_state.messages.append({
        "role": "assistant",
        "content": assistant_answer
    })

    st.session_state.messages = st.session_state.messages[-10:]

    with st.chat_message("assistant"):
        st.markdown(assistant_answer)

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
            st.write(item["text"])
