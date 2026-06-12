# Tools — ESP32 Delta-OTA Measurement Harness

Run all scripts on the **Arch Linux host** (not inside the devcontainer).

```
pip install -r tools/requirements.txt
```

---

## Serial port exclusivity

**Only one process can hold `/dev/ttyUSB0` at a time.**
`idf.py monitor` and `harness.py capture/tail` are mutually exclusive.
Always close the monitor before running capture:

```bash
# In the devcontainer terminal: Ctrl-] to exit idf.py monitor
# Then on the host:
python tools/harness.py capture --port /dev/ttyUSB0 ...
```

`harness.py` prints a clear error if the port is busy.

---

## version.txt quirk (local builds)

`version.txt` is gitignored and is normally produced by CI.
For local builds, write it manually before `idf.py build`:

```bash
echo "$(date +%s)-local" > version.txt
idf.py build
```

Without it, `PROJECT_VER` falls back to `"1"` and all metric lines will show `fw:"1"`.

---

## Full capture workflow

```bash
# 1. Flash instrumented firmware (inside devcontainer)
idf.py flash

# 2. Capture metrics on the host (close monitor first!)
python tools/harness.py capture \
  --port /dev/ttyUSB0 --baud 115200 \
  --db runs.db \
  --experiment EXP_001 \
  --scenario NU \
  --note "baseline no-update run" \
  --duration 600

# 3. Export to CSV
python tools/harness.py export \
  --db runs.db --experiment EXP_001 --out exp001.csv

# 4. Plot
python tools/harness.py plot \
  --db runs.db --experiment EXP_001 --out plots/

# 5. Live tail (debug, no DB)
python tools/harness.py tail --port /dev/ttyUSB0
```

Raw console log is saved to `logs/<EXP_ID>_<timestamp>.rawlog` automatically.

---

## Corpus builder (`make_corpus.py`)

Requires Docker with the `espressif/idf:release-v5.2` image pulled.

```bash
# Build two firmware versions
python tools/make_corpus.py build \
  --ref main --version-label v1 --out corpus/

python tools/make_corpus.py build \
  --ref HEAD~3 --version-label v0 --out corpus/

# Generate a delta patch (unencrypted — AES-CTR is size-preserving)
python tools/make_corpus.py pair \
  --old corpus/v0.bin --new corpus/v1.bin \
  --out corpus/pairs/ --scenario NU

# CSV report
python tools/make_corpus.py report \
  --corpus corpus/ --out corpus/corpus.csv
```

---

## Manifest flipper (`flip_manifest.py`)

