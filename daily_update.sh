#!/bin/bash

cd ~/cjeu-ai || exit 1

source .venv/bin/activate

echo "========================================="
echo "Starting CJEU daily update"
date
echo "========================================="

echo ""
echo "STEP 1 — Downloading latest metadata..."
echo ""

cjeu-py download-cellar \
  --max-items 10000 \
  --doc-types CJ \
  --skip-citations \
  --skip-subjects \
  --force

echo ""
echo "STEP 2 — Fetching texts..."
echo ""

cjeu-py fetch-texts --max-items 10000

echo ""
echo "STEP 3 — Incremental indexing..."
echo ""

python incremental_index_pgvector.py

echo ""
echo "========================================="
echo "Daily update completed"
date
echo "========================================="
