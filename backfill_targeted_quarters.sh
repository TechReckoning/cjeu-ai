#!/bin/bash

cd ~/cjeu-ai || exit 1
source .venv/bin/activate

echo "========================================="
echo "Starting targeted quarterly backfill"
date
echo "========================================="

YEARS=("2006" "2007" "2017")

QUARTERS=(
  "01-01 03-31"
  "04-01 06-30"
  "07-01 09-30"
  "10-01 12-31"
)

for YEAR in "${YEARS[@]}"; do
  for Q in "${QUARTERS[@]}"; do
    START_MD=$(echo $Q | cut -d' ' -f1)
    END_MD=$(echo $Q | cut -d' ' -f2)

    DATE_FROM="$YEAR-$START_MD"
    DATE_TO="$YEAR-$END_MD"

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

echo "Targeted quarterly backfill completed."
date
