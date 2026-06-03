"""
Corpus rebuild ingester (schema v2). See docs/schema_v2_design.md.

Per decision: fetch CELLAR metadata via SPARQL (codes/agents resolved to labels),
fetch EUR-Lex HTML and parse paragraphs era-aware (reliable number + section),
parse title/parties, embed paragraphs, write decisions_v2 + paragraphs_v2.

Judgments only (JUDG). Idempotent per CELEX. Per-year orchestration with a
validation gate. Builds alongside the live tables; touches nothing existing.

Resumable: fetched documents are cached on disk (CJEU_V2_CACHE), and decisions
already in decisions_v2 are skipped (unless --force), so an interrupted backfill
restarts cheaply without re-fetching or redoing work.

Usage:
    python ingest_v2.py --year 2026 --dry-run        # fetch+parse+validate, NO writes, NO embedding
    python ingest_v2.py --year 2026 --no-embed       # write rows, skip embeddings (cheap)
    python ingest_v2.py --year 2026                  # full: write + embed
    python ingest_v2.py --year 2026 --limit 20 --dry-run
    python ingest_v2.py --year 2026 --force          # re-ingest even if already present
    python ingest_v2.py --year 2026 --no-cache       # bypass the on-disk fetch cache
"""

import os
import re
import sys
import time
import json
import urllib.parse
import urllib.request
import urllib.error

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
XSD_STR = "^^<http://www.w3.org/2001/XMLSchema#string>"
EMBED_MODEL = "text-embedding-3-small"
FTS_CONFIG = "english"
HEADERS = {"User-Agent": "Amicus-research/1.0"}

# On-disk cache of fetched judgment documents, so re-runs don't re-hit the API
# (a 70-year backfill must be resumable and not re-fetch). One file per CELEX:
# the raw document, or an empty file meaning "fetched, no English document".
CACHE_DIR = os.path.expanduser(os.getenv("CJEU_V2_CACHE", "~/.cjeu-py/data/v2_html_cache"))
NO_CACHE = "--no-cache" in sys.argv


def _cache_path(celex):
    return os.path.join(CACHE_DIR, f"{celex}.html")


