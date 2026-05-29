#!/bin/bash

cd ~/cjeu-ai || exit 1
source .venv/bin/activate

echo "========================================="
echo "Starting targeted monthly backfill"
date
echo "========================================="

YEARS=("2007" "2017" "2025" "2026")

for YEAR in "${YEARS[@]}"; do
  for MONTH in $(seq -w 1 12); do
    DATE_FROM="$YEAR-$MONTH-01"
    DATE_TO=$(date -j -v+1m -v1d -v-1d -f "%Y-%m-%d" "$DATE_FROM" +"%Y-%m-%d" 2>/dev/null)

    echo "-----------------------------------------"
    echo "Processing $DATE_FROM to $DATE_TO"
    echo "-----------------------------------------"

    cjeu-py download-cellar \
      --max-items 10000 \
      --doc-types CJ \
      --date-from "$DATE_FROM" \
      --date-to "$DATE_TO" \
      --skip-citations \
      --skip-subjects \
      --force

    cjeu-py fetch-texts --max-items 10000

    python incremental_index_pgvector.py

    echo "Finished $DATE_FROM to $DATE_TO"
    date
  done
done

echo "Targeted monthly backfill completed."
date
