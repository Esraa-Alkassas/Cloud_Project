#!/usr/bin/env bash
# run_campaign.sh — Stage 2 OTA timing campaign setup and execution.
#
# Run from the project root on the Arch Linux HOST (not devcontainer).
# Needs: AWS credentials, boto3, pyserial, detools, cryptography.
#
# Usage:
#   bash tools/run_campaign.sh [--experiment MN_DELTA_01] [--port /dev/ttyUSB0]
#                              [--runs 10] [--settle 20]
#
# Before running:
#   1. Build both firmware binaries (inside devcontainer):
#        echo "s2-base" > version.txt && idf.py reconfigure build
#        cp build/wifi_station.bin /tmp/build_s2-base.bin
#        git checkout corpus/mn-1-s2
#        echo "s2-mn1" > version.txt && idf.py reconfigure build
#        cp build/wifi_station.bin /tmp/build_s2-mn1.bin
#        git checkout feat/stage2-timing
#   2. Erase otadata so device boots from factory (s2-base):
#        python -m esptool --chip esp32 -p /dev/ttyUSB0 erase_region 0xd000 0x2000
#   3. Flash s2-base to factory (if not already there):
#        idf.py flash  (from devcontainer on feat/stage2-timing)
#
# What it does:
#   1. Encrypts and uploads both firmware binaries to S3
#   2. Generates delta patches A→B and B→A, encrypts and uploads to S3
#   3. Updates manifest so Lambda knows both versions and patches
#   4. Confirms device is running version A (or helps you prepare it)
#   5. Launches the automated campaign (harness.py campaign)
#   6. Exports results to results/stage2/

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BUCKET="esp32-firmware-rewaa"
PORT="${PORT:-/dev/ttyUSB0}"
BAUD=115200
RUNS=10
SETTLE=20
EXPERIMENT="MN_DELTA_01"
DB="results/stage2/runs.db"
OUT_DIR="results/stage2"
PLOTS_DIR="results/stage2/plots"

# Version labels embedded in the firmware app descriptor (from version.txt at build time)
VERSION_A="s2-base"
VERSION_B="s2-mn1"

# Local unencrypted firmware binaries (built from feat/stage2-timing and corpus/mn-1-s2)
BIN_A="/tmp/build_s2-base.bin"
BIN_B="/tmp/build_s2-mn1.bin"

# AES-CTR parameters — must match ota.c key/iv; set these in the environment before running:
#   export OTA_AES_KEY="..."
#   export OTA_AES_IV="..."
AES_KEY="${OTA_AES_KEY:?OTA_AES_KEY env var not set}"
AES_IV="${OTA_AES_IV:?OTA_AES_IV env var not set}"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment) EXPERIMENT="$2"; shift 2 ;;
        --port)       PORT="$2";       shift 2 ;;
        --runs)       RUNS="$2";       shift 2 ;;
        --settle)     SETTLE="$2";     shift 2 ;;
        --bin-a)      BIN_A="$2";      shift 2 ;;
        --bin-b)      BIN_B="$2";      shift 2 ;;
        --ver-a)      VERSION_A="$2";  shift 2 ;;
        --ver-b)      VERSION_B="$2";  shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "  [INFO]  $*"; }
ok()    { echo "  [OK]    $*"; }
err()   { echo "  [ERROR] $*" >&2; exit 1; }
ask()   { echo -n "  [?]     $* [y/N] "; read -r ans; [[ "$ans" =~ ^[Yy]$ ]]; }

mkdir -p "$OUT_DIR" "$PLOTS_DIR"

# ---------------------------------------------------------------------------
# 0. Prerequisites
# ---------------------------------------------------------------------------
info "Checking prerequisites..."
python3 -c "import boto3"        2>/dev/null || err "boto3 not installed: pip install boto3"
python3 -c "import serial"       2>/dev/null || err "pyserial not installed: pip install pyserial"
python3 -c "import detools"      2>/dev/null || err "detools not installed: pip install detools"
python3 -c "import cryptography" 2>/dev/null || err "cryptography not installed: pip install cryptography"
[[ -f "$BIN_A" ]] || err "Binary A not found: $BIN_A (see header comments for build instructions)"
[[ -f "$BIN_B" ]] || err "Binary B not found: $BIN_B (see header comments for build instructions)"
[[ -c "$PORT"  ]] || err "Serial port not found: $PORT (is the board connected?)"

python3 -c "
import boto3, sys
try:
    boto3.client('sts').get_caller_identity()
except Exception as e:
    print(f'AWS creds error: {e}', file=sys.stderr)
    sys.exit(1)
" || err "AWS credentials not configured. Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY or ~/.aws/credentials"
ok "Prerequisites OK"

