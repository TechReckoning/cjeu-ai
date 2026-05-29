#!/bin/bash

cd ~/cjeu-ai || exit 1
source .venv/bin/activate

echo "Starting historical CJEU backfill..."
date

YEARS=(
  "1954 1959"
  "1960 1969"
  "1970 1979"
  "1980 1989"
  "1990 1999"
  "2000 2009"
  "2010 2019"
  "2020 2026"
)

for RANGE in "${YEARS[@]}"; do
  START_YEAR=$(echo $RANGE | cut -d' ' -f1)
  END_YEAR=$(echo $RANGE | cut -d' ' -f2)

  echo "----------------------------------------"
  echo "Processing $START_YEAR-$END_YEAR"
  echo "----------------------------------------"

  cjeu-py download-cellar \
    --max-items 10000 \
    --doc-types CJ \
    --date-from "$START_YEAR-01-01" \
    --date-to "$END_YEAR-12-31" \
    --skip-citations \
    --skip-subjects \
    --force

  cjeu-py fetch-texts --max-items 10000

  python incremental_index_pgvector.py

  echo "Finished $START_YEAR-$END_YEAR"
  date
done

echo "Historical backfill completed."
date
