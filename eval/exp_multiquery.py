"""
EXPERIMENT (not wired into the app): does multi-query expansion fix the
non-procurement retrieval failures (gold 004/009/010, case recall 0.00)?

Hypothesis: those questions fail because the natural-language phrasing doesn't
match the Court's legalese. Fix: ask an LLM to produce N legal-term-rich
reformulations, retrieve each, and fuse with RRF across all sub-queries +
the original. Compare CASE recall@k to the single-query baseline.

Read-only DB; makes OpenAI calls. Run: ./.venv/bin/python eval/exp_multiquery.py
"""

import os
import sys
import json
import glob

sys.path.insert(0, os.path.dirname(__file__))
from run_eval import (  # reuse exact baseline building blocks
    client, conninfo, EMBED_MODEL, FTS_CONFIG, K_RRF, HNSW_EF_SEARCH,
    CANDIDATE_LIMIT, score_case,
)
import psycopg
from pgvector.psycopg import register_vector

REWRITE_MODEL = "gpt-4.1-mini"
N_VARIANTS = 3
K = 8


def expand(question):
    """Return [original] + N legal reformulations."""
    prompt = (
        "You are an EU-law search expert. Rewrite the user's question into "
        f"{N_VARIANTS} alternative search queries that a CJEU judgment would match, "
        "using the Court's terminology, doctrine names, and key legal phrases "
        "(e.g. 'disapply national law', 'direct effect', 'of its own motion', "
        "'primacy of EU law'). Vary the angle. Return ONLY a JSON array of "
        f"{N_VARIANTS} strings.\nQuestion: {question}"
    )
    try:
        r = client.responses.create(model=REWRITE_MODEL, input=prompt)
        txt = r.output_text.strip()
        if txt.startswith("```"):                      # strip ```json ... ``` fences
            txt = txt.split("```")[1].lstrip("json").strip()
        start, end = txt.find("["), txt.rfind("]")     # extract the array span
        if start != -1 and end != -1:
            txt = txt[start:end + 1]
        variants = json.loads(txt)
        return [question] + [v for v in variants if isinstance(v, str)][:N_VARIANTS]
    except Exception as e:
        print(f"  [expand failed: {e}] raw={r.output_text[:80]!r}")
        return [question]


def retrieve_one(cur, question):
    """Vector + keyword ranks for a single query (ids -> (vrank, krank))."""
    emb = client.embeddings.create(model=EMBED_MODEL, input=[question]).data[0].embedding
    ranks = {}
    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH};")
    cur.execute(
        """SELECT id,celex,paragraph_number FROM cjeu_paragraphs
           ORDER BY embedding <=> %s::vector LIMIT %s;""",
        (emb, CANDIDATE_LIMIT),
    )
    for r, (rid, celex, pn) in enumerate(cur.fetchall(), 1):
        ranks[rid] = {"celex": celex, "pn": pn, "vr": r, "kr": None}
    cur.execute(
        """SELECT id,celex,paragraph_number FROM cjeu_paragraphs
           WHERE search_vector @@ websearch_to_tsquery(%s::regconfig,%s)
           ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery(%s::regconfig,%s)) DESC
           LIMIT %s;""",
        (FTS_CONFIG, question, FTS_CONFIG, question, CANDIDATE_LIMIT),
    )
    for r, (rid, celex, pn) in enumerate(cur.fetchall(), 1):
        if rid in ranks:
            ranks[rid]["kr"] = r
        else:
            ranks[rid] = {"celex": celex, "pn": pn, "vr": None, "kr": r}
    return ranks


def multiquery_retrieve(cur, queries):
    """RRF-fuse vector+keyword ranks across ALL sub-queries into one ordering."""
    fused = {}
    for q in queries:
        for rid, d in retrieve_one(cur, q).items():
            s = (1/(K_RRF+d["vr"]) if d["vr"] else 0) + (1/(K_RRF+d["kr"]) if d["kr"] else 0)
            if rid not in fused:
                fused[rid] = {"celex": d["celex"], "pn": d["pn"], "score": 0.0}
            fused[rid]["score"] += s
    return sorted(fused.values(), key=lambda x: x["score"], reverse=True)


def main():
    conn = psycopg.connect(conninfo(), prepare_threshold=None)
    register_vector(conn)
    cur = conn.cursor()
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "gold", "*.json")))

    base_rec, mq_rec = [], []
    print(f"Multi-query expansion ({N_VARIANTS} variants) vs baseline | CASE recall@{K}\n")
    for f in files:
        g = json.load(open(f))
        if g.get("corpus_gap"):
            continue
        gold = [e["celex"] for e in g["expected"]]

        base = retrieve_one  # single query, baseline RRF
        base_ranked = multiquery_retrieve(cur, [g["question"]])   # 1 query == baseline
        b_rec, *_ = score_case([c["celex"] for c in base_ranked], gold, K)

        queries = expand(g["question"])
        mq_ranked = multiquery_retrieve(cur, queries)
        m_rec, *_ = score_case([c["celex"] for c in mq_ranked], gold, K)

        base_rec.append(b_rec); mq_rec.append(m_rec)
        flag = "  <== improved" if m_rec > b_rec else ("  <== REGRESSED" if m_rec < b_rec else "")
        print(f"[{g['id']:48s}] baseline={b_rec:.2f}  multiquery={m_rec:.2f}{flag}")

    n = len(base_rec)
    print(f"\n=== AGGREGATE (n={n}) ===")
    print(f"  baseline   mean CASE recall@{K}: {sum(base_rec)/n:.3f}")
    print(f"  multiquery mean CASE recall@{K}: {sum(mq_rec)/n:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
