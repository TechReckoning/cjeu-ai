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

## Open questions for review
1. Include `ORDER` and `OPIN_AG` (AG opinions) as their own decisions, or
   judgments only? (Opinions are persuasive and citation-relevant.)
2. Keep `summary`/`operative` paragraphs in `paragraphs_v2` (tagged) or grounds
   only? (Recommend keep + tag — section filter handles ranking.)
3. Party names: extract from EUR-Lex title (not a reliable CDM predicate) into a
   `parties`/`title` column now, or defer?
4. Per-year validation gate before proceeding to the next year, or run straight
   through and validate at the end?
