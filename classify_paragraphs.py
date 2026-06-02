"""
Populate paragraph_meta — classify every cjeu_paragraphs row by document kind,
derived from the CELEX suffix (_SUM/_RES/_INF) the cjeu-py pipeline assigns to
official Summary / Resolution / Information documents.

  judgment   — base CELEX, no suffix (the real judgment/order)
  summary    — CELEX ends _SUM (official Summary / headnote / catchwords)
  resolution — CELEX ends _RES
  info       — CELEX ends _INF

Side table only; cjeu_paragraphs is untouched. Re-runnable (full rebuild).
Pooler-safe single-connection keyset scan.

Usage:
    python classify_paragraphs.py --dry-run   # report counts, write nothing
    python classify_paragraphs.py             # rebuild paragraph_meta
"""

import os
import re
import sys

from extract_citations import _build_conninfo   # reuse env-based resolver

SUFFIX_KIND = {"_SUM": "summary", "_RES": "resolution", "_INF": "info"}
_SUFFIX_RE = re.compile(r"_(SUM|RES|INF)$")


def classify(celex):
    """Return (base_celex, doc_kind) for a CELEX (possibly suffixed)."""
    m = _SUFFIX_RE.search(celex)
    if m:
        return celex[: m.start()], SUFFIX_KIND["_" + m.group(1)]
    return celex, "judgment"


def main():
    from dotenv import load_dotenv
    import psycopg
    load_dotenv()

    dry_run = "--dry-run" in sys.argv
    conninfo, target = _build_conninfo()
    print(f"Target database: {target}" + ("  (DRY RUN — no writes)" if dry_run else ""))

    conn = psycopg.connect(conninfo, prepare_threshold=None)
    conn.autocommit = True
    cur = conn.cursor()

    CHUNK = 5000
    last_id = ""
    counts = {"judgment": 0, "summary": 0, "resolution": 0, "info": 0}
    rows = []
    total = 0
    while True:
        cur.execute(
            "SELECT id, celex FROM cjeu_paragraphs WHERE id > %s ORDER BY id LIMIT %s;",
            (last_id, CHUNK),
        )
        batch = cur.fetchall()
        if not batch:
            break
        for rid, celex in batch:
            base, kind = classify(celex)
            counts[kind] += 1
            rows.append((rid, base, kind))
            total += 1
        last_id = batch[-1][0]
        if len(batch) < CHUNK:
            break

    print(f"classified {total} paragraphs:")
    for k, v in counts.items():
        print(f"  {k:11s}: {v}")

    if dry_run:
        print("DRY RUN — paragraph_meta not modified.")
        conn.close()
        return

    print("Writing paragraph_meta...", flush=True)
    cur.execute("TRUNCATE paragraph_meta;")
    insert = "INSERT INTO paragraph_meta (id, base_celex, doc_kind) VALUES (%s,%s,%s)"
    for i in range(0, len(rows), 5000):
        cur.executemany(insert, rows[i:i + 5000])
        if i % 100000 == 0:
            print(f"  wrote {min(i + 5000, len(rows))}/{len(rows)}", flush=True)
    cur.execute("SELECT count(*) FROM paragraph_meta;")
    print(f"paragraph_meta rows: {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
