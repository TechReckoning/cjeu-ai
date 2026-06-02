"""
Retrieval evaluation harness for Amicus.

Runs each gold question in eval/gold/*.json through the SAME pipeline app.py uses
(hybrid retrieval -> RRF -> LLM rerank -> citation-authority tie-breaker) and
scores the final ranked results against the expert-provided gold cases/paragraphs.

Two granularities, per the design decision:
  * CASE level (primary) — did we retrieve the right decisions (CELEX)?
      Recall@k, MRR (rank of first gold case), nDCG@k.
  * PARAGRAPH level (secondary) — did we surface the exact gold paragraphs?
      precision/recall of (celex, paragraph_number) pairs in the final top-N.

Scores the FULL pipeline including rerank (matches what feeds the answer), so a
run makes OpenAI calls. Read-only against the DB.

Usage:
    ./.venv/bin/python eval/run_eval.py            # all gold files, final_limit=8
    ./.venv/bin/python eval/run_eval.py --k 10     # report case metrics @k=10
    ./.venv/bin/python eval/run_eval.py --no-rerank # retrieval-only (free, faster)
"""

import os
import sys
import json
import glob
import math

from dotenv import load_dotenv
from openai import OpenAI
import psycopg
from pgvector.psycopg import register_vector

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv()  # also try CWD
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

EMBED_MODEL = "text-embedding-3-small"
RERANK_MODEL = "gpt-4.1-mini"
FTS_CONFIG = "english"
K_RRF = 60
RERANK_POOL = 50
HNSW_EF_SEARCH = 100
CANDIDATE_LIMIT = 40
FINAL_LIMIT = 8

K = int(sys.argv[sys.argv.index("--k") + 1]) if "--k" in sys.argv else FINAL_LIMIT
USE_RERANK = "--no-rerank" not in sys.argv
DEMOTE = "--no-demote" not in sys.argv   # down-rank non-judgment paragraphs (default on)


def conninfo():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set (.env).")
    return url


def retrieve(cur, question):
    """Hybrid retrieval + RRF fusion. Returns candidates sorted by hybrid_score."""
    emb = client.embeddings.create(model=EMBED_MODEL, input=[question]).data[0].embedding
    results = {}
    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH};")
    cur.execute(
        """SELECT p.id,p.celex,p.paragraph_number,p.text,
                  1-(p.embedding <=> %s::vector),
                  coalesce(m.doc_kind,'judgment')
           FROM cjeu_paragraphs p
           LEFT JOIN paragraph_meta m ON m.id = p.id
           ORDER BY p.embedding <=> %s::vector LIMIT %s;""",
        (emb, emb, CANDIDATE_LIMIT),
    )
    for rank, (rid, celex, pn, text, vs, kind) in enumerate(cur.fetchall(), 1):
        results[rid] = {"celex": celex, "paragraph_number": pn, "text": text,
                        "vr": rank, "kr": None, "doc_kind": kind}
    cur.execute(
        """SELECT p.id,p.celex,p.paragraph_number,p.text, coalesce(m.doc_kind,'judgment')
           FROM cjeu_paragraphs p
           LEFT JOIN paragraph_meta m ON m.id = p.id
           WHERE p.search_vector @@ websearch_to_tsquery(%s::regconfig,%s)
           ORDER BY ts_rank_cd(p.search_vector,websearch_to_tsquery(%s::regconfig,%s)) DESC
           LIMIT %s;""",
        (FTS_CONFIG, question, FTS_CONFIG, question, CANDIDATE_LIMIT),
    )
    for rank, (rid, celex, pn, text, kind) in enumerate(cur.fetchall(), 1):
        if rid in results:
            results[rid]["kr"] = rank
        else:
            results[rid] = {"celex": celex, "paragraph_number": pn, "text": text,
                            "vr": None, "kr": rank, "doc_kind": kind}
    for it in results.values():
        vr, kr = it["vr"], it["kr"]
        it["hybrid"] = (1/(K_RRF+vr) if vr else 0) + (1/(K_RRF+kr) if kr else 0)
        it["is_judgment"] = 1 if it["doc_kind"] == "judgment" else 0
    if DEMOTE:
        # judgment paragraphs first, then by hybrid — keeps summaries retrievable
        # but stops their near-duplicate text outranking real holdings.
        cands = sorted(results.values(), key=lambda x: (x["is_judgment"], x["hybrid"]),
                       reverse=True)[:RERANK_POOL]
    else:
        cands = sorted(results.values(), key=lambda x: x["hybrid"], reverse=True)[:RERANK_POOL]
    return cands