# --------------------------------------------------------------------------- #
# SPARQL
# --------------------------------------------------------------------------- #
def sparql(query, timeout=90):
    data = urllib.parse.urlencode({
        "query": query, "format": "application/sparql-results+json"
    }).encode()
    req = urllib.request.Request(SPARQL, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("results", {}).get("bindings", [])


def list_year_judgments(year):
    """All CELEXes of CJ judgments for a given case year.

    resource_legal_year is typed xsd:gYear (not string). We also constrain via the
    CELEX prefix 6{year}CJ for robustness (sector 6 = case-law, CJ = Court of Justice
    judgment), and require the JUDG resource-type.
    """
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    SELECT DISTINCT ?celex WHERE {{
      ?w cdm:resource_legal_id_celex ?celex .
      ?w cdm:resource_legal_year "{year}"^^<http://www.w3.org/2001/XMLSchema#gYear> .
      ?w cdm:work_has_resource-type <http://publications.europa.eu/resource/authority/resource-type/JUDG> .
      FILTER(STRSTARTS(STR(?celex), "6{year}CJ"))
    }} ORDER BY ?celex'''
    return [b["celex"]["value"] for b in sparql(q)]


def fetch_metadata(celex):
    """One query: scalar fields, resolved labels, names, citation CELEXes."""
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT ?field ?val WHERE {{
      ?w cdm:resource_legal_id_celex "{celex}"{XSD_STR} .
      {{ ?w cdm:case-law_ecli ?val BIND("ecli" AS ?field) }}
      UNION {{ ?w cdm:resource_legal_number_natural_celex ?val BIND("case_number" AS ?field) }}
      UNION {{ ?w cdm:resource_legal_year ?val BIND("case_year" AS ?field) }}
      UNION {{ ?w cdm:work_date_document ?val BIND("decision_date" AS ?field) }}
      UNION {{ ?w cdm:case-law_uses_procedure_language ?c.
               ?c skos:prefLabel ?val BIND("procedure_language" AS ?field) FILTER(lang(?val)="en") }}
      UNION {{ ?w cdm:case-law_has_type_procedure_concept_type_procedure ?c.
               ?c skos:prefLabel ?val BIND("procedure_type" AS ?field) FILTER(lang(?val)="en") }}
      UNION {{ ?w cdm:case-law_delivered_by_court-formation ?c.
               ?c skos:prefLabel ?val BIND("court_formation" AS ?field) FILTER(lang(?val)="en") }}
      UNION {{ ?w cdm:resource_legal_is_about_subject-matter ?c.
               ?c skos:prefLabel ?val BIND("subject_matter" AS ?field) FILTER(lang(?val)="en") }}
      UNION {{ ?w cdm:case-law_originates_in_country ?c.
               ?c skos:prefLabel ?val BIND("country_origin" AS ?field) FILTER(lang(?val)="en") }}
      UNION {{ ?w cdm:case-law_delivered_by_advocate-general ?a.
               ?a cdm:agent_name ?val BIND("advocate_general" AS ?field) }}
      UNION {{ ?w cdm:case-law_delivered_by_judge ?j.
               ?j cdm:agent_name ?val BIND("judge_rapporteur" AS ?field) }}
      UNION {{ ?w cdm:work_cites_work ?cw. ?cw cdm:resource_legal_id_celex ?val BIND("cites" AS ?field) }}
      UNION {{ ?w cdm:case-law_interpretes_resource_legal ?lr.
               ?lr cdm:resource_legal_id_celex ?val BIND("interprets" AS ?field) }}
      UNION {{ ?w cdm:cellar_id ?val BIND("cellar_uri" AS ?field) }}
    }}'''
    scalar, multi = {}, {"subject_matters": set(), "cites": set(), "interprets_legislation": set()}
    for b in sparql(q):
        f, v = b["field"]["value"], b["val"]["value"]
        if f == "subject_matter":
            multi["subject_matters"].add(v)
        elif f == "cites":
            if v != celex:
                multi["cites"].add(v)
        elif f == "interprets":
            multi["interprets_legislation"].add(v)
        else:
            scalar.setdefault(f, v)
    scalar["subject_matters"] = sorted(multi["subject_matters"])
    scalar["cites"] = sorted(multi["cites"])
    scalar["interprets_legislation"] = sorted(multi["interprets_legislation"])
    return scalar


# --------------------------------------------------------------------------- #
# EUR-Lex HTML — fetch + era-aware paragraph parse (promoted from prototype)
# --------------------------------------------------------------------------- #
def has_english_expression(celex):
    """True if an actual English document FILE exists (>=1 ENG item).

    The work-level ENG-expression flag is optimistic: ~6% of judgments have an
    ENG expression in the metadata but ZERO downloadable ENG items (no English
    text actually published — French-only/untranslated). Counting items is the
    accurate predictor — recoverable cases have >=1 item, genuine non-losses 0.
    """
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    ASK {{
      ?w cdm:resource_legal_id_celex "{celex}"{XSD_STR} .
      ?e cdm:expression_belongs_to_work ?w .
      ?e cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
      ?m cdm:manifestation_manifests_expression ?e .
      ?item cdm:item_belongs_to_manifestation ?m .
    }}'''
    data = urllib.parse.urlencode({"query": q, "format": "application/sparql-results+json"}).encode()
    req = urllib.request.Request(SPARQL, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("boolean", False)


def _fetch_celex(celex, accept, timeout):
    url = f"http://publications.europa.eu/resource/celex/{celex}"
    req = urllib.request.Request(
        url, headers={**HEADERS, "Accept": accept, "Accept-Language": "eng"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code in (404, 406):
            return ""
        raise


def fetch_html(celex, timeout=90):
    """Fetch the ENG judgment document from the Publications Office content-
    negotiation API (the machine path, not the throttled eur-lex.europa.eu UI).

    Primary format is structured XHTML; ~5% of judgments 404 for xhtml but serve
    as plain HTML, so fall back to Accept: text/html. The parser handles both
    layouts. Returns "" only when neither format yields a document.

    Cached on disk per CELEX (unless --no-cache): a cache hit avoids the network
    entirely, making re-runs free and the backfill resumable. An empty cache file
    records a confirmed "no English document".
    """
    path = _cache_path(celex)
    if not NO_CACHE and os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    doc = _fetch_celex(celex, "application/xhtml+xml", timeout)
    if not doc.strip():
        doc = _fetch_celex(celex, "text/html", timeout)   # HTML fallback
    if not NO_CACHE:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc


def fetch_title(celex, timeout=60):
    """Case title via SPARQL cdm:expression_title (ENG expression). Fields are
    '#'-separated: 'Judgment ... of <date>.#<PARTIES>.#<Request...>#Case C-x/yy.'
    Returns (title, parties)."""
    q = f'''PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    SELECT ?title WHERE {{
      ?w cdm:resource_legal_id_celex "{celex}"{XSD_STR} .
      ?e cdm:expression_belongs_to_work ?w .
      ?e cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
      ?e cdm:expression_title ?title .
    }} LIMIT 1'''
    rows = sparql(q, timeout=timeout)
    if not rows:
        return None, []
    raw = rows[0]["title"]["value"]
    title = raw.replace("#", " ").strip()
    parties = []
    fields = [f.strip() for f in raw.split("#") if f.strip()]
    if len(fields) >= 2:
        # second field is the party line ("SIA 'Oribalt Riga v Valsts ...")
        party_line = re.sub(r'\.$', '', fields[1])
        parts = re.split(r'\s+v\.?\s+', party_line)
        if len(parts) >= 2:
            parties = [p.strip(" ,.‘’'") for p in parts if p.strip()]
    return title, parties


_SECTION_MARKERS = [
    (re.compile(r'name="SM"|>\s*Summary\s*<', re.I), "summary"),
    (re.compile(r'name="MO"|>\s*Grounds\s*<', re.I), "grounds"),
    (re.compile(r'name="CO"|>\s*Operative part\s*<', re.I), "operative"),
]


def _strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _boundaries(html):
    out = []
    for rx, lab in _SECTION_MARKERS:
        for m in rx.finditer(html):
            out.append((m.start(), lab))
    return sorted(out)


def _section_at(pos, bnds):
    label, best = "grounds", -1
    for mp, lab in bnds:
        if mp <= pos and mp > best:
            best, label = mp, lab
    return label


# Paragraph anchors appear as id="pointN" (structured XHTML) or NAME="pointN"
# (the plain-HTML fallback layout), with paragraph text following the anchor.
_POINT_ANCHOR = re.compile(r'(?:id|name)="point(\d+)"', re.I)


def parse_paragraphs(html):
    """Return list of (paragraph_number, section, text). Era-aware."""
    bnds = _boundaries(html)
    paras = []
    if _POINT_ANCHOR.search(html):                        # modern: point anchors
        anchors = list(_POINT_ANCHOR.finditer(html))
        for i, m in enumerate(anchors):
            seg = html[m.end(): anchors[i+1].start() if i+1 < len(anchors) else len(html)]
            t = _strip(seg).lstrip(">").strip()
            if t:
                paras.append((int(m.group(1)), _section_at(m.start(), bnds), t))
    else:                                                  # old: inline numbers
        for m in re.finditer(r"<p[^>]*>\s*(\d{1,3})\s*\.?\s*(?=[A-Z(])(.*?)</p>", html, re.S):
            t = _strip(m.group(2))
            if len(t) >= 20:
                paras.append((int(m.group(1)), _section_at(m.start(), bnds), t))
    return paras


def parse_title(html):
    """Extract the case title from EUR-Lex HTML (<p id="title">) and derive
    parties. Title format: "Judgment of the Court (...) of <date>. <PARTIES>.
    <Request/Action...>." Parties are the sentence after the date, split on ' v '.
    """
    m = re.search(r'<p[^>]*id="title"[^>]*>(.*?)</p>', html, re.S)
    title = _strip(m.group(1)) if m else None
    parties = []
    if title:
        # drop the leading "Judgment of the Court (...) of <date>." preamble
        body = re.sub(r'^Judgment of the Court[^.]*\.\s*', '', title)
        # the party sentence is up to the next '. ' (before "Request"/"Action"/etc.)
        first = re.split(r'\.\s', body, 1)[0]
        parts = re.split(r'\s+v\.?\s+', first)
        if len(parts) >= 2:
            parties = [p.strip(" ,.‘’'") for p in parts if p.strip()]
    return title, parties


def classify_fetch(html, paras):
    if not html:
        return "error"            # 404/406/empty from the API
    if not paras:
        return "no_paragraphs"    # fetched a doc but parser found nothing (inspect)
    return "ok"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main():
    args = sys.argv
    dry_run = "--dry-run" in args
    no_embed = "--no-embed" in args or dry_run
    year = int(args[args.index("--year")+1]) if "--year" in args else None
    limit = int(args[args.index("--limit")+1]) if "--limit" in args else None
    if not year:
        raise SystemExit("Specify --year YYYY")

    print(f"=== ingest_v2 year={year} "
          f"{'DRY RUN (no writes, no embed)' if dry_run else ('no-embed' if no_embed else 'FULL')} ===",
          flush=True)

    force = "--force" in args
    celexes = list_year_judgments(year)
    if limit:
        celexes = celexes[:limit]
    print(f"JUDG decisions in {year}: {len(celexes)}", flush=True)

    # Resume: skip CELEXes already written to decisions_v2 (unless --force or a
    # dry-run). Makes an interrupted backfill resumable without redoing work.
    if not dry_run and not force:
        try:
            import psycopg as _pg
            from dotenv import load_dotenv as _ld
            _ld()
            _c = _pg.connect(os.getenv("DATABASE_URL"), prepare_threshold=None,
                             connect_timeout=20)
            done = {r[0] for r in _c.execute(
                "SELECT celex FROM decisions_v2 WHERE celex = ANY(%s)", (celexes,)
            ).fetchall()}
            _c.close()
            if done:
                celexes = [c for c in celexes if c not in done]
                print(f"  resume: {len(done)} already ingested, {len(celexes)} remaining",
                      flush=True)
        except Exception as e:
            print(f"  (resume check skipped: {e})", flush=True)

    stats = {"decisions": 0, "paragraphs": 0, "fetch_ok": 0, "fetch_bad": 0,
             "nonmonotonic": 0, "no_title": 0, "with_meta": 0}
    rows_dec, rows_par = [], []

    for i, celex in enumerate(celexes, 1):
        try:
            meta = fetch_metadata(celex)
        except Exception as e:
            print(f"  [{celex}] metadata error: {e}", flush=True)
            meta = {}
        if meta.get("ecli"):
            stats["with_meta"] += 1

        # Skip text fetch when no English text is published yet (recent cases).
        try:
            eng = has_english_expression(celex)
        except Exception:
            eng = True   # assume yes; fetch will reveal otherwise
        if not eng:
            stats["no_english_text"] = stats.get("no_english_text", 0) + 1
            rows_dec.append({"celex": celex, "meta": meta, "title": None,
                             "parties": [], "fetch_status": "no_english_text"})
            if i % 25 == 0:
                print(f"  processed {i}/{len(celexes)}  paras={stats['paragraphs']}", flush=True)
            time.sleep(0.2)
            continue

        try:
            html = fetch_html(celex)
        except Exception as e:
            print(f"  [{celex}] html error: {e}", flush=True)
            html = ""
        paras = parse_paragraphs(html)
        try:
            title, parties = fetch_title(celex)
        except Exception:
            title, parties = None, []
        fetch_status = classify_fetch(html, paras)
        stats["fetch_ok" if fetch_status == "ok" else "fetch_bad"] += 1
        if not title:
            stats["no_title"] += 1

        # within-section monotonic check (grounds)
        gnums = [n for n, s, _ in paras if s == "grounds"]
        if gnums != sorted(gnums):
            stats["nonmonotonic"] += 1

        stats["decisions"] += 1
        stats["paragraphs"] += len(paras)

        rows_dec.append({"celex": celex, "meta": meta, "title": title,
                         "parties": parties, "fetch_status": fetch_status})
        seq = 0
        for num, section, text in paras:
            seq += 1
            rows_par.append({"id": f"{celex}_{section}_{num}_{seq}", "celex": celex,
                             "paragraph_number": num, "section": section, "seq": seq,
                             "text": text})
        if i % 25 == 0:
            print(f"  processed {i}/{len(celexes)}  paras={stats['paragraphs']}", flush=True)
        time.sleep(0.2)   # be polite to the endpoints

    print("\n=== validation report ===", flush=True)
    for k, v in stats.items():
        print(f"  {k:14s}: {v}")
    print(f"  avg paragraphs/decision: {stats['paragraphs']/max(stats['decisions'],1):.1f}")

    if dry_run:
        # show a few sample parsed decisions
        print("\n=== sample parsed decisions ===")
        for r in rows_dec[:3]:
            m = r["meta"]
            print(f"  {r['celex']}  {r['title']}")
            print(f"     date={m.get('decision_date')} proc={m.get('procedure_type')} "
                  f"formation={m.get('court_formation')} country={m.get('country_origin')}")
            print(f"     AG={m.get('advocate_general')} rapporteur={m.get('judge_rapporteur')} "
                  f"cites={len(m.get('cites',[]))} subjects={m.get('subject_matters')}")
            print(f"     parties={r['parties']} fetch={r['fetch_status']}")
        print(f"\nDRY RUN — nothing written. {stats['decisions']} decisions, "
              f"{stats['paragraphs']} paragraphs parsed.")
        return

    # --- writes (only when not dry-run) ---
    write_db(rows_dec, rows_par, no_embed)


def write_db(rows_dec, rows_par, no_embed):
    from dotenv import load_dotenv
    import psycopg
    from pgvector.psycopg import register_vector
    load_dotenv()
    conn = psycopg.connect(os.getenv("DATABASE_URL"), prepare_threshold=None)
    register_vector(conn)
    cur = conn.cursor()

    print(f"\nWriting {len(rows_dec)} decisions, {len(rows_par)} paragraphs"
          f"{' (no embeddings)' if no_embed else ''}...", flush=True)
    for r in rows_dec:
        m = r["meta"]
        cur.execute("""
            INSERT INTO decisions_v2 (celex,ecli,cellar_uri,case_number,case_year,
                decision_date,doc_type,procedure_type,court_formation,subject_matters,
                advocate_general,judge_rapporteur,country_origin,procedure_language,
                title,parties,cites,interprets_legislation,source_fetch_status)
            VALUES (%s,%s,%s,%s,%s,%s,'JUDG',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (celex) DO UPDATE SET
                ecli=EXCLUDED.ecli, decision_date=EXCLUDED.decision_date,
                procedure_type=EXCLUDED.procedure_type, court_formation=EXCLUDED.court_formation,
                subject_matters=EXCLUDED.subject_matters, advocate_general=EXCLUDED.advocate_general,
                judge_rapporteur=EXCLUDED.judge_rapporteur, country_origin=EXCLUDED.country_origin,
                title=EXCLUDED.title, parties=EXCLUDED.parties, cites=EXCLUDED.cites,
                interprets_legislation=EXCLUDED.interprets_legislation,
                source_fetch_status=EXCLUDED.source_fetch_status
        """, (r["celex"], m.get("ecli"), m.get("cellar_uri"), m.get("case_number"),
              int(m["case_year"]) if m.get("case_year") else None, m.get("decision_date"),
              m.get("procedure_type"), m.get("court_formation"), m.get("subject_matters"),
              m.get("advocate_general"), m.get("judge_rapporteur"), m.get("country_origin"),
              m.get("procedure_language"), r["title"], r["parties"], m.get("cites"),
              m.get("interprets_legislation"), r["fetch_status"]))
        cur.execute("DELETE FROM paragraphs_v2 WHERE celex=%s", (r["celex"],))
    conn.commit()

    embeddings = {}
    if not no_embed:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        texts = [p["text"][:8000] for p in rows_par]
        for s in range(0, len(texts), 300):
            batch = texts[s:s+300]
            resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
            for j, d in enumerate(resp.data):
                embeddings[s+j] = d.embedding
            print(f"  embedded {min(s+300,len(texts))}/{len(texts)}", flush=True)

    # Batched executemany — one round-trip per ~500 rows instead of per row
    # (31k individual INSERTs over the pooler is network-bound and slow).
    insert_sql = """
        INSERT INTO paragraphs_v2 (id,celex,paragraph_number,section,seq,text,embedding,search_vector)
        VALUES (%s,%s,%s,%s,%s,%s,%s, to_tsvector(%s::regconfig, coalesce(%s,'')))
        ON CONFLICT (id) DO NOTHING
    """
    PB = 500
    params = [
        (p["id"], p["celex"], p["paragraph_number"], p["section"], p["seq"],
         p["text"], embeddings.get(idx), FTS_CONFIG, p["text"])
        for idx, p in enumerate(rows_par)
    ]
    for s in range(0, len(params), PB):
        cur.executemany(insert_sql, params[s:s + PB])
        conn.commit()
        if s % 5000 == 0:
            print(f"  wrote {min(s + PB, len(params))}/{len(params)} paragraphs", flush=True)
    cur.execute("SELECT count(*) FROM decisions_v2"); d = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM paragraphs_v2"); pg = cur.fetchone()[0]
    print(f"decisions_v2={d}  paragraphs_v2={pg}")
    conn.close()


if __name__ == "__main__":
    main()
