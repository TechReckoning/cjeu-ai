import os
from dotenv import load_dotenv
from openai import OpenAI

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

conn = psycopg.connect("dbname=cjeu_ai user=serbansarbu host=localhost")
register_vector(conn)

question = input("Legal question: ")

embedding_response = client.embeddings.create(
    model="text-embedding-3-small",
    input=[question]
)

query_embedding = embedding_response.data[0].embedding

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
        LIMIT 8;
    """, (query_embedding, query_embedding))

    rows = cur.fetchall()

context_blocks = []

for i, row in enumerate(rows, start=1):
    celex, url, paragraph_number, paragraph_index, text, similarity = row
    context_blocks.append(
        f"[Source {i}] CELEX: {celex}, paragraph: {paragraph_number}, similarity: {similarity:.4f}\n{text}"
    )

context = "\n\n".join(context_blocks)

prompt = f"""
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
    input=prompt
)

print("\nAI Answer:\n")
print(answer.output_text)

print("\nSources used:\n")
for i, row in enumerate(rows, start=1):
    celex, url, paragraph_number, paragraph_index, text, similarity = row
    print(f"- Source {i}: CELEX {celex}, para. {paragraph_number}, similarity {similarity:.4f}")

conn.close()
