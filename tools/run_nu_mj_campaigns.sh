#!/usr/bin/env bash
# run_nu_mj_campaigns.sh — Run NU_DELTA and MJ_DELTA campaigns back-to-back.
# Run from project root after MN_FULL campaign has completed and device port is free.
#
# Usage:  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... bash tools/run_nu_mj_campaigns.sh

set -euo pipefail

PORT="/dev/ttyUSB0"
BAUD=115200
RUNS=10
SETTLE=20
DB="results/stage2/runs.db"
OUT_DIR="results/stage2"
PLOTS_DIR="results/stage2/plots"
BUCKET="esp32-firmware-rewaa"

export AWS_DEFAULT_REGION="us-east-1"

info()  { echo "  [INFO]  $*"; }
ok()    { echo "  [OK]    $*"; }
err()   { echo "  [ERROR] $*" >&2; exit 1; }

mkdir -p "$OUT_DIR" "$PLOTS_DIR"

flash_s2_base() {
    info "Flashing s2-base to factory partition and erasing otadata..."
    esptool.py --chip esp32 --port "$PORT" --baud 460800 \
        erase_region 0xd000 0x2000
    esptool.py --chip esp32 --port "$PORT" --baud 460800 \
        write_flash 0x10000 /tmp/build_s2-base.bin
    ok "Flashed s2-base. Waiting 8 s for boot..."
    sleep 8
}

set_manifest_for_nu() {
    info "Updating manifest for NU_DELTA (s2-base→s2-nu2 patch)..."
    python3 - <<'EOF'
import boto3, json
s3 = boto3.client('s3', region_name='us-east-1')
BUCKET = 'esp32-firmware-rewaa'
mf = json.loads(s3.get_object(Bucket=BUCKET, Key='manifest.json')['Body'].read())
mf['delta_patches']['s2-base'] = 'patch_s2-base_to_s2-nu2.bin'
mf['latest_version'] = 's2-nu2'
body = json.dumps(mf, indent=2).encode()
s3.put_object(Bucket=BUCKET, Key='manifest.json', Body=body, ContentType='application/json')
s3.put_object(Bucket=BUCKET, Key='manifest.backup.json', Body=body, ContentType='application/json')
print("  manifest → latest=s2-nu2, s2-base patch=s2-nu2")
EOF
}

set_manifest_for_mj() {
    info "Updating manifest for MJ_DELTA (s2-base→s2-mj3 patch)..."
    python3 - <<'EOF'
import boto3, json
s3 = boto3.client('s3', region_name='us-east-1')
BUCKET = 'esp32-firmware-rewaa'
mf = json.loads(s3.get_object(Bucket=BUCKET, Key='manifest.json')['Body'].read())
mf['delta_patches']['s2-base'] = 'patch_s2-base_to_s2-mj3.bin'
mf['latest_version'] = 's2-mj3'
body = json.dumps(mf, indent=2).encode()
s3.put_object(Bucket=BUCKET, Key='manifest.json', Body=body, ContentType='application/json')
s3.put_object(Bucket=BUCKET, Key='manifest.backup.json', Body=body, ContentType='application/json')
print("  manifest → latest=s2-mj3, s2-base patch=s2-mj3")
EOF
}

export_and_plot() {
    local exp="$1"
    info "Exporting $exp..."
    PYTHONUNBUFFERED=1 python3 tools/harness.py export \
        --db "$DB" --experiment "$exp" --out "$OUT_DIR/${exp}.csv"
    info "Plotting $exp..."
    PYTHONUNBUFFERED=1 python3 tools/harness.py plot \
        --db "$DB" --experiment "$exp" --out "$PLOTS_DIR"
    ok "$exp exported and plotted → $OUT_DIR/${exp}.csv"
}

# ── NU_DELTA ────────────────────────────────────────────────────────────────
flash_s2_base
set_manifest_for_nu

info "Starting NU_DELTA campaign..."
PYTHONUNBUFFERED=1 python3 -u tools/harness.py campaign \
    --port "$PORT" --baud "$BAUD" \
    --db   "$DB" \
    --experiment "NU_DELTA" \
    --version-a "s2-base" \
    --version-b "s2-nu2" \
    --runs "$RUNS" \
    --settle "$SETTLE"

export_and_plot "NU_DELTA"

# ── MJ_DELTA ────────────────────────────────────────────────────────────────
flash_s2_base
set_manifest_for_mj

info "Starting MJ_DELTA campaign..."
PYTHONUNBUFFERED=1 python3 -u tools/harness.py campaign \
    --port "$PORT" --baud "$BAUD" \
    --db   "$DB" \
    --experiment "MJ_DELTA" \
    --version-a "s2-base" \
    --version-b "s2-mj3" \
    --runs "$RUNS" \
    --settle "$SETTLE"

export_and_plot "MJ_DELTA"

ok "All campaigns done. Results in $OUT_DIR/"
