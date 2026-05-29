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

vector_limit = 30
keyword_limit = 30

results = {}

with conn.cursor() as cur:
    # 1. Vector search
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
    """, (query_embedding, query_embedding, vector_limit))

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
            "source": "vector"
        }

    # 2. Keyword full-text search
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
    """, (question, question, keyword_limit))

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
            results[row_id]["source"] = "both"
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
                "source": "keyword"
            }

# 3. Simple hybrid scoring
items = list(results.values())

for item in items:
    item["hybrid_score"] = (
        item["vector_score"] * 0.7
        + item["keyword_score"] * 0.3
    )

items.sort(key=lambda x: x["hybrid_score"], reverse=True)

top = items[:15]

print("\nTop hybrid results:\n")

for i, item in enumerate(top, start=1):
    print("=" * 80)
    print(f"Result {i}")
    print(f"CELEX: {item['celex']}")
    print(f"Paragraph: {item['paragraph_number']}")
    print(f"Source: {item['source']}")
    print(f"Vector score: {item['vector_score']:.4f}")
    print(f"Keyword score: {item['keyword_score']:.4f}")
    print(f"Hybrid score: {item['hybrid_score']:.4f}")
    print()
    print(item["text"][:1500])
    print()

conn.close()
