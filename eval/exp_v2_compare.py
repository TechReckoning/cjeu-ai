"""
EXPERIMENT: focused current-vs-v2 retrieval comparison.

v2 currently holds ONLY 2018, so a full gold-set recall comparison would be
unfair (v2 lacks most gold cases by construction). Instead we compare on the one
gold case present in BOTH corpora — Tim, 62018CJ0395 (gold para 34, question
007) — and ask the question that actually matters for the rebuild:

  When a query retrieves the right CASE, does it surface the right PARAGRAPH?

For each corpus we run vector retrieval for the q007 query, then report:
  - rank of the Tim case (first Tim paragraph)
  - whether gold paragraph 34 appears in the top-K paragraphs
  - which Tim paragraph ranks highest (and its number)

This isolates paragraph-level retrieval quality without the corpus-size penalty.
Read-only. Makes one embedding call.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from run_eval import client, conninfo, EMBED_MODEL, HNSW_EF_SEARCH
import psycopg
from pgvector.psycopg import register_vector

Q = ("Can Member States transpose the optional exclusion grounds in Article 57(4) "
     "of Directive 2014/24/EU as mandatory exclusion grounds in their national legislation?")
GOLD_CELEX = "62018CJ0395"
GOLD_PARA = 34
K = 40


def probe(cur, table, emb):
    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH};")
    cur.execute(
        f"""SELECT celex, paragraph_number, 1-(embedding <=> %s::vector) AS sim
            FROM {table} WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector LIMIT %s""",
        (emb, emb, K),
    )
    rows = cur.fetchall()
    case_rank = next((i+1 for i, r in enumerate(rows) if r[0] == GOLD_CELEX), None)
    tim_paras = [(r[1], r[2]) for r in rows if r[0] == GOLD_CELEX]
    gold_in_topk = any(pn == GOLD_PARA for pn, _ in tim_paras)
    return {"case_rank": case_rank, "tim_paras": tim_paras[:5],
            "gold34_in_topk": gold_in_topk, "n_tim": len(tim_paras)}


def main():
    emb = client.embeddings.create(model=EMBED_MODEL, input=[Q]).data[0].embedding
    conn = psycopg.connect(conninfo(), prepare_threshold=None)
    register_vector(conn)
    cur = conn.cursor()
    print(f"Query (q007): {Q}\nGold: {GOLD_CELEX} para {GOLD_PARA}\n")
    for table in ["cjeu_paragraphs", "paragraphs_v2"]:
        r = probe(cur, table, emb)
        print(f"=== {table} ===")
        print(f"  Tim case first appears at rank: {r['case_rank']}")
        print(f"  Tim paragraphs in top-{K} (num, sim): {r['tim_paras']}")
        print(f"  GOLD para {GOLD_PARA} in top-{K}: {r['gold34_in_topk']}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
