"""
Phase 1 citation extractor — parses CJEU judgment paragraph text in
cjeu_paragraphs, resolves each "Case .../..." reference to a CELEX (verified
against the corpus, using the cjeu_celex_idx index), detects the citation type
from the Court's stereotyped phrasing, and writes:

  * citation_mentions  — one row per detected occurrence (paragraph-precise)
  * citation_edges     — deduplicated decision -> decision edges (resolved only)

This is the text-parsed source. The CELLAR CDM metadata source (more reliable
cited-case identity, no paragraph precision) is a later phase.

The parsing/typing/CELEX-construction logic is pure (no DB) and lives at module
top so it can be unit-tested without a database — see test_extract_citations.py.
DB libraries are imported lazily inside main().

Usage:
    python extract_citations.py --dry-run            # parse + report stats, write nothing
    python extract_citations.py --dry-run --limit 5000
    python extract_citations.py                      # full rebuild of text mentions + edges
"""

import os
import re
import sys

# --------------------------------------------------------------------------- #
# Pure parsing logic (no DB) — unit-tested in test_extract_citations.py
# --------------------------------------------------------------------------- #

# A single case-number token, with optional registry letter:
#   C- Court of Justice · T- General Court · F- Civil Service Tribunal · P- appeal
_CASE_NUM = r'(?:C-|T-|F-|P-)?\d{1,4}/\d{2,4}'

# Full citation construct: the literal word Case/Cases/Joined Cases (this is what
# disambiguates a case number from "Directive 71/305" / "Regulation No 1408/71")
# followed by one or more case-number tokens joined by ',', 'and', or 'to'.
CITATION_RE = re.compile(
    r'(?P<kw>Joined Cases|Cases|Case)\s+'
    r'(?P<nums>' + _CASE_NUM + r'(?:\s*(?:,|and|to)\s*' + _CASE_NUM + r')*)'
)
TOKEN_RE = re.compile(r'(?P<reg>C-|T-|F-|P-)?(?P<num>\d{1,4})/(?P<yr>\d{2,4})')
PARA_RE = re.compile(r'\bparagraphs?\s+(\d+)', re.IGNORECASE)

# Relation cues, checked most-specific first. Each entry: (relation_type, [phrases]).
_RELATION_CUES = [
    ("distinguishing", [
        "unlike in", "in contrast", "must be distinguished", "cannot be transposed",
        "differs from", "distinguished from",
    ]),
    ("by_analogy", [
        "by analogy", "mutatis mutandis", "applied by analogy",
    ]),
    ("following", [
        "settled case-law", "settled case law", "consistently held",
        "has already held", "has also held", "the court has held",
        "as the court held", "as the court ruled", "the court ruled",
        "in accordance with the judgment", "it is settled", "it is also settled",
        "the court has consistently", "as the court has held",
    ]),
    ("see", [
        "see, in particular", "see to that effect", "see also", "(see", "see ",
    ]),
]


def candidate_celex(reg, num, yr):
    """Construct the candidate CELEX for a parsed case number (judgment assumed)."""
    yr = int(yr)
    if yr < 100:
        # 2-digit year: Court founded 1953, so 53-99 => 19xx, 00-52 => 20xx.
        year = 1900 + yr if yr >= 53 else 2000 + yr
    else:
        year = yr
    descriptor = "TJ" if reg == "T-" else ("FJ" if reg == "F-" else "CJ")
    return f"6{year:04d}{descriptor}{int(num):04d}"


def detect_relation_type(text, kw_start):
    """Classify the citation from cues in a window around the 'Case' keyword."""
    window = text[max(0, kw_start - 90): kw_start + 120].lower()
    for relation, phrases in _RELATION_CUES:
        for phrase in phrases:
            if phrase in window:
                return relation, phrase.strip()
    return "cites", None


def parse_citations(text):
    """
    Parse all case citations in a paragraph. Returns a list of dicts with:
      candidate_celex, cited_paragraph_number, relation_type, signal_phrase,
      raw_reference. No DB access — cited_celex resolution happens in main().
    """
    out = []
    for m in CITATION_RE.finditer(text):
        relation, signal = detect_relation_type(text, m.start())
        base = m.start("nums")
        tokens = list(TOKEN_RE.finditer(m.group("nums")))
        for i, tok in enumerate(tokens):
            celex = candidate_celex(tok.group("reg"), tok.group("num"), tok.group("yr"))
            tok_abs_end = base + tok.end()
            seg_end = (base + tokens[i + 1].start()) if i + 1 < len(tokens) \
                else min(len(text), tok_abs_end + 160)
            segment = text[tok_abs_end:seg_end]
            pm = PARA_RE.search(segment)
            out.append({
                "candidate_celex": celex,
                "cited_paragraph_number": int(pm.group(1)) if pm else None,
                "relation_type": relation,
                "signal_phrase": signal,
                "raw_reference": text[m.start():seg_end].strip()[:300],
            })
    return out


def score_confidence(resolved, has_pinpoint, typed):
    c = 0.5
    if resolved:
        c += 0.3
    if has_pinpoint:
        c += 0.1
    if typed:
        c += 0.1
    return round(min(c, 1.0), 2)


# --------------------------------------------------------------------------- #
# DB driver (lazy imports so the pure logic above stays importable without psycopg)
# --------------------------------------------------------------------------- #

