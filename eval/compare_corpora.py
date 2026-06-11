"""
Cutover measurement: run every gold question's hybrid retrieval against BOTH
corpora (current cjeu_paragraphs vs new paragraphs_v2) and report case-level and
paragraph-level recall side by side.

Now a FAIR comparison: v2 covers the full 1954-2026 corpus, so this measures
retrieval quality + coverage head-to-head (unlike the earlier 2018-only probe).

v2 retrieval: same hybrid (vector + FTS, RRF), filtered to section='grounds'
(the judgment holdings) so summary/operative don't dilute — the clean version of
the doc-kind demotion we prototyped on the old corpus.

Read-only. Run: ./.venv/bin/python eval/compare_corpora.py
"""
import os, sys, json, glob, math
sys.path.insert(0, os.path.dirname(__file__))
from run_eval import client, conninfo, EMBED_MODEL, FTS_CONFIG, K_RRF, HNSW_EF_SEARCH, score_case
import psycopg
from pgvector.psycopg import register_vector

CANDIDATE_LIMIT = 40
K = 8   # case recall @k


def retrieve(cur, question, table):
    emb = client.embeddings.create(model=EMBED_MODEL, input=[question]).data[0].embedding
    results = {}
    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH};")
    if table == "paragraphs_v2":
        # v2: restrict to grounds (the holdings) — clean section filter
        vec = ("SELECT id,celex,paragraph_number,1-(embedding <=> %s::vector) "
               "FROM paragraphs_v2 WHERE embedding IS NOT NULL AND section='grounds' "
               "ORDER BY embedding <=> %s::vector LIMIT %s")
        kw = ("SELECT id,celex,paragraph_number FROM paragraphs_v2 "
              "WHERE section='grounds' AND search_vector @@ websearch_to_tsquery(%s::regconfig,%s) "
              "ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery(%s::regconfig,%s)) DESC LIMIT %s")
    else:
        vec = ("SELECT id,celex,paragraph_number,1-(embedding <=> %s::vector) "
               "FROM cjeu_paragraphs WHERE embedding IS NOT NULL "
               "ORDER BY embedding <=> %s::vector LIMIT %s")
        kw = ("SELECT id,celex,paragraph_number FROM cjeu_paragraphs "
              "WHERE search_vector @@ websearch_to_tsquery(%s::regconfig,%s) "
              "ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery(%s::regconfig,%s)) DESC LIMIT %s")
    cur.execute(vec, (emb, emb, CANDIDATE_LIMIT))
    for rank, (rid, celex, pn, vs) in enumerate(cur.fetchall(), 1):
        results[rid] = {"celex": celex, "pn": pn, "vr": rank, "kr": None}
    cur.execute(kw, (FTS_CONFIG, question, FTS_CONFIG, question, CANDIDATE_LIMIT))
    for rank, (rid, celex, pn) in enumerate(cur.fetchall(), 1):
        if rid in results:
            results[rid]["kr"] = rank
        else:
            results[rid] = {"celex": celex, "pn": pn, "vr": None, "kr": rank}
    for it in results.values():
        it["h"] = (1/(K_RRF+it["vr"]) if it["vr"] else 0) + (1/(K_RRF+it["kr"]) if it["kr"] else 0)
    return sorted(results.values(), key=lambda x: x["h"], reverse=True)


def main():
    conn = psycopg.connect(conninfo(), prepare_threshold=None)
    register_vector(conn); cur = conn.cursor()
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "gold", "*.json")))
    agg = {"cur": {"rec": [], "para": []}, "v2": {"rec": [], "para": []}}

    print(f"{'gold':<42} {'CUR case/para':<16} {'V2 case/para':<16}")
    for f in files:
        g = json.load(open(f))
        gold_celex = [e["celex"] for e in g["expected"]]
        gold_pairs = {(e["celex"], p) for e in g["expected"] for p in e.get("paragraphs", [])}
        row = {}
        for key, table in [("cur", "cjeu_paragraphs"), ("v2", "paragraphs_v2")]:
            ranked = retrieve(cur, g["question"], table)
            rec, _, _, _ = score_case([c["celex"] for c in ranked], gold_celex, K)
            final = ranked[:K]
            pairs = {(c["celex"], c["pn"]) for c in final}
            para = (len(gold_pairs & pairs) / len(gold_pairs)) if gold_pairs else None
            agg[key]["rec"].append(rec)
            if para is not None:
                agg[key]["para"].append(para)
            row[key] = (rec, para)
        cur_s = f"{row['cur'][0]:.2f}/" + ("-" if row['cur'][1] is None else f"{row['cur'][1]:.2f}")
        v2_s = f"{row['v2'][0]:.2f}/" + ("-" if row['v2'][1] is None else f"{row['v2'][1]:.2f}")
        flag = " WIN" if row['v2'][0] > row['cur'][0] else ""
        print(f"  {g['id'][:40]:<40} {cur_s:<16} {v2_s:<16}{flag}")

    print("\n=== AGGREGATE (12 gold questions) ===")
    for key, label in [("cur", "CURRENT cjeu_paragraphs"), ("v2", "NEW paragraphs_v2")]:
        r = agg[key]["rec"]; p = agg[key]["para"]
        print(f"  {label:26s}: case_recall@{K}={sum(r)/len(r):.3f}  "
              f"para_recall={sum(p)/len(p):.3f} (n_para={len(p)})")
    conn.close()


if __name__ == "__main__":
    main()