# Export all config vars so python subprocesses can access them
export BUCKET VERSION_A VERSION_B BIN_A BIN_B
# OTA_AES_KEY and OTA_AES_IV are already in env (validated above); re-export to be safe
export OTA_AES_KEY="$AES_KEY" OTA_AES_IV="$AES_IV"

# ---------------------------------------------------------------------------
# 1. Encrypt and upload binaries to S3
# ---------------------------------------------------------------------------
info "Encrypting and uploading firmware binaries to S3..."
python3 - <<'PYEOF2'
import boto3, os, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

AES_KEY = os.environ['OTA_AES_KEY'].encode()
AES_IV  = os.environ['OTA_AES_IV'].encode()
s3      = boto3.client('s3', region_name='us-east-1')

def encrypt_bytes(data):
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CTR(AES_IV), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()

bucket  = os.environ['BUCKET']
ver_a   = os.environ['VERSION_A']
ver_b   = os.environ['VERSION_B']
bin_a   = os.environ['BIN_A']
bin_b   = os.environ['BIN_B']

for ver, path in [(ver_a, bin_a), (ver_b, bin_b)]:
    key = f'build_{ver}.bin'
    try:
        s3.head_object(Bucket=bucket, Key=key)
        print(f'  already in S3: {key}')
        continue
    except Exception:
        pass
    with open(path, 'rb') as f:
        raw = f.read()
    enc = encrypt_bytes(raw)
    print(f'  uploading {key} ({len(raw)//1024} KB, encrypted)...')
    s3.put_object(Bucket=bucket, Key=key, Body=enc)
    print(f'  uploaded:  {key}')
PYEOF2
ok "Binaries in S3"

# ---------------------------------------------------------------------------
# 2. Generate delta patches A→B and B→A, encrypt, upload, update manifest
# ---------------------------------------------------------------------------
info "Generating delta patches and updating manifest..."
python3 - <<'PYEOF'
import boto3, json, os, sys, tempfile
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import detools

AES_KEY = os.environ['OTA_AES_KEY'].encode()
AES_IV  = os.environ['OTA_AES_IV'].encode()
s3      = boto3.client('s3', region_name='us-east-1')
bucket  = os.environ['BUCKET']
ver_a   = os.environ['VERSION_A']
ver_b   = os.environ['VERSION_B']
bin_a   = os.environ['BIN_A']
bin_b   = os.environ['BIN_B']

def encrypt_bytes(data):
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CTR(AES_IV), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()

# Fetch current manifest
try:
    resp = s3.get_object(Bucket=bucket, Key='manifest.json')
    manifest = json.loads(resp['Body'].read())
except Exception:
    manifest = {'latest_version': '', 'delta_patches': {}}

# Backup
s3.put_object(Bucket=bucket, Key='manifest.backup.json',
              Body=json.dumps(manifest, indent=2).encode())
print('  manifest backed up → manifest.backup.json')

def gen_and_upload_patch(old_path, old_ver, new_path, new_ver):
    patch_name = f'patch_{old_ver}_to_{new_ver}.bin'
    patch_key  = patch_name

    try:
        s3.head_object(Bucket=bucket, Key=patch_key)
        print(f'  patch already in S3: {patch_key}')
        return patch_key
    except Exception:
        pass

    patch_path = f'/tmp/{patch_name}.plain'
    print(f'  generating {patch_name}...')
    with open(old_path, 'rb') as fo, open(new_path, 'rb') as fn, \
         open(patch_path, 'wb') as fp:
        detools.create_patch(fo, fn, fp, compression='crle')

    with open(patch_path, 'rb') as f:
        patch_data = f.read()
    enc_patch = encrypt_bytes(patch_data)

    print(f'  uploading encrypted patch ({len(patch_data)//1024} KB → {len(enc_patch)//1024} KB)...')
    s3.put_object(Bucket=bucket, Key=patch_key, Body=enc_patch)
    print(f'  uploaded: {patch_key}')
    return patch_key

patch_ab = gen_and_upload_patch(bin_a, ver_a, bin_b, ver_b)
patch_ba = gen_and_upload_patch(bin_b, ver_b, bin_a, ver_a)

manifest.setdefault('delta_patches', {})
manifest['delta_patches'][ver_a] = {'to': ver_b, 'patch_key': patch_ab}
manifest['delta_patches'][ver_b] = {'to': ver_a, 'patch_key': patch_ba}
manifest['latest_version'] = ver_b  # device has A → will update to B on first poll

s3.put_object(Bucket=bucket, Key='manifest.json',
              Body=json.dumps(manifest, indent=2).encode(),
              ContentType='application/json')
print(f'  manifest updated: latest={ver_b}')
print(f'  delta_patches registered: {ver_a}→{ver_b}, {ver_b}→{ver_a}')
PYEOF
ok "Patches generated, encrypted, and manifest updated"