def _build_conninfo():
    url = os.getenv("DATABASE_URL")
    if url:
        return url, "DATABASE_URL"
    host = os.getenv("SUPABASE_HOST")
    if host and os.getenv("SUPABASE_DBNAME") and os.getenv("SUPABASE_USER"):
        return (
            f"host={host} port={os.getenv('SUPABASE_PORT')} "
            f"dbname={os.getenv('SUPABASE_DBNAME')} user={os.getenv('SUPABASE_USER')} "
            f"password={os.getenv('SUPABASE_PASSWORD')} sslmode=require"
        ), host
    raise SystemExit("No DB connection configured. Set DATABASE_URL or SUPABASE_*.")


def main():
    from dotenv import load_dotenv
    import psycopg
    load_dotenv()

    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    conninfo, target = _build_conninfo()
    print(f"Target database: {target}" + ("  (DRY RUN — no writes)" if dry_run else ""))

    conn = psycopg.connect(conninfo)
    read_cur = conn.cursor(name="paragraph_scan")   # server-side cursor for the big scan
    write_cur = conn.cursor()

    if not dry_run:
        write_cur.execute("DELETE FROM citation_mentions WHERE source = 'text';")
        conn.commit()
        print("Cleared previous text mentions (full rebuild).")

    resolved_cache = {}   # candidate_celex -> bool exists-in-corpus

    def resolve(celexes):
        unknown = [c for c in celexes if c not in resolved_cache]
        if unknown:
            write_cur.execute(
                "SELECT celex FROM cjeu_paragraphs WHERE celex = ANY(%s);", (unknown,)
            )
            present = {r[0] for r in write_cur.fetchall()}
            for c in unknown:
                resolved_cache[c] = c in present
        return resolved_cache

    sql = "SELECT celex, paragraph_number, text FROM cjeu_paragraphs"
    if limit:
        sql += f" LIMIT {int(limit)}"
    read_cur.execute(sql)

    stats = {"paragraphs": 0, "citations": 0, "resolved": 0, "unresolved": 0,
             "self": 0, "by_type": {}}
    pending = []
    BATCH = 500

    def flush(pending):
        if not pending:
            return
        cands = {p["candidate_celex"] for p in pending}
        cache = resolve(list(cands))
        for p in pending:
            celex = p["candidate_celex"]
            exists = cache.get(celex, False)
            if exists and celex == p["citing_celex"]:
                stats["self"] += 1
                continue
            cited = celex if exists else None
            if cited:
                stats["resolved"] += 1
            else:
                stats["unresolved"] += 1
            stats["by_type"][p["relation_type"]] = stats["by_type"].get(p["relation_type"], 0) + 1
            if not dry_run:
                write_cur.execute(
                    """
                    INSERT INTO citation_mentions (
                        citing_celex, citing_paragraph_number, cited_celex,
                        cited_paragraph_number, relation_type, signal_phrase,
                        raw_reference, source, confidence
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'text',%s);
                    """,
                    (
                        p["citing_celex"], p["citing_paragraph_number"], cited,
                        p["cited_paragraph_number"], p["relation_type"],
                        p["signal_phrase"], p["raw_reference"],
                        score_confidence(bool(cited), p["cited_paragraph_number"] is not None,
                                         p["relation_type"] != "cites"),
                    ),
                )
        if not dry_run:
            conn.commit()

    for celex, paragraph_number, text in read_cur:
        stats["paragraphs"] += 1
        if not text:
            continue
        for c in parse_citations(text):
            stats["citations"] += 1
            c["citing_celex"] = celex
            c["citing_paragraph_number"] = paragraph_number
            pending.append(c)
        if len(pending) >= BATCH:
            flush(pending)
            pending = []
        if stats["paragraphs"] % 50000 == 0:
            print(f"  scanned {stats['paragraphs']} paragraphs, "
                  f"{stats['resolved']} resolved / {stats['unresolved']} unresolved")
    flush(pending)

    if not dry_run:
        print("Rebuilding citation_edges from resolved mentions...")
        write_cur.execute("TRUNCATE citation_edges;")
        write_cur.execute(
            """
            INSERT INTO citation_edges (citing_celex, cited_celex, mention_count,
                                        dominant_relation_type, from_text)
            SELECT citing_celex, cited_celex, count(*) AS mention_count,
                   mode() WITHIN GROUP (ORDER BY relation_type) AS dominant_relation_type,
                   true
            FROM citation_mentions
            WHERE source = 'text' AND cited_celex IS NOT NULL
            GROUP BY citing_celex, cited_celex;
            """
        )
        conn.commit()
        write_cur.execute("SELECT count(*) FROM citation_edges;")
        print(f"citation_edges rows: {write_cur.fetchone()[0]}")

    print("\n=== Extraction stats ===")
    print(f"  paragraphs scanned : {stats['paragraphs']}")
    print(f"  citations found    : {stats['citations']}")
    print(f"  resolved           : {stats['resolved']}")
    print(f"  unresolved         : {stats['unresolved']}")
    print(f"  self-citations     : {stats['self']}")
    print(f"  by relation type   : {stats['by_type']}")
    if stats["citations"]:
        print(f"  resolution rate    : {stats['resolved'] / stats['citations']:.1%}")

    read_cur.close()
    write_cur.close()
    conn.close()


if __name__ == "__main__":
    main()