Requires AWS credentials (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` in env,
or `~/.aws/credentials`, or an IAM role).

```bash
# Inspect current manifest
python tools/flip_manifest.py show

# Backup before experimenting
python tools/flip_manifest.py backup

# Point latest_version at an existing build (validates build exists in S3)
python tools/flip_manifest.py set-latest --version 1748000000-ab12cd3

# Restore if needed
python tools/flip_manifest.py restore
```

`set-latest` refuses to set a version whose `build_<ver>.bin` is absent from the bucket,
and leaves `delta_patches` untouched.

---

## Stage 1 — Patch-size benchmark (`bench_diff.py`)

### Tool installation

**On the Arch Linux host (outside devcontainer):**

```bash
sudo pacman -S bsdiff xdelta3
yay -S hdiffpatch          # or: paru -S hdiffpatch
pip install detools>=0.52 cryptography>=41.0
```

**Inside the ESP-IDF devcontainer (Ubuntu 22.04) — required for corpus builds:**

```bash
apt-get install -y bsdiff xdelta3

# hdiffpatch is NOT in Ubuntu apt — build from source (~2 min):
# The Makefile references ../lzma and ../bzip2 as sibling dirs; disable them
# and use system libs instead (LZMA=0 ZSTD=0 LDEF=0 ZLIB=2 with libbz2-dev installed).
apt-get install -y libbz2-dev libzstd-dev
cd /tmp && git clone --depth=1 https://github.com/sisong/HDiffPatch.git hdiffpatch_src
cd /tmp/hdiffpatch_src
make hdiffz hpatchz LZMA=0 ZSTD=0 LDEF=0 ZLIB=2 MD5=0 XXH=0 VCD=0 DIR_DIFF=0 MT=0 BSD=0
cp hdiffz hpatchz /usr/local/bin/

pip install detools>=0.52 cryptography>=41.0 matplotlib numpy pandas
```

> **Note for replication:** hdiffpatch has no Ubuntu package and no published binary release.
> The source-build step above is required on any Ubuntu/Debian machine.
> On Arch, `yay -S hdiffpatch` is simpler. The compiled binaries (`hdiffz`, `hpatchz`) are
> what bench_diff.py calls — just ensure they are on `$PATH`.

Verify all tools:

```bash
bsdiff 2>&1 | head -1
xdelta3 --version
hdiffz --version
detools --version
```

### Corpus branches

Each branch lives in `corpus/<label>` and differs from `feat/runtim-mesurments` by exactly one change:

| Branch | Scenario | Change |
|--------|----------|--------|
| `feat/runtim-mesurments` | base | production firmware |
| `corpus/nu-2` | NU | add comment to `ota.c` |
| `corpus/nu-3` | NU | change OTA success log string |
| `corpus/mn-1` | MN | static attempt counter + 3 s reboot delay |
| `corpus/mn-2` | MN | add `ota_check_heap_guard()` function |
| `corpus/mn-3` | MN | add `heap_delta` field to heartbeat |
| `corpus/mj-1` | MJ | enable `CONFIG_TELEMETRY_ENABLE=y` |
| `corpus/mj-2` | MJ | enable debug log level + mbedtls debug |
| `corpus/mj-3` | MJ | add `diag.c`/`diag.h` (~120 lines) |
| `corpus/wc-1` | WC | switch optimisation to `-O2` (perf) |

### Building the corpus binaries

**Inside the devcontainer** (Docker not available there — use `build_corpus.py`):

```bash
# Preview what will be built
python tools/build_corpus.py --dry-run

# Build all 10 binaries (~60–80 min, logs per-build in results/stage1/)
python tools/build_corpus.py --out corpus/bins/ --manifest-out results/stage1/

# Re-run skipping already-built binaries
python tools/build_corpus.py --out corpus/bins/ --skip-existing
```

Per-build logs land at `results/stage1/build_<label>.log`.
A JSON manifest at `results/stage1/corpus_builds.json` records label, branch,
git SHA, build time, binary size, and SHA256 for every build.

**On the Arch host** (Docker available — uses `make_corpus.py` directly):

```bash
for label in base nu-2 nu-3 mn-1 mn-2 mn-3 mj-1 mj-2 mj-3 wc-1; do
  ref="feat/runtim-mesurments"
  [ "$label" != "base" ] && ref="corpus/$label"
  python tools/make_corpus.py build --ref "$ref" --version-label "$label" --out corpus/bins/
done
```

### Running the benchmark

```bash
python tools/bench_diff.py run \
  --corpus corpus/bins/ \
  --pairs "base:nu-2:NU" "base:nu-3:NU" \
          "base:mn-1:MN" "base:mn-2:MN" "base:mn-3:MN" \
          "base:mj-1:MJ" "base:mj-2:MJ" "base:mj-3:MJ" \
          "base:wc-1:WC" \
  --out results/bench.csv \
  --reps 5

# Quick summary table
python tools/bench_diff.py report --csv results/bench.csv
```

### Sanity checks

After the run, eyeball these invariants in `results/bench.csv`:

- `verified=True` for every row (patch round-trips correctly)
- `full` rows have `compression_factor=1.0` and `reduction_pct=0`
- `detools-lzma` rows have a `note` warning about device-deployability
- NU pairs should have the highest `compression_factor` (tiny patches)
- WC pair should have the lowest `compression_factor` (near-full-image patch)

---

## Metric line format

Every metric line on serial looks like:

```
##M## {"v":2,"seq":17,"t_us":123456789,"fw":"1748000000-ab12cd3","dev":"esp32-A","ev":"boot","reset_reason":1,"part":"ota_0","prev_boot_marker":1,"t_app_ms":412}
```

- `##M## ` prefix — survives interleaved ESP_LOG noise; grep-friendly.
- `v` — schema version (2 from Stage 2 onwards).
- `seq` — monotonically increasing per boot; gaps indicate dropped UART lines.
- `t_us` — µs since boot (`esp_timer_get_time()`).
- `fw` — firmware version from app descriptor.
- `dev` — `CONFIG_METRICS_DEVICE_ID` (default `esp32-A`).
- `ev` — event name.

Events (schema v2):
| Event | Extra fields |
|-------|-------------|
| `boot` | `reset_reason`, `part`, `prev_boot_marker`, `t_app_ms` |
| `heartbeat` | `heap_free`, `heap_min` (bytes) |
| `ota_check` | `rtt_ms`, `http` (status code), `update`, `is_delta`, `rssi`, `heap_free` |
| `ota_start` | `is_delta`, `fw_to`, `force_full`, `rssi`, `heap_min` |
| `ota_summary` | `ok`, `err`, `is_delta`, `t_total_ms`, `t_connect_ms`, `t_apply_wall_ms`, `t_http`, `t_decrypt`, `t_from_read`, `t_flash`, `t_seek`, `t_finalize_ms`, `b_patch`, `b_flash`, `b_from`, `calls_r`, `calls_w`, `rssi`, `heap_min_during` |
| `ota_reboot` | *(none; serial timestamp is the reboot-time anchor)* |

All time fields in `ota_summary` are in **milliseconds**. Accumulator fields (`t_http`, `t_decrypt`, `t_from_read`, `t_flash`, `t_seek`) are derived in the harness to yield:
- `goodput_kBps = b_patch / t_http`
- `flash_kBps = b_flash / t_flash`
- `t_reboot_ms` = wall-clock diff `recv_utc(boot) − recv_utc(ota_reboot)` (serial-buffer jitter ≤100 ms)
- `t_downtime_ms = t_reboot_ms + boot.t_app_ms`
- `t_unaccounted_ms = t_apply_wall_ms − (t_http + t_decrypt + t_from_read + t_flash + t_seek)` — this is diff-CPU + scheduling overhead; should be positive and <25% of wall time.

---

## Stage 2 — On-device time decomposition

### Kconfig options added (in `main/Kconfig.projbuild`)

| Option | Default | Purpose |
|--------|---------|---------|
| `CONFIG_OTA_FORCE_FULL` | n | Appends `force_full=1` to every broker check; forces full-image OTA. Used for the Stage 2 full-image baseline. |
| `CONFIG_METRICS_OTA_PROGRESS` | n | Emits `ota_progress` event every 128 KB (debug only — **never** enable during timed runs). |

### Experiment protocol

**Before starting a campaign:**
1. `CONFIG_TELEMETRY_ENABLE=n`, `CONFIG_METRICS_OTA_PROGRESS=n` in `sdkconfig.defaults`.
2. Board placement and AP unchanged for the entire campaign; note RSSI from `ota_check` events.
3. Both versions in the ping-pong must carry Stage-2 instrumentation (build them from `feat/stage2-timing` or rebased `corpus/*` branches).

**Campaigns to run (N=10 valid runs each):**
| Campaign | Branch pair | `OTA_FORCE_FULL` | Priority |
|----------|-------------|-----------------|---------|
| MN-delta | `base` ↔ `corpus/mn-1` (or mn-2/mn-3) | n | Primary |
| MN-full  | same pair | y (recompile) | Primary |
| NU-delta | `base` ↔ `corpus/nu-2` | n | Secondary |
| MJ-delta | `base` ↔ `corpus/mj-3` | n | Secondary |

**Running a campaign:**
```bash
# 1. Flash instrumented firmware (inside devcontainer)
echo "$(date +%s)-stage2a" > version.txt && idf.py build flash

# 2. Close the monitor, then on the host:
python tools/harness.py campaign \
  --port /dev/ttyUSB0 --db results/stage2/runs.db \
  --experiment MN_DELTA_01 \
  --version-a <sha_base> --version-b <sha_mn1> \
  --runs 10 --settle 20

# 3. Export to CSV (includes derived columns)
python tools/harness.py export \
  --db results/stage2/runs.db --experiment MN_DELTA_01 \
  --out results/stage2/MN_DELTA_01.csv

# 4. Generate plots
python tools/harness.py plot \
  --db results/stage2/runs.db --experiment MN_DELTA_01 \
  --out results/stage2/plots/
```

**Sanity checks on results:**
- `t_unaccounted_ms` positive and <25% of `t_apply_wall_ms` in all valid runs.
- Flash throughput (`flash_kBps`) roughly constant across runs (±15%).
- Ordering: NU total time < MN < MJ; delta total < full for NU/MN pairs.
- RSSI ≥ −75 dBm in all valid runs (harness auto-excludes weaker-signal runs).
