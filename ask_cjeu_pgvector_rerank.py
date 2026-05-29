import os
import json
from dotenv import load_dotenv
from openai import OpenAI

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

conn = psycopg.connect("dbname=cjeu_ai user=serbansarbu host=localhost")
register_vector(conn)

question = input("Legal question: ")

# 1. Embed question
embedding_response = client.embeddings.create(
    model="text-embedding-3-small",
    input=[question]
)

query_embedding = embedding_response.data[0].embedding

# 2. Retrieve broad candidate pool from pgvector
candidate_limit = 40
final_limit = 8

with conn.cursor() as cur:
    cur.execute("""
        SELECT
            celex,
            url,
            paragraph_number,
            paragraph_index,
            text,
            1 - (embedding <=> %s::vector) AS similarity
        FROM cjeu_paragraphs
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
    """, (query_embedding, query_embedding, candidate_limit))

    candidates = cur.fetchall()

# 3. Prepare reranking input
candidate_blocks = []

for i, row in enumerate(candidates, start=1):
    celex, url, paragraph_number, paragraph_index, text, similarity = row
    candidate_blocks.append({
        "id": i,
        "celex": celex,
        "paragraph_number": paragraph_number,
        "paragraph_index": paragraph_index,
        "similarity": float(similarity),
        "text": text[:2000]
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
- Penalize paragraphs that merely share similar words but concern a different legal concept.
- For example, "abuse of dominant position" is not the same as "misuse of powers", unless the question asks about competition law abuse.
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
    print("Reranker did not return valid JSON:")
    print(raw)
    conn.close()
    raise SystemExit

score_map = {item["id"]: item for item in scores}

ranked = []

for i, row in enumerate(candidates, start=1):
    celex, url, paragraph_number, paragraph_index, text, similarity = row
    score_item = score_map.get(i, {"score": 0, "reason": "No score returned"})

    ranked.append({
        "id": i,
        "celex": celex,
        "url": url,
        "paragraph_number": paragraph_number,
        "paragraph_index": paragraph_index,
        "text": text,
        "similarity": float(similarity),
        "rerank_score": float(score_item.get("score", 0)),
        "rerank_reason": score_item.get("reason", "")
    })

ranked.sort(
    key=lambda x: (x["rerank_score"], x["similarity"]),
    reverse=True
)

top_results = ranked[:final_limit]

# 4. Build final answer using reranked sources
context_blocks = []

for i, item in enumerate(top_results, start=1):
    context_blocks.append(
        f"[Source {i}] CELEX: {item['celex']}, paragraph: {item['paragraph_number']}, "
        f"rerank_score: {item['rerank_score']}, similarity: {item['similarity']:.4f}\n"
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

print("\nAI Answer:\n")
print(answer.output_text)

print("\nReranked sources used:\n")
for i, item in enumerate(top_results, start=1):
    print(
        f"- Source {i}: CELEX {item['celex']}, para. {item['paragraph_number']}, "
        f"rerank {item['rerank_score']}, similarity {item['similarity']:.4f} — "
        f"{item['rerank_reason']}"
    )

conn.close()
