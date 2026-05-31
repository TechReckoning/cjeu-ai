"""
Incremental indexer — embeds new CJEU paragraphs and writes them straight to
the production Supabase Postgres (pgvector).

This replaces the previous design, which wrote to a local `cjeu_ai` Postgres and
relied on a separate `incremental_index_supabase.py` (missing from the repo) to
push local -> Supabase. There is now ONE target: whatever DATABASE_URL /
SUPABASE_* point to — the same connection `app.py` reads from.

Source of paragraphs: the JSONL produced by `cjeu-py fetch-texts`
(default ~/.cjeu-py/data/raw/texts/gc_texts.jsonl; override with CJEU_TEXTS_FILE).

Idempotent: paragraph IDs are `{celex}_p_{index}`; existing IDs (for the CELEXes
in the input file) are skipped before embedding, and INSERT uses
ON CONFLICT (id) DO NOTHING as a backstop.

Schema and indexes are NOT managed here — they live in migrations. New rows are
indexed automatically by the existing HNSW/GIN indexes on INSERT.

Usage:
    python incremental_index_pgvector.py            # embed + write to Supabase
    python incremental_index_pgvector.py --dry-run  # report counts, write nothing
"""

import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

import psycopg
from pgvector.psycopg import register_vector

load_dotenv()

EMBED_MODEL = "text-embedding-3-small"   # 1536-dim, matches cjeu_paragraphs.embedding
FTS_CONFIG = "english"                    # corpus is English-only (see claude.md)
MAX_CHARS = 6000                          # truncate very long paragraphs
MIN_CHARS = 80                            # skip fragments shorter than this
BATCH_SIZE = 300                          # embeddings per OpenAI request
MAX_RETRIES = 5

DRY_RUN = "--dry-run" in sys.argv

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def build_conninfo():
    """Resolve a Postgres connection string from the environment (Supabase)."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url, "DATABASE_URL"
    host = os.getenv("SUPABASE_HOST")
    port = os.getenv("SUPABASE_PORT")
    dbname = os.getenv("SUPABASE_DBNAME")
    user = os.getenv("SUPABASE_USER")
    password = os.getenv("SUPABASE_PASSWORD")
    if host and dbname and user:
        return (
            f"host={host} port={port} dbname={dbname} "
            f"user={user} password={password} sslmode=require"
        ), host
    raise SystemExit(
        "No database connection configured. Set DATABASE_URL, or "
        "SUPABASE_HOST/PORT/DBNAME/USER/PASSWORD."
    )


def load_new_rows(source_file, existing_ids_for):
    """
    Parse the JSONL and return (documents, rows) for paragraphs not already in
    the DB. `existing_ids_for(celexes)` returns the set of paragraph IDs that
    already exist for the given CELEXes.
    """
    items = []
    celexes = set()
    with open(source_file, "r") as f:
        for line in f:
            item = json.loads(line)
            items.append(item)
            celexes.add(item.get("celex", "unknown"))

    print(f"Decisions in input file: {len(items)} ({len(celexes)} distinct CELEXes)")
    existing_ids = existing_ids_for(celexes)
    print(f"Existing paragraphs in DB for those CELEXes: {len(existing_ids)}")

    documents, rows = [], []
    for item in items:
        celex = item.get("celex", "unknown")
        url = item.get("url", "")
        language = item.get("language", "")
        paragraphs = item.get("paragraphs", [])
        paragraph_nums = item.get("paragraph_nums", [])

        for index, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if len(paragraph) > MAX_CHARS:
                paragraph = paragraph[:MAX_CHARS]
            if len(paragraph) < MIN_CHARS:
                continue

            row_id = f"{celex}_p_{index}"
            if row_id in existing_ids:
                continue

            paragraph_number = (
                paragraph_nums[index] if index < len(paragraph_nums) else index + 1
            )
            documents.append(paragraph)
            rows.append({
                "id": row_id,
                "celex": celex,
                "url": url,
                "language": language,
                "paragraph_number": paragraph_number,
                "paragraph_index": index,
                "text": paragraph,
            })
    return documents, rows


def insert_batch(cur, batch_rows, embeddings):
    for row, embedding in zip(batch_rows, embeddings):
        cur.execute(
            """
            INSERT INTO cjeu_paragraphs (
                id, celex, url, language,
                paragraph_number, paragraph_index, text,
                embedding, search_vector
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                to_tsvector(%s::regconfig, coalesce(%s, ''))
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            (
                row["id"], row["celex"], row["url"], row["language"],
                row["paragraph_number"], row["paragraph_index"], row["text"],
                embedding, FTS_CONFIG, row["text"],
            ),
        )


def main():
    source_file = os.path.expanduser(
        os.getenv("CJEU_TEXTS_FILE", "~/.cjeu-py/data/raw/texts/gc_texts.jsonl")
    )
    if not os.path.exists(source_file):
        raise SystemExit(f"Source file not found: {source_file}")

    conninfo, target_label = build_conninfo()
    print(f"Target database: {target_label}")
    if DRY_RUN:
        print("DRY RUN — no rows will be written.")

    conn = psycopg.connect(conninfo)
    register_vector(conn)
    cur = conn.cursor()

    def existing_ids_for(celexes):
        cur.execute(
            "SELECT id FROM cjeu_paragraphs WHERE celex = ANY(%s);",
            (list(celexes),),
        )
        return {r[0] for r in cur.fetchall()}

    documents, rows = load_new_rows(source_file, existing_ids_for)
    print(f"New paragraphs to index: {len(documents)}")

    if not documents:
        print("Nothing new to index.")
        cur.close()
        conn.close()
        return

    if DRY_RUN:
        print(f"DRY RUN — would embed and insert {len(documents)} paragraphs.")
        cur.close()
        conn.close()
        return

    inserted = 0
    for start in range(0, len(documents), BATCH_SIZE):
        end = min(start + BATCH_SIZE, len(documents))
        batch_docs = documents[start:end]
        batch_rows = rows[start:end]
        print(f"Embedding batch {start}-{end}...")

        for attempt in range(MAX_RETRIES):
            try:
                response = client.embeddings.create(
                    model=EMBED_MODEL, input=batch_docs
                )
                embeddings = [d.embedding for d in response.data]
                insert_batch(cur, batch_rows, embeddings)
                conn.commit()
                inserted += len(batch_rows)
                break
            except RateLimitError:
                wait_time = 2 * (attempt + 1)
                print(f"Rate limit hit. Waiting {wait_time}s...")
                time.sleep(wait_time)
        else:
            conn.rollback()
            print(f"Batch {start}-{end} failed after {MAX_RETRIES} retries; skipping.")

        time.sleep(0.3)

    print(f"Incremental indexing completed. Inserted ~{inserted} paragraphs.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
