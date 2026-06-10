# Amicus corpus rebuild — schema v2 design

**Status:** DRAFT for review. Nothing built yet. The current production tables
(`cjeu_paragraphs`, `amicus_queries`, citation graph, `paragraph_meta`) are
untouched and the live app keeps running throughout the rebuild.

## Why rebuild
Evidence gathered this session:
- **Paragraph numbering is corrupt in ~26.5% of judgments** (4,020 of 15,198),
  concentrated pre-2000 (83–90% in the 1950s–70s). Root cause is upstream
  `cjeu-py` HTML extraction, NOT the indexer — confirmed in the source JSONL
  (e.g. Simmenthal source `paragraph_nums = [1,2,3,4,1,2,3,4]`, only 8 paras).
- **Headnote/summary text is conflated with grounds**, so catchwords outrank
  holdings and `(CELEX, para N)` citations are ambiguous.
- **Coverage gaps**: whole areas missing (copyright — Cofemel/Infopaq/Levola
  absent), recent landmarks missing (Kolin C-652/22), `_SUM/_RES/_INF` pseudo-
  decisions mixed into the corpus.
- **Rich metadata is available but discarded** (`--skip-citations`,
  `--skip-subjects`), so no faceted search, no temporal/court signals.

Two prototypes proved the fix is tractable:
- Metadata: CELLAR SPARQL returns reliable, label-resolvable fields for ALL eras
  (verified on Alstom 2022 and Simmenthal 1978).
- Text: an era-aware EUR-Lex HTML parser recovers reliable paragraph numbers +
  section tags (Simmenthal went 0 → 27 grounds paragraphs, recovering the cited
  paras 15–16).

## Design principles
1. **Normalize**: one row per decision (metadata) + one row per paragraph (text),
   instead of repeating `celex`/`url` across 600k rows.
2. **Reliable paragraph identity**: `paragraph_number` comes from document markup
   (point anchors / inline numbers), tagged by `section`, never guessed.
3. **Metadata enables faceting + ranking**: date, court formation, procedure,
   subject, AG/judge, country — all queryable filters and ranking signals.
4. **Citations are first-class metadata** (`cdm:work_cites_work` +
   `interprets_legislation`), authoritative, not only text-parsed.
5. **Build alongside**: `*_v2` tables in the same Supabase; cut over only when
   measured (via the eval harness) to beat the current corpus.
6. **English-only stays** (settled): one English expression per decision.

---

## Tables

### `decisions_v2` — one row per CJEU decision
| column | type | source (CDM predicate) | notes |
|---|---|---|---|
| `celex` | text PK | `resource_legal_id_celex` | e.g. `62020CJ0532` |
| `ecli` | text | `case-law_ecli` | `ECLI:EU:C:2022:128` |
| `cellar_uri` | text | the Work URI | stable internal id |
| `case_number` | text | `resource_legal_number_natural_celex` | `532` |
| `case_year` | int | `resource_legal_year` | `2020` |
| `decision_date` | date | `work_date_document` | delivery date |
| `doc_type` | text | `work_has_resource-type` | JUDG / ORDER / OPIN_AG |
| `procedure_type` | text | `…type_procedure…` (label) | "Reference for a preliminary ruling" |
| `court_formation` | text | `…court-formation` (label) | "Ninth Chamber", "Full Court" |
| `subject_matters` | text[] | `…is_about_subject-matter` (labels) | e.g. {"Approximation of laws"} |
| `advocate_general` | text | `…delivered_by_advocate-general` | resolved name |
| `judge_rapporteur` | text | `…delivered_by_judge` | resolved name |
| `country_origin` | text | `case-law_originates_in_country` | ISO/label, prelim. refs |
| `procedure_language` | text | `case-law_uses_procedure_language` | authentic language |
| `title` | text | EUR-Lex case title | full title, e.g. "Alstom Transport SA v CFR" |
| `parties` | text[] | parsed from title | {"Alstom Transport SA","CFR"} |
| `cites` | text[] | `work_cites_work` (→ CELEX) | decision→work citations |
| `interprets_legislation` | text[] | `case-law_interpretes_resource_legal` | CELEX of legislation |
| `ingested_at` | timestamptz | — | provenance |
| `source_fetch_status` | text | — | ok / placeholder / fallback_used |

