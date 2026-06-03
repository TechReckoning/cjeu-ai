"""
Backfill embeddings for paragraphs_v2 rows that were written with --no-embed.
Reads rows where embedding IS NULL, embeds in batches, updates in place.
Pooler-safe (single connection, batched). Re-runnable (only NULLs).

Usage: python embed_v2.py            # all NULL-embedding paragraphs
       python embed_v2.py --year 2018
"""
import os, sys, time
from dotenv import load_dotenv
from openai import OpenAI
import psycopg
from pgvector.psycopg import register_vector

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv()
# Explicit timeout + retries so a hung connection can't stall the run forever.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=30.0, max_retries=5)
EMBED_MODEL = "text-embedding-3-small"
BATCH = 300


def embed_with_retry(texts, attempts=5):
    for a in range(attempts):
        try:
            return client.embeddings.create(model=EMBED_MODEL, input=texts)
        except Exception as e:
            wait = 3 * (a + 1)
            print(f"  embed error ({e}); retry in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("embedding failed after retries")

year = None
if "--year" in sys.argv:
    year = sys.argv[sys.argv.index("--year") + 1]

where = "embedding IS NULL"
params = []
if year:
    where += " AND celex LIKE %s"
    params = [f"6{year}CJ%"]


def connect():
    """Fresh short-lived connection per batch. A single long-lived connection
    stalls indefinitely on the Supabase pooler (psycopg blocks in wait_c/poll).
    A statement_timeout is a server-side backstop so no query hangs forever."""
    c = psycopg.connect(os.getenv("DATABASE_URL"), prepare_threshold=None,
                        connect_timeout=20)
    c.execute("SET statement_timeout = '60s'")
    register_vector(c)
    return c


c = connect()
total = c.execute(f"SELECT count(*) FROM paragraphs_v2 WHERE {where}", params).fetchone()[0]
c.close()
print(f"paragraphs to embed: {total}", flush=True)

done = 0
while True:
    c = connect()
    cur = c.cursor()
    cur.execute(f"SELECT id, text FROM paragraphs_v2 WHERE {where} LIMIT %s", params + [BATCH])
    rows = cur.fetchall()
    if not rows:
        c.close()
        break
    ids = [r[0] for r in rows]
    texts = [r[1][:8000] for r in rows]
    resp = embed_with_retry(texts)          # OpenAI call OUTSIDE any open DB txn
    updates = [(d.embedding, ids[i]) for i, d in enumerate(resp.data)]
    cur.executemany("UPDATE paragraphs_v2 SET embedding=%s WHERE id=%s", updates)
    c.commit()
    c.close()                                # release connection each batch
    done += len(rows)
    print(f"  embedded {done}/{total}", flush=True)

c = connect()
remaining = c.execute(f"SELECT count(*) FROM paragraphs_v2 WHERE {where}", params).fetchone()[0]
print(f"remaining NULL embeddings: {remaining}")
c.close()
