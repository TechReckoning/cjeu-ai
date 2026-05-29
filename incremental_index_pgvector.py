import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

conn = psycopg.connect("dbname=cjeu_ai user=serbansarbu host=localhost")
register_vector(conn)

cur = conn.cursor()

source_file = os.path.expanduser(
    "~/.cjeu-py/data/raw/texts/gc_texts.jsonl"
)

# Ensure full-text column exists
cur.execute("""
ALTER TABLE cjeu_paragraphs
ADD COLUMN IF NOT EXISTS search_vector tsvector;
""")

conn.commit()

# Load existing paragraph ids from PostgreSQL
print("Loading existing paragraph IDs from PostgreSQL...")

cur.execute("SELECT id FROM cjeu_paragraphs;")
existing_ids = {row[0] for row in cur.fetchall()}

print(f"Existing paragraphs in DB: {len(existing_ids)}")

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

            MAX_CHARS = 6000

            if len(paragraph) > MAX_CHARS:
                paragraph = paragraph[:MAX_CHARS]

            if len(paragraph) < 80:
                continue

            row_id = f"{celex}_p_{index}"

            if row_id in existing_ids:
                continue

            paragraph_number = (
                paragraph_nums[index]
                if index < len(paragraph_nums)
                else index + 1
            )

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

print(f"New paragraphs to index: {len(documents)}")

if not documents:
    print("Nothing new to index.")
    cur.close()
    conn.close()
    raise SystemExit

batch_size = 300

for start in range(0, len(documents), batch_size):
    end = min(start + batch_size, len(documents))

    batch_docs = documents[start:end]
    batch_rows = rows[start:end]

    print(f"Embedding new batch {start}–{end}...")

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
                    embedding,
                    search_vector
                )
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,
                    to_tsvector('english', coalesce(%s, ''))
                )
                ON CONFLICT (id) DO NOTHING;
                """, (
                    row["id"],
                    row["celex"],
                    row["url"],
                    row["language"],
                    row["paragraph_number"],
                    row["paragraph_index"],
                    row["text"],
                    embedding,
                    row["text"]
                ))

            conn.commit()
            break

        except RateLimitError:
            wait_time = 2 * (attempt + 1)
            print(f"Rate limit hit. Waiting {wait_time} seconds...")
            time.sleep(wait_time)

    time.sleep(0.3)

print("Incremental indexing completed.")

# Recreate/ensure indexes exist
print("Ensuring indexes exist...")

cur.execute("""
CREATE INDEX IF NOT EXISTS cjeu_embedding_idx
ON cjeu_paragraphs
USING hnsw (embedding vector_cosine_ops);
""")

cur.execute("""
CREATE INDEX IF NOT EXISTS cjeu_search_idx
ON cjeu_paragraphs
USING gin(search_vector);
""")

conn.commit()

print("Indexes ready.")

cur.close()
conn.close()
