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

## Metric line format

Every metric line on serial looks like:

```
##M## {"v":1,"seq":17,"t_us":123456789,"fw":"1748000000-ab12cd3","dev":"esp32-A","ev":"boot","reset_reason":1,"part":"ota_0","prev_boot_marker":1}
```

- `##M## ` prefix — survives interleaved ESP_LOG noise; grep-friendly.
- `v` — schema version (1).
- `seq` — monotonically increasing per boot; gaps indicate dropped UART lines.
- `t_us` — µs since boot (`esp_timer_get_time()`).
- `fw` — firmware version from app descriptor.
- `dev` — `CONFIG_METRICS_DEVICE_ID` (default `esp32-A`).
- `ev` — event name.

Stage 0 events:
| Event | Extra fields |
|-------|-------------|
| `boot` | `reset_reason`, `part`, `prev_boot_marker` |
| `heartbeat` | `heap_free`, `heap_min` (bytes) |
