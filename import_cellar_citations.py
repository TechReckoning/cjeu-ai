"""
Phase 2 — import the authoritative CELLAR citation skeleton.

Reads the CELLAR CDM citation metadata (cjeu-py's gc_citations.parquet:
citing_celex -> cited_celex), keeps only edges whose BOTH endpoints are CJ
decisions in the corpus, and writes them to citation_mentions as source='cellar'
(decision-level: no paragraph, no relation type). Then rebuilds citation_edges
from ALL mentions so each edge carries from_text / from_cellar flags — edges
confirmed by both sources are the most trustworthy.

Idempotent: deletes prior source='cellar' mentions first; the text mentions
(source='text') are untouched, so re-running extract_citations and this script in
any order converges to the same merged edge set.

Usage:
    python import_cellar_citations.py --dry-run   # report coverage, write nothing
    python import_cellar_citations.py             # write cellar mentions + rebuild edges
"""

import os
import sys


def main():
    from dotenv import load_dotenv
    import psycopg
    import pandas as pd
    from extract_citations import _build_conninfo, rebuild_citation_edges
    load_dotenv()

    dry_run = "--dry-run" in sys.argv
    path = os.path.expanduser(os.getenv(
        "CELLAR_CITATIONS_FILE", "~/.cjeu-py/data/raw/cellar/gc_citations.parquet"
    ))
    if not os.path.exists(path):
        raise SystemExit(f"CELLAR citations file not found: {path}")

    conninfo, target = _build_conninfo()
    print(f"Target database: {target}" + ("  (DRY RUN — no writes)" if dry_run else ""))

    df = pd.read_parquet(path, columns=["citing_celex", "cited_celex"])
    print(f"CELLAR rows: {len(df)}")

    conn = psycopg.connect(conninfo, prepare_threshold=None)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT celex FROM cjeu_paragraphs;")
    known = {r[0] for r in cur.fetchall()}

    df = df[
        df.citing_celex.isin(known)
        & df.cited_celex.isin(known)
        & (df.citing_celex != df.cited_celex)
    ].drop_duplicates()
    edges = [tuple(x) for x in df[["citing_celex", "cited_celex"]].to_numpy()]
    print(f"CELLAR edges with both endpoints in corpus: {len(edges)}")

    if dry_run:
        print("DRY RUN — citation_mentions/edges not modified.")
        conn.close()
        return

    cur.execute("DELETE FROM citation_mentions WHERE source = 'cellar';")
    insert_sql = (
        "INSERT INTO citation_mentions (citing_celex, citing_paragraph_number, "
        "cited_celex, cited_paragraph_number, relation_type, signal_phrase, "
        "raw_reference, source, confidence) "
        "VALUES (%s, NULL, %s, NULL, NULL, NULL, NULL, 'cellar', 0.9)"
    )
    print(f"Writing {len(edges)} CELLAR mentions...", flush=True)
    for i in range(0, len(edges), 1000):
        cur.executemany(insert_sql, edges[i:i + 1000])

    print("Rebuilding citation_edges (merging text + cellar)...", flush=True)
    rebuild_citation_edges(cur)

    cur.execute(
        "SELECT count(*), "
        "count(*) FILTER (WHERE from_cellar), "
        "count(*) FILTER (WHERE from_text AND from_cellar), "
        "count(*) FILTER (WHERE from_cellar AND NOT from_text) "
        "FROM citation_edges;"
    )
    total, from_cellar, confirmed, cellar_only = cur.fetchone()
    print("\n=== citation_edges after merge ===")
    print(f"  total edges            : {total}")
    print(f"  from_cellar            : {from_cellar}")
    print(f"  confirmed (text+cellar): {confirmed}")
    print(f"  cellar-only (new)      : {cellar_only}")
    conn.close()
    print("\nNext: re-run compute_citation_metrics.py to refresh decision_metrics.")


if __name__ == "__main__":
    main()
