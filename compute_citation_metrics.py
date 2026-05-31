"""
Phase 3 — citation-graph metrics.

Loads citation_edges into a networkx directed graph (edge A -> B means
"decision A cites decision B"), computes authority/centrality metrics offline,
and writes them to decision_metrics (one row per decision in the graph):

  * in_degree   — number of decisions that cite this one (citation count)
  * out_degree  — number of decisions this one cites
  * pagerank    — global authority (weighted by mention_count); cited-heavy,
                  cited-by-authoritative decisions score high
  * authority   — HITS authority score
  * hub         — HITS hub score
  * community_id — doctrinal cluster (Louvain community on the undirected graph)

decision_metrics is joined at query time by app.py; it never widens the hot
cjeu_paragraphs table. Re-runnable: the table is truncated and rebuilt each run.

Usage:
    python compute_citation_metrics.py --dry-run   # compute + print top-authority, no write
    python compute_citation_metrics.py             # compute + write decision_metrics
"""

import os
import sys

from extract_citations import _build_conninfo   # reuse the env-based resolver


def main():
    from dotenv import load_dotenv
    import psycopg
    import networkx as nx
    load_dotenv()

    dry_run = "--dry-run" in sys.argv
    conninfo, target = _build_conninfo()
    print(f"Target database: {target}" + ("  (DRY RUN — no writes)" if dry_run else ""))

    # --- read edges (single connection, then close) --------------------------
    conn = psycopg.connect(conninfo, prepare_threshold=None)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT citing_celex, cited_celex, mention_count FROM citation_edges;")
    edges = cur.fetchall()
    conn.close()
    print(f"edges loaded: {len(edges)}")

    G = nx.DiGraph()
    for citing, cited, w in edges:
        G.add_edge(citing, cited, weight=int(w or 1))
    print(f"graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    # --- metrics -------------------------------------------------------------
    print("computing pagerank...", flush=True)
    pagerank = nx.pagerank(G, weight="weight")

    print("computing HITS authority/hub...", flush=True)
    try:
        hubs, authority = nx.hits(G, max_iter=1000, normalized=True)
    except Exception as e:
        print(f"  HITS did not converge ({e}); storing zeros.")
        hubs = {n: 0.0 for n in G}
        authority = {n: 0.0 for n in G}

    print("detecting communities (Louvain, undirected)...", flush=True)
    UG = G.to_undirected()
    try:
        communities = nx.community.louvain_communities(UG, weight="weight", seed=42)
    except Exception as e:
        print(f"  community detection failed ({e}); leaving community_id NULL.")
        communities = []
    community_of = {}
    for cid, members in enumerate(communities):
        for n in members:
            community_of[n] = cid

    rows = [
        (
            n, G.in_degree(n), G.out_degree(n),
            float(pagerank.get(n, 0.0)),
            float(authority.get(n, 0.0)),
            float(hubs.get(n, 0.0)),
            community_of.get(n),
        )
        for n in G.nodes()
    ]

    # --- report ---------------------------------------------------------------
    top = sorted(pagerank.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print("\n=== Top 10 by PageRank (highest authority) ===")
    for celex, pr in top:
        print(f"  {celex}  pagerank={pr:.5f}  in_degree={G.in_degree(celex)}")
    print(f"\ncommunities: {len(communities)}  |  decisions scored: {len(rows)}")

    if dry_run:
        print("\nDRY RUN — decision_metrics not written.")
        return

    # --- write (fresh connection, after compute) -----------------------------
    print(f"\nWriting {len(rows)} decision_metrics rows...", flush=True)
    wconn = psycopg.connect(conninfo, prepare_threshold=None)
    wcur = wconn.cursor()
    wcur.execute("TRUNCATE decision_metrics;")
    insert_sql = (
        "INSERT INTO decision_metrics "
        "(celex, in_degree, out_degree, pagerank, authority, hub, community_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)"
    )
    WB = 1000
    for i in range(0, len(rows), WB):
        wcur.executemany(insert_sql, rows[i:i + WB])
        wconn.commit()
    wcur.execute("SELECT count(*) FROM decision_metrics;")
    print(f"decision_metrics rows: {wcur.fetchone()[0]}")
    wcur.close()
    wconn.close()


if __name__ == "__main__":
    main()
