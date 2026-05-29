import os
import json
from dotenv import load_dotenv
from openai import OpenAI

import psycopg
from pgvector.psycopg import register_vector

import streamlit as st

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

st.set_page_config(page_title="CJEU AI Research", layout="wide")

st.title("CJEU AI Research")
st.caption("Hybrid semantic + full-text search with GPT reranking over CJEU case-law")

question = st.text_area("Legal question", height=100)

final_limit = st.slider("Final sources", 3, 12, 8)
candidate_limit = st.slider("Candidate pool per method", 20, 80, 40)

if st.button("Ask") and question.strip():
    with st.spinner("Running hybrid retrieval..."):
        embedding_response = client.embeddings.create(
            model="text-embedding-3-small",
            input=[question]
        )

        query_embedding = embedding_response.data[0].embedding

        conn = psycopg.connect("dbname=cjeu_ai user=serbansarbu host=localhost")
        register_vector(conn)

        results = {}

        with conn.cursor() as cur:
            # Vector search
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

            # Full-text keyword search
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
            """, (question, question, candidate_limit))

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

User question:
{question}

Task:
Evaluate each candidate paragraph for legal relevance to the user's question.

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
You are a careful EU law research assistant.

Answer the user's legal question using ONLY the sources below.
If the sources are insufficient, say that the current local database is insufficient.
Do not invent cases, principles, citations, or legal rules.

For every legal proposition, cite the source as:
(CELEX [number], para. [paragraph_number]).

User question:
{question}

Sources:
{context}
"""

        answer = client.responses.create(
            model="gpt-4.1-mini",
            input=answer_prompt
        )

    st.subheader("AI Answer")
    st.write(answer.output_text)

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
