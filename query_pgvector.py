import os
from dotenv import load_dotenv
from openai import OpenAI

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

conn = psycopg.connect("dbname=cjeu_ai user=serbansarbu host=localhost")
register_vector(conn)

question = input("Question: ")

embedding_response = client.embeddings.create(
    model="text-embedding-3-small",
    input=[question]
)

query_embedding = embedding_response.data[0].embedding

with conn.cursor() as cur:
    cur.execute("""
        SELECT
            celex,
            paragraph_number,
            paragraph_index,
            text,
            1 - (embedding <=> %s::vector) AS similarity
        FROM cjeu_paragraphs
        ORDER BY embedding <=> %s::vector
        LIMIT 8;
    """, (query_embedding, query_embedding))

    rows = cur.fetchall()

print("\nTop pgvector results:\n")

for i, row in enumerate(rows, start=1):
    celex, para_no, para_index, text, similarity = row
    print("=" * 80)
    print(f"Result {i}")
    print(f"CELEX: {celex}")
    print(f"Paragraph: {para_no}")
    print(f"Paragraph index: {para_index}")
    print(f"Similarity: {similarity:.4f}")
    print()
    print(text[:1500])
    print()

conn.close()
