import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

conn = psycopg.connect(
    "dbname=cjeu_ai user=serbansarbu host=localhost"
)

register_vector(conn)

cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS cjeu_paragraphs (
    id TEXT PRIMARY KEY,
    celex TEXT,
    url TEXT,
    language TEXT,
    paragraph_number INT,
    paragraph_index INT,
    text TEXT,
    embedding vector(1536)
);
""")

conn.commit()

source_file = os.path.expanduser(
    "~/.cjeu-py/data/raw/texts/gc_texts.jsonl"
)

documents = []
rows = []

with open(source_file, "r") as f:
    for line in f:
        item = json.loads(line)

        celex = item.get("celex", "unknown")
        url = item.get("url", "")
        language = item.get("language", "")

        paragraphs = item.get("paragraphs", [])
        paragraph_nums = item.get("paragraph_nums", [])

        for index, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()

            if len(paragraph) < 80:
                continue

            paragraph_number = (
                paragraph_nums[index]
                if index < len(paragraph_nums)
                else index + 1
            )

            row_id = f"{celex}_p_{index}"

            documents.append(paragraph)

            rows.append({
                "id": row_id,
                "celex": celex,
                "url": url,
                "language": language,
                "paragraph_number": paragraph_number,
                "paragraph_index": index,
                "text": paragraph
            })

print(f"Prepared {len(documents)} paragraphs")

batch_size = 300

for start in range(0, len(documents), batch_size):
    end = min(start + batch_size, len(documents))

    batch_docs = documents[start:end]
    batch_rows = rows[start:end]

    print(f"Embedding batch {start}–{end}...")

    max_retries = 5

    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=batch_docs
            )

            embeddings = [d.embedding for d in response.data]

            for row, embedding in zip(batch_rows, embeddings):
                cur.execute("""
                INSERT INTO cjeu_paragraphs (
                    id,
                    celex,
                    url,
                    language,
                    paragraph_number,
                    paragraph_index,
                    text,
                    embedding
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING;
                """, (
                    row["id"],
                    row["celex"],
                    row["url"],
                    row["language"],
                    row["paragraph_number"],
                    row["paragraph_index"],
                    row["text"],
                    embedding
                ))

            conn.commit()

            break

        except RateLimitError:
            wait_time = 2 * (attempt + 1)
            print(f"Rate limit hit. Waiting {wait_time} seconds...")
            time.sleep(wait_time)

    time.sleep(0.3)

print("Migration completed.")

cur.execute("""
CREATE INDEX IF NOT EXISTS cjeu_embedding_idx
ON cjeu_paragraphs
USING hnsw (embedding vector_cosine_ops);
""")

conn.commit()

print("Vector index created.")

cur.close()
conn.close()