Indexes: PK(`celex`); btree(`decision_date`, `court_formation`, `procedure_type`,
`country_origin`, `case_year`); GIN(`subject_matters`, `cites`).

### `paragraphs_v2` — one row per paragraph
| column | type | notes |
|---|---|---|
| `id` | text PK | `{celex}_{section}_{paragraph_number}` (stable, meaningful) |
| `celex` | text FK → `decisions_v2.celex` | |
| `paragraph_number` | int | RELIABLE — from markup, within-section |
| `section` | text | summary / grounds / operative |
| `seq` | int | global order within the decision (stable tiebreak) |
| `text` | text | full paragraph (no 6000-char truncation cap) |
| `embedding` | vector(1536) | text-embedding-3-small |
| `search_vector` | tsvector | FTS (english) |

Indexes: PK(`id`); btree(`celex`); HNSW(`embedding vector_cosine_ops`);
GIN(`search_vector`).
Retrieval can now filter by `section` (e.g. prefer `grounds`, demote `summary`)
and JOIN `decisions_v2` for faceted/temporal ranking — both impossible today.

### Citation graph (reuse, repoint)
`citation_mentions` / `citation_edges` / `decision_metrics` keep their shape but
edges now also seed from `decisions_v2.cites` (authoritative CELLAR) merged with
text-parsed mentions — same two-source model, cleaner inputs.

---

## Ingestion pipeline (per decision, 2026 → 1954)
1. **Discover** target CELEXes by year (SPARQL: all `6{year}CJ*` works) — also
   closes coverage gaps (no `--skip`, no caps, include orders/opinions as chosen).
2. **Fetch metadata** (one SPARQL query per decision; codes resolved to EN labels).
3. **Fetch text** — EUR-Lex HTML; era-aware parse → (paragraph_number, section,
   text). Fallback for placeholder/empty pages: alt URI form, then `fmx4`
   manifestation. Record `source_fetch_status`.
4. **Embed** new paragraphs (batched), write `paragraphs_v2`.
5. **Write** `decisions_v2` row incl. `cites` / `interprets_legislation`.
6. After a batch: rebuild citation edges + recompute `decision_metrics`.
7. **Idempotent**: keyed on `celex` / paragraph `id`; re-runnable per year.

Order **2026 → 1954** (newest first): newest cases parse cleanest (point
anchors), deliver immediate value, and surface fetch-robustness issues on easy
cases before hitting the hard old ones.

## Cutover (measured, not assumed)
- Build `_v2` to parity coverage, then point the eval harness at `_v2` and
  compare against current `cjeu_paragraphs` on the gold set.
