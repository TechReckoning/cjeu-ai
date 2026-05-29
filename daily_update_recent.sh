#!/bin/bash

cd ~/cjeu-ai || exit 1
source .venv/bin/activate

DATE_TO=$(date +"%Y-%m-%d")
DATE_FROM=$(date -v-60d +"%Y-%m-%d")

echo "========================================="
echo "Starting recent CJEU update"
echo "Date range: $DATE_FROM to $DATE_TO"
date
echo "========================================="

echo ""
echo "STEP 1 — Downloading recent metadata..."
echo ""

cjeu-py download-cellar \
  --max-items 10000 \
  --doc-types CJ \
  --date-from "$DATE_FROM" \
  --date-to "$DATE_TO" \
  --skip-citations \
  --skip-subjects \
  --force

echo ""
echo "STEP 2 — Fetching recent texts..."
echo ""

cjeu-py fetch-texts --max-items 10000

echo ""
echo "STEP 3 — Incremental indexing..."
echo ""

python incremental_index_pgvector.py

echo ""
echo "========================================="
echo "Recent update completed"
date
echo "========================================="