def rerank(cur, question, cands):
    """LLM rerank + authority tie-breaker (mirrors app.py)."""
    blocks = [{"id": i, "celex": c["celex"], "paragraph_number": c["paragraph_number"],
               "text": c["text"][:2000]} for i, c in enumerate(cands, 1)]
    prompt = (
        "You are a legal relevance reranker for EU Court of Justice case-law.\n"
        f"Question: {question}\n"
        "Score each candidate paragraph 0-10 for legal relevance "
        "(10=directly answers, 0=irrelevant). Return ONLY JSON "
        '[{"id":1,"score":9}].\nCandidates:\n' + json.dumps(blocks, ensure_ascii=False)
    )
    sm = {}
    try:
        r = client.responses.create(model=RERANK_MODEL, input=prompt)
        sm = {s["id"]: float(s.get("score", 0)) for s in json.loads(r.output_text.strip())}
    except Exception as e:
        print("  [rerank failed, falling back to hybrid order]", e)
    celexes = list({c["celex"] for c in cands})
    cur.execute("SELECT celex,pagerank FROM decision_metrics WHERE celex = ANY(%s);", (celexes,))
    pr = {row[0]: float(row[1] or 0) for row in cur.fetchall()}
    for i, c in enumerate(cands, 1):
        c["rr"] = sm.get(i, 0.0)
        c["pr"] = pr.get(c["celex"], 0.0)
    cands.sort(key=lambda x: (x["rr"], x["pr"], x["hybrid"]), reverse=True)
    return cands


def dcg(rels):
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def score_case(ranked_celexes, gold_celexes, k):
    """Case-level metrics: recall@k, MRR (first gold), nDCG@k."""
    seen, dedup = set(), []
    for c in ranked_celexes:
        if c not in seen:
            seen.add(c); dedup.append(c)
    topk = dedup[:k]
    found = [c for c in gold_celexes if c in topk]
    recall = len(found) / len(gold_celexes)
    mrr = 0.0
    for i, c in enumerate(dedup, 1):
        if c in gold_celexes:
            mrr = 1 / i; break
    rels = [1 if c in gold_celexes else 0 for c in topk]
    ideal = sorted(rels, reverse=True)
    ndcg = (dcg(rels) / dcg(ideal)) if any(ideal) else 0.0
    return recall, mrr, ndcg, found


def main():
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "gold", "*.json")))
    if not files:
        raise SystemExit("No gold files in eval/gold/.")
    print(f"Eval: {len(files)} gold question(s) | "
          f"{'FULL pipeline (rerank+authority)' if USE_RERANK else 'retrieval-only'} | "
          f"case metrics @k={K}\n")

    conn = psycopg.connect(conninfo(), prepare_threshold=None)
    register_vector(conn)
    cur = conn.cursor()

    agg = {"recall": [], "mrr": [], "ndcg": [], "para_recall": []}
    for f in files:
        g = json.load(open(f))
        gap = bool(g.get("corpus_gap"))
        gold_celexes = [e["celex"] for e in g["expected"]]
        gold_paras = {(e["celex"], p) for e in g["expected"] for p in e.get("paragraphs", [])}

        cands = retrieve(cur, g["question"])
        ranked = rerank(cur, g["question"], cands) if USE_RERANK else cands
        final = ranked[:FINAL_LIMIT]

        recall, mrr, ndcg, found = score_case([c["celex"] for c in ranked], gold_celexes, K)
        final_pairs = {(c["celex"], c["paragraph_number"]) for c in final}
        para_hits = gold_paras & final_pairs
        para_recall = len(para_hits) / len(gold_paras) if gold_paras else 0.0

        # corpus_gap entries (controlling case absent from corpus) are reported
        # but EXCLUDED from the main aggregate so they don't unfairly penalise
        # retrieval for data we don't hold.
        if not gap:
            agg["recall"].append(recall); agg["mrr"].append(mrr)
            agg["ndcg"].append(ndcg); agg["para_recall"].append(para_recall)

        print(f"[{g['id']}]" + ("  (corpus_gap — excluded from aggregate)" if gap else ""))
        print(f"  Q: {g['question']}")
        print(f"  CASE @k={K}: recall={recall:.2f} ({len(found)}/{len(gold_celexes)}) "
              f"MRR={mrr:.3f} nDCG={ndcg:.3f}  found={found}")
        print(f"  PARA in final-{FINAL_LIMIT}: recall={para_recall:.2f} "
              f"({len(para_hits)}/{len(gold_paras)}) hits={sorted(para_hits)}")
        # show where each gold case landed
        dedup_order = []
        for c in ranked:
            if c["celex"] not in dedup_order:
                dedup_order.append(c["celex"])
        for e in g["expected"]:
            pos = dedup_order.index(e["celex"]) + 1 if e["celex"] in dedup_order else None
            print(f"    {e['celex']} {e['name'][:24]:24s} case_rank={pos}")
        print()

    n = len(agg["recall"])
    print(f"=== AGGREGATE ({n} questions; corpus_gap entries excluded) ===")
    if n:
        print(f"  mean CASE recall@{K} : {sum(agg['recall'])/n:.3f}")
        print(f"  mean MRR             : {sum(agg['mrr'])/n:.3f}")
        print(f"  mean nDCG@{K}         : {sum(agg['ndcg'])/n:.3f}")
        print(f"  mean PARA recall     : {sum(agg['para_recall'])/n:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