- Cut the app over only if `_v2` ≥ current on case recall AND paragraph recall
  (the latter should jump — that's the whole point).
- Keep old tables until cutover is confirmed; trivial rollback.

## Costs / risks (honest)
- **Re-embedding** ~600k+ paragraphs = real OpenAI spend (one-time). Estimate +
  confirm before running the full backfill.
- **SPARQL/EUR-Lex fetch volume**: ~14k decisions × (1 SPARQL + 1 HTML) — rate-
  limit-friendly pacing needed; resumable per year.
- **Fetch gaps** (Bosman placeholder) need the fallback chain; some very old
  cases may still resist and need flagging, not silent loss.
- **Name resolution** (AG/judge cellar URIs → names) needs a second lookup;
  cache per agent URI.

## Resolved decisions (2026-06-02)
1. **Doc types: Judgments only** (`doc_type = JUDG`). Orders and AG opinions are
   excluded for the rebuild — matches the core of the current corpus; revisit later.
2. **Sections: keep all, tagged.** `paragraphs_v2` stores summary / grounds /
   operative, each tagged via `section`; retrieval filters/ranks by section
   (this is the clean fix for the headnote problem).
3. **Party names: capture now.** Add `title` (full case title) + `parties`
   (parsed) columns to `decisions_v2`, extracted from the EUR-Lex title (not a
   reliable CDM predicate). Enables name-based search and friendlier citations.
4. **Validation: per-year gate.** After each year's ingestion run integrity
   checks (within-section numbering monotonic, fetch-status counts, paragraph/
   decision counts, embedding non-null) before proceeding to the previous year.

### Schema deltas from these decisions
- `decisions_v2`: add `title text` and `parties text[]`.
- Ingestion discovery filters to `JUDG` only.
- A per-year `validate_year()` step gates progression (logs a report; halts on
  anomalies above a threshold).

---

## Backfill status & KNOWN ISSUE (older-era parsing) — 2026-06-04

**Done & clean: 2008–2026.** Modern (id-anchor) and 2008-era (name-anchor + C75
summary/grounds) templates parse correctly. The C75 section fix resolved the
2009 non-monotonic explosion (170 -> 3). decisions_v2 spans 1993–2026,
~11,419 decisions / ~616k paragraphs, 100% embedded.

**OPEN ISSUE — pre-2008 text extraction.** The backfill 2007->1954 was PAUSED at
~1992 because two older HTML templates are not yet handled well:
- **2001–2007 "S-class" / mixed template**: summaries use `class="S35MotClenumerote"`
  / `S01PointAltN`; grounds use inconsistent markup (some `<p style="text-indent">N text`,
  many classless `<p>`), varying within a single document. Current parser SILENTLY
  skips docs it can't match -> **~1,369 decisions (1993–2007) have ZERO paragraphs**
  in the DB (metadata is fine; only the paragraph text is missing).
- **1992–2000 "inline" template** (`<p>N. TEXT`): mostly parses (avg ~48–62 paras)
  but has high non-monotonic numbering (summary+grounds both numbered from 1; the
  C75 divider isn't present in this template, so the summary/grounds split fix
  doesn't apply).

**Template census (paragraph-marker by year)** is in the session notes; key point:
2011+ = id-anchor; 2008–2010 = name-anchor; 2001–2007 = S-class/mixed/none;
1992–2000 = inline.

**A cleaner source exists: Formex XML (`fmx4`)** is available for these years —
CELLAR's structured legal XML with explicit, consistent paragraph tags. Fetching
the Formex *document body* needs an extra SPARQL hop (manifestation -> item URL),
then a Formex parser. This is the likely-robust fix for 2001–2007 (HTML there is
too inconsistent to parse reliably) and possibly a better primary source overall.

**DECISION DEFERRED** (user): how to extract the older eras — options on the table:
(1) Formex for 2001–2007 + keep HTML inline for 1992–2000; (2) Formex for all
pre-2008; (3) keep patching HTML (fragile); (4) ship modern era, defer rest.
The modern corpus (2008–2026) is clean and usable now regardless.

**To resume the older-era work:** decide the approach above, then re-ingest the
affected years with `--force` (fetches are cached, so cheap) and re-embed.

---

## Older-era rebuild COMPLETE (1993-2007) via Formex — 2026-06-04

The pre-2008 parsing problem is SOLVED. A Formex (fmx4) fallback was wired into
ingest_v2 (HTML-first; when HTML yields 0 paragraphs or non-monotonic grounds,
fetch structured Formex XML and use it if its grounds are monotonic). Modern
years keep using HTML (no Formex calls, no regression).

Re-ingested 1993-2007 with --force. Validation (non-monotonic before -> after,
avg paras before -> after):
- 2002: 1.9 -> 50.3 paras/dec (26x!), nonmono 0   [worst S-class year]
- 2001: 6.7 -> 56.5, nonmono 0
- 2003: 14.9 -> 53.3, nonmono 1
- 2000: 33 -> 58.1, nonmono 1 (was 103)
- 1998: -> 53.4, nonmono 3 (was 186)
- 1994: -> 47.2, nonmono 2 (was 60; needed a targeted re-run after transient
  Formex-fetch failures during the batch)
- All other years 1995-2007: nonmono <=5, avg paras ~43-59
- 2007: keeps ~54 nonmono — NO Formex coverage there; residual quoted-legislation
  edge case in HTML. Acceptable (data complete, avg 51.7).

Net: paragraphs_v2 grew ~616k -> ~669k (the broken HTML parser had silently
DROPPED ~53k paragraphs that Formex recovered). v2 now cleanly covers 1993-2026
(~11,419 decisions), fully embedded.

LAST REMAINING PIECE: the 1992 -> 1954 ALL-CAPS era backfill (not yet started).
