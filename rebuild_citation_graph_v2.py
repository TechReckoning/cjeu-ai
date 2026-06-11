"""
Rebuild citation_edges from the v2 corpus's authoritative CELLAR citations
(decisions_v2.cites), then recompute decision_metrics.

The old graph was text-parsed from cjeu_paragraphs (~56k edges). v2 captured the
authoritative CELLAR citation metadata during ingestion (decisions_v2.cites,
~169k entries, ~89k pointing to in-corpus decisions) — cleaner and more complete.

Keeps only edges whose BOTH endpoints are decisions in the corpus. Writes the new
edges into citation_edges via an atomic TRUNCATE+INSERT in one transaction so the
live app never sees an empty graph mid-rebuild. decision_metrics is recomputed
afterwards by compute_citation_metrics.py (run separately or via --metrics).

Usage:
    python rebuild_citation_graph_v2.py --dry-run    # report edge counts, no writes
    python rebuild_citation_graph_v2.py              # rebuild citation_edges
    python rebuild_citation_graph_v2.py --metrics    # rebuild edges THEN recompute metrics
"""
import os
import sys
from dotenv import load_dotenv
import psycopg

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv()

DRY = "--dry-run" in sys.argv
WITH_METRICS = "--metrics" in sys.argv


def main():
    conn = psycopg.connect(os.getenv("DATABASE_URL"), prepare_threshold=None, connect_timeout=20)
    conn.autocommit = False
    cur = conn.cursor()

    # Build the in-corpus authoritative edges from decisions_v2.cites.
    # Deduplicated decision->decision; from_cellar=true (authoritative source).
    build_select = """
        SELECT x.citing AS citing_celex, x.cited AS cited_celex
        FROM (
            SELECT d.celex AS citing, unnest(d.cites) AS cited
            FROM decisions_v2 d
            WHERE d.cites IS NOT NULL
        ) x
        WHERE x.cited <> x.citing
          AND x.cited IN (SELECT celex FROM decisions_v2)
        GROUP BY x.citing, x.cited
    """
    cur.execute(f"SELECT count(*) FROM ({build_select}) e")
    n_edges = cur.fetchone()[0]
    cur.execute(f"SELECT count(DISTINCT cited_celex) FROM ({build_select}) e")
    n_cited = cur.fetchone()[0]
    print(f"v2 authoritative edges (both endpoints in corpus): {n_edges}")
    print(f"distinct cited decisions: {n_cited}")
    cur.execute("SELECT count(*) FROM citation_edges")
    print(f"current citation_edges (old graph): {cur.fetchone()[0]}")

    if DRY:
        print("DRY RUN — citation_edges not modified.")
        # show a few top-cited for sanity
        cur.execute(f"""
            SELECT cited_celex, count(*) AS indeg FROM ({build_select}) e
            GROUP BY cited_celex ORDER BY indeg DESC LIMIT 8
        """)
        print("top cited (in-degree):")
        for celex, indeg in cur.fetchall():
            print(f"  {celex}: {indeg}")
        conn.rollback()
        conn.close()
        return

    # Atomic swap: truncate + repopulate in one transaction.
    print("Rebuilding citation_edges (atomic)...", flush=True)
    cur.execute("TRUNCATE citation_edges")
    cur.execute(f"""
        INSERT INTO citation_edges
            (citing_celex, cited_celex, mention_count, dominant_relation_type,
             from_text, from_cellar)
        SELECT citing_celex, cited_celex, 1, 'cites', false, true
        FROM ({build_select}) e
    """)
    cur.execute("SELECT count(*) FROM citation_edges")
    print(f"citation_edges now: {cur.fetchone()[0]}")
    conn.commit()
    conn.close()
    print("Committed.")

    if WITH_METRICS:
        print("\nRecomputing decision_metrics...", flush=True)
        import subprocess
        venv_py = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python")
        subprocess.run([venv_py, "compute_citation_metrics.py"], check=True)


if __name__ == "__main__":
    main()
