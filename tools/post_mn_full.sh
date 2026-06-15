#!/usr/bin/env bash
# post_mn_full.sh — Export MN_FULL results then run NU_DELTA + MJ_DELTA.
# Run from project root after task bg6pk490g (MN_FULL) has completed.
set -euo pipefail

export AWS_DEFAULT_REGION="us-east-1"  # credentials in ~/.aws/credentials

DB="results/stage2/runs.db"
OUT_DIR="results/stage2"
PLOTS_DIR="results/stage2/plots"

mkdir -p "$OUT_DIR" "$PLOTS_DIR"

echo "=== Export MN_FULL ==="
PYTHONUNBUFFERED=1 python3 tools/harness.py export \
    --db "$DB" --experiment MN_FULL --out "$OUT_DIR/MN_FULL.csv"
PYTHONUNBUFFERED=1 python3 tools/harness.py plot \
    --db "$DB" --experiment MN_FULL --out "$PLOTS_DIR"
echo "  MN_FULL exported → $OUT_DIR/MN_FULL.csv"

echo "=== Running NU_DELTA + MJ_DELTA ==="
bash tools/run_nu_mj_campaigns.sh
