#!/bin/bash
# Backfill driver: ingest + embed each year, 2026 -> 1954, in order.
# Resumable (ingest skips done decisions; embed only fills NULLs). Watchdog-wrapped
# so a hung Supabase-pooler connection dies and the step retries.
cd /Users/serbansarbu/Desktop/Amicus
VENV=./.venv/bin/python
START=${1:-2026}
END=${2:-1954}

for YEAR in $(seq $START -1 $END); do
  echo "============================================================"
  echo "YEAR $YEAR  ($(date +%H:%M:%S))"
  echo "============================================================"
  # 1. ingest (no-embed): write decisions + paragraphs. Resume-skips done ones.
  $VENV ingest_v2.py --year $YEAR --no-embed 2>&1 | grep -E "JUDG|resume|fetch_ok|fetch_bad|no_english|nonmono|avg para|decisions_v2=|paragraphs_v2=|Nothing"
  # 2. embed that year via watchdog restart loop
  for i in $(seq 1 500); do
    out=$(perl -e 'alarm 150; exec @ARGV' $VENV embed_v2.py --year $YEAR --one-chunk 2>&1)
    echo "$out" | grep -q ALL_DONE && { echo "  [$YEAR] embeddings complete"; break; }
    echo "$out" | grep -q "paragraphs to embed: 0" && { echo "  [$YEAR] nothing to embed"; break; }
  done
  echo "  [$YEAR] DONE $(date +%H:%M:%S)"
done
echo "BACKFILL_COMPLETE $START..$END"