# ---------------------------------------------------------------------------
# 3. Verify device is running version A
# ---------------------------------------------------------------------------
info "Checking which version the device is currently running..."
CURRENT_FW=$(python3 - <<EOF
import serial, time, json

try:
    ser = serial.Serial('$PORT', $BAUD, timeout=2)
except Exception as e:
    print(f'ERROR:{e}')
    exit(1)

fw = None
deadline = time.time() + 20
while time.time() < deadline:
    try:
        raw = ser.readline().decode('utf-8', errors='replace').strip()
    except Exception:
        continue
    if '##M##' in raw:
        idx = raw.find('##M##')
        try:
            obj = json.loads(raw[idx+6:])
            fw = obj.get('fw', '')
            break
        except Exception:
            pass
ser.close()
print(fw or 'UNKNOWN')
EOF
)
info "Device firmware: $CURRENT_FW"

if [[ "$CURRENT_FW" == "$VERSION_A" ]]; then
    ok "Device is running version A — ready to campaign"
elif [[ "$CURRENT_FW" == "$VERSION_B" ]]; then
    info "Device is running version B. Flipping manifest so it will update to A first..."
    python3 tools/flip_manifest.py set-latest --version "$VERSION_A"
    info "Waiting 70 s for device to poll and update to A (poll ~31 s × 2)..."
    sleep 70
    info "Re-checking firmware version..."
    CURRENT_FW2=$(python3 -c "
import serial, time, json
ser = serial.Serial('$PORT', $BAUD, timeout=2)
fw = None
deadline = time.time() + 20
while time.time() < deadline:
    raw = ser.readline().decode('utf-8', errors='replace').strip()
    if '##M##' in raw:
        try:
            obj = json.loads(raw[raw.find('##M##')+6:])
            fw = obj.get('fw','')
            break
        except Exception: pass
ser.close()
print(fw or 'UNKNOWN')
")
    if [[ "$CURRENT_FW2" == "$VERSION_A" ]]; then
        ok "Device is now at version A"
    else
        info "Device is at: $CURRENT_FW2 (expected $VERSION_A)"
        info "If device is still updating, wait and re-run."
        info "Or erase otadata to boot from factory: python -m esptool --chip esp32 -p $PORT erase_region 0xd000 0x2000"
        if ask "Proceed anyway (campaign will detect wrong version and abort)?"; then
            true
        else
            exit 1
        fi
    fi
else
    echo ""
    echo "  Device is running an unexpected firmware: $CURRENT_FW"
    echo "  Expected version A: $VERSION_A"
    echo ""
    echo "  To reset to version A, erase otadata so factory boots:"
    echo "    python -m esptool --chip esp32 -p $PORT erase_region 0xd000 0x2000"
    echo "  (factory partition should already have $VERSION_A)"
    echo ""
    if ask "Erase otadata and wait for factory boot now?"; then
        python -m esptool --chip esp32 -p "$PORT" erase_region 0xd000 0x2000
        info "Erased otadata. Waiting 20 s for factory boot..."
        sleep 20
    else
        echo "  Please prepare the device and re-run."
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 4. Run the campaign
# ---------------------------------------------------------------------------
ok "Starting campaign: $EXPERIMENT ($RUNS runs)"
echo ""
echo "  version A = $VERSION_A  (feat/stage2-timing)"
echo "  version B = $VERSION_B  (corpus/mn-1-s2)"
echo "  DB        = $DB"
echo "  Press Ctrl-C to abort (manifest will be restored automatically)"
echo ""

python3 tools/harness.py campaign \
    --port "$PORT" --baud "$BAUD" \
    --db   "$DB" \
    --experiment "$EXPERIMENT" \
    --version-a "$VERSION_A" \
    --version-b "$VERSION_B" \
    --runs "$RUNS" \
    --settle "$SETTLE"

# ---------------------------------------------------------------------------
# 5. Export and plot
# ---------------------------------------------------------------------------
info "Exporting results..."
python3 tools/harness.py export \
    --db "$DB" --experiment "$EXPERIMENT" \
    --out "$OUT_DIR/${EXPERIMENT}.csv"

info "Generating plots..."
python3 tools/harness.py plot \
    --db "$DB" --experiment "$EXPERIMENT" \
    --out "$PLOTS_DIR"

ok "Done."
echo ""
echo "  Results: $OUT_DIR/${EXPERIMENT}.csv"
echo "  Plots:   $PLOTS_DIR/"
echo "  DB:      $DB"
echo ""
echo "  Sanity checks:"
echo "    python3 -c \"import sqlite3; c=sqlite3.connect('$DB'); print(c.execute('SELECT count(*),sum(valid) FROM runs WHERE experiment=\\\"$EXPERIMENT\\\"').fetchone())\""
echo "    grep t_unaccounted_ms $OUT_DIR/${EXPERIMENT}.csv | head"
