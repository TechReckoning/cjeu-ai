#!/bin/bash

cd ~/cjeu-ai || exit 1
source .venv/bin/activate

echo "========================================="
echo "Starting problem years backfill"
date
echo "========================================="

YEARS=(
  "2004"
  "2005"
  "2006"
  "2013"
  "2014"
  "2015"
  "2016"
  "2017"
  "2022"
  "2023"
  "2025"
  "2026"
)

for YEAR in "${YEARS[@]}"; do
  echo "-----------------------------------------"
  echo "Processing year $YEAR"
  echo "-----------------------------------------"

  cjeu-py download-cellar \
    --max-items 10000 \
    --doc-types CJ \
    --date-from "$YEAR-01-01" \
    --date-to "$YEAR-12-31" \
    --skip-citations \
    --skip-subjects \
    --force

  cjeu-py fetch-texts --max-items 10000

  python incremental_index_pgvector.py

  echo "Finished year $YEAR"
  date
done

echo "Problem years backfill completed."
date
