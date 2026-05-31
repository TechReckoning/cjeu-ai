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

# Hyphen class: ASCII plus the Unicode hyphens/minus the Court uses in case
# numbers (e.g. "C‑410/13" with a non-breaking hyphen U+2011).
_H = r'[-‐‑‒–−]'
_REG = r'(?:C|T|F|P)'   # C Court of Justice · T General Court · F Civil Service · P appeal

# A single case-number token, with optional registry letter.
_CASE_NUM = r'(?:' + _REG + _H + r')?\d{1,4}/\d{2,4}'

# OLD-style construct: the literal word Case/Cases/Joined Cases (disambiguates a
# bare number from "Directive 71/305" / "Regulation No 1408/71") + token list.
CITATION_RE = re.compile(
    r'(?P<kw>Joined Cases|Cases|Case)\s+'
    r'(?P<nums>' + _CASE_NUM + r'(?:\s*(?:,|and|to)\s*' + _CASE_NUM + r')*)'
)
# MODERN-style: a registry-LETTERED token ("C‑222/08") not necessarily preceded
# by "Case" — common post-2012 (ECR discontinued) where cites read
# "Party, C‑202/97, EU:C:2000:75, paragraph 51". The required letter prefix is
# what keeps Directive/Regulation numbers from matching.
MODERN_RE = re.compile(r'(?<![A-Za-z])(?P<reg>' + _REG + r')' + _H + r'(?P<num>\d{1,4})/(?P<yr>\d{2,4})')
TOKEN_RE = re.compile(r'(?:(?P<reg>' + _REG + r')' + _H + r')?(?P<num>\d{1,4})/(?P<yr>\d{2,4})')
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
    descriptor = "TJ" if reg == "T" else ("FJ" if reg == "F" else "CJ")
    return f"6{year:04d}{descriptor}{int(num):04d}"


def detect_relation_type(text, kw_start):
    """Classify the citation from cues in a window around the 'Case' keyword."""
    window = text[max(0, kw_start - 90): kw_start + 120].lower()
    for relation, phrases in _RELATION_CUES:
        for phrase in phrases:
            if phrase in window:
                return relation, phrase.strip()
    return "cites", None


def _pinpoint(text, start):
    pm = PARA_RE.search(text[start:min(len(text), start + 160)])
    return int(pm.group(1)) if pm else None


