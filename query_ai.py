import os
from dotenv import load_dotenv
from openai import OpenAI
import chromadb

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# connect to chroma
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_collection("cjeu_cases")

query = input("Question: ")

# embed query
response = client.embeddings.create(
    model="text-embedding-3-small",
    input=[query]
)

query_embedding = response.data[0].embedding

# semantic search
results = collection.query(
    query_embeddings=[query_embedding],
    n_results=5
)

print("\nTop Results:\n")

for i in range(len(results["documents"][0])):
    doc = results["documents"][0][i]
    meta = results["metadatas"][0][i]

    print("=" * 80)
    print(f"CELEX: {meta['celex']}")
    print(f"Chunk: {meta['chunk']}")
    print()
    print(doc[:1500])
    print()
