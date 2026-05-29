import os
from dotenv import load_dotenv
from openai import OpenAI
import chromadb

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

chroma_client = chromadb.PersistentClient(path="./chroma_paragraph_db")
collection = chroma_client.get_collection("cjeu_paragraphs")

question = input("Legal question: ")

embedding_response = client.embeddings.create(
    model="text-embedding-3-small",
    input=[question]
)

query_embedding = embedding_response.data[0].embedding

results = collection.query(
    query_embeddings=[query_embedding],
    n_results=8
)

context_blocks = []

for i in range(len(results["documents"][0])):
    doc = results["documents"][0][i]
    meta = results["metadatas"][0][i]

    context_blocks.append(
        f"[Source {i+1}] CELEX: {meta['celex']}, paragraph: {meta['paragraph_number']}\n{doc}"
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
for i in range(len(results["documents"][0])):
    meta = results["metadatas"][0][i]
    print(f"- Source {i+1}: CELEX {meta['celex']}, para. {meta['paragraph_number']}")