def parse_citations(text):
    """
    Parse all case citations in a paragraph. Returns a list of dicts with:
      candidate_celex, cited_paragraph_number, relation_type, signal_phrase,
      raw_reference. No DB access — cited_celex resolution happens in main().

    Two passes: OLD-style ("Case C-57/94 ... [1995] ECR ...") and MODERN-style
    (a registry-lettered token "C‑202/97" without a "Case" keyword). Modern tokens
    that fall inside an old-style match are skipped to avoid double-counting.
    """
    out = []
    covered = []   # char spans consumed by old-style matches

    for m in CITATION_RE.finditer(text):
        relation, signal = detect_relation_type(text, m.start())
        base = m.start("nums")
        tokens = list(TOKEN_RE.finditer(m.group("nums")))
        for i, tok in enumerate(tokens):
            celex = candidate_celex(tok.group("reg"), tok.group("num"), tok.group("yr"))
            tok_abs_end = base + tok.end()
            seg_end = (base + tokens[i + 1].start()) if i + 1 < len(tokens) \
                else min(len(text), tok_abs_end + 160)
            out.append({
                "candidate_celex": celex,
                "cited_paragraph_number": _pinpoint(text, tok_abs_end),
                "relation_type": relation,
                "signal_phrase": signal,
                "raw_reference": text[m.start():seg_end].strip()[:300],
            })
        covered.append((m.start(), m.end()))

    for tok in MODERN_RE.finditer(text):
        s = tok.start()
        if any(a <= s < b for a, b in covered):
            continue
        relation, signal = detect_relation_type(text, s)
        celex = candidate_celex(tok.group("reg"), tok.group("num"), tok.group("yr"))
        out.append({
            "candidate_celex": celex,
            "cited_paragraph_number": _pinpoint(text, tok.end()),
            "relation_type": relation,
            "signal_phrase": signal,
            "raw_reference": text[s:min(len(text), tok.end() + 160)].strip()[:300],
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

    # Connections to the Supabase pooler are used ONE AT A TIME and never
    # interleaved (two concurrent pooler sessions from one client hang). The scan
    # is single-connection, read-only, keyset-paginated; resolution is a pure
    # in-memory set lookup (no per-batch query); writes happen only afterwards on
    # a fresh connection. prepare_threshold=None avoids psycopg auto-preparing
    # repeated statements, which the pooler dislikes.
    CHUNK = 2000

    # --- preload known CELEXes for in-memory resolution ----------------------
    read_conn = psycopg.connect(conninfo, prepare_threshold=None)
    read_conn.autocommit = True
    read_cur = read_conn.cursor()
    print("Loading known CELEXes...", flush=True)
    read_cur.execute("SELECT DISTINCT celex FROM cjeu_paragraphs;")
    known = {r[0] for r in read_cur.fetchall()}
    print(f"  known decisions: {len(known)}", flush=True)

    # --- scan + parse + resolve, accumulating mention rows in memory ---------
    stats = {"paragraphs": 0, "citations": 0, "resolved": 0, "unresolved": 0,
             "self": 0, "by_type": {}}
    mentions = []   # tuples ready for INSERT (citing, citing_pn, cited, cited_pn,
                    #                          relation, signal, raw, confidence)
    last_id = ""
    remaining = limit
    while remaining is None or remaining > 0:
        n = CHUNK if remaining is None else min(CHUNK, remaining)
        read_cur.execute(
            "SELECT id, celex, paragraph_number, text FROM cjeu_paragraphs "
            "WHERE id > %s ORDER BY id LIMIT %s;",
            (last_id, n),
        )
        batch = read_cur.fetchall()
        if not batch:
            break
        for row_id, celex, paragraph_number, text in batch:
            stats["paragraphs"] += 1
            if not text:
                continue
            for c in parse_citations(text):
                stats["citations"] += 1
                cand = c["candidate_celex"]
                exists = cand in known
                if exists and cand == celex:
                    stats["self"] += 1
                    continue
                cited = cand if exists else None
                stats["resolved" if cited else "unresolved"] += 1
                stats["by_type"][c["relation_type"]] = stats["by_type"].get(c["relation_type"], 0) + 1
                mentions.append((
                    celex, paragraph_number, cited, c["cited_paragraph_number"],
                    c["relation_type"], c["signal_phrase"], c["raw_reference"],
                    score_confidence(bool(cited), c["cited_paragraph_number"] is not None,
                                     c["relation_type"] != "cites"),
                ))
        last_id = batch[-1][0]
        if remaining is not None:
            remaining -= len(batch)
        if stats["paragraphs"] % 50000 < CHUNK:
            print(f"  scanned {stats['paragraphs']} paragraphs, "
                  f"{stats['resolved']} resolved / {stats['unresolved']} unresolved",
                  flush=True)
        if len(batch) < n:
            break
    read_cur.close()
    read_conn.close()

    print("\n=== Extraction stats ===")
    print(f"  paragraphs scanned : {stats['paragraphs']}")
    print(f"  citations found    : {stats['citations']}")
    print(f"  resolved           : {stats['resolved']}")
    print(f"  unresolved         : {stats['unresolved']}")
    print(f"  self-citations     : {stats['self']}")
    print(f"  by relation type   : {stats['by_type']}")
    if stats["citations"]:
        print(f"  resolution rate    : {stats['resolved'] / stats['citations']:.1%}")

    if dry_run:
        print(f"\nDRY RUN — would write {len(mentions)} mention rows. Nothing written.")
        return

    # --- write phase: fresh connection, opened only after the scan is done ---
    print(f"\nWriting {len(mentions)} mentions...", flush=True)
    write_conn = psycopg.connect(conninfo, prepare_threshold=None)
    write_cur = write_conn.cursor()
    write_cur.execute("DELETE FROM citation_mentions WHERE source = 'text';")
    write_conn.commit()
    insert_sql = (
        "INSERT INTO citation_mentions ("
        "citing_celex, citing_paragraph_number, cited_celex, cited_paragraph_number, "
        "relation_type, signal_phrase, raw_reference, source, confidence) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'text',%s)"
    )
    WBATCH = 1000
    for i in range(0, len(mentions), WBATCH):
        write_cur.executemany(insert_sql, mentions[i:i + WBATCH])
        write_conn.commit()
        if i % 20000 == 0:
            print(f"  wrote {min(i + WBATCH, len(mentions))}/{len(mentions)}", flush=True)

    print("Rebuilding citation_edges...", flush=True)
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
    write_conn.commit()
    write_cur.execute("SELECT count(*) FROM citation_edges;")
    print(f"citation_edges rows: {write_cur.fetchone()[0]}")
    write_cur.close()
    write_conn.close()


if __name__ == "__main__":
    main()
