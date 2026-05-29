import json
import os
import time
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError
import chromadb

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection("cjeu_cases")

source_file = os.path.expanduser(
    "~/.cjeu-py/data/raw/texts/gc_texts.jsonl"
)

documents = []
metadatas = []
ids = []

with open(source_file, "r") as f:
    for line in f:
        item = json.loads(line)

        celex = item.get("celex", "unknown")
        text = item.get("text", "")

        if not text:
            continue

        chunks = text.split("\n\n")

        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()

            if len(chunk) < 100:
                continue

            documents.append(chunk)
            metadatas.append({
                "celex": celex,
                "chunk": i
            })
            ids.append(f"{celex}_{i}")

print(f"Prepared {len(documents)} chunks")

batch_size = 300

for start in range(0, len(documents), batch_size):
    end = min(start + batch_size, len(documents))

    batch_docs = documents[start:end]
    batch_meta = metadatas[start:end]
    batch_ids = ids[start:end]

    print(f"Embedding batch {start}–{end}...")

    max_retries = 5

    for attempt in range(max_retries):
        try:
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=batch_docs
            )

            embeddings = [d.embedding for d in response.data]

            collection.upsert(
                documents=batch_docs,
                embeddings=embeddings,
                metadatas=batch_meta,
                ids=batch_ids
            )

            break

        except RateLimitError:
            wait_time = 2 * (attempt + 1)
            print(f"Rate limit hit. Waiting {wait_time} seconds...")
            time.sleep(wait_time)

    time.sleep(0.3)

print("Index built successfully.")
