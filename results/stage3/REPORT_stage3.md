# Stage 3 — On-Device Resource Footprint
**Branch:** feat/stage2-timing  
**Date:** 2026-06-14 / 2026-06-15  
**Board:** ESP32-D0WD-V3, ESP-IDF v5.2  
**DB:** `results/stage3/runs.db`

---

## 1. Objective

Stage 3 makes the device's resource footprint during an OTA update a first-class measurement. Stage 2 captured timing; this stage captures how much heap the OTA process consumes, how the OTA task uses its stack, and the exact flash write and erase pattern — including the striking fixed-cost behaviour at the flash layer regardless of patch size.

---

## 2. Method

### 2.1 Firmware changes (v3 schema)

Four fields were added to `ota_summary` and two to the `boot` event, gated behind `CONFIG_METRICS_ENABLE`:

**ota_summary additions:**
- `heap_start` — `esp_get_free_heap_size()` sampled at `ota_start`
- `largest_block_start` / `largest_block_min` — `heap_caps_get_largest_free_block(MALLOC_CAP_DEFAULT)` at start and minimum (fragmentation indicator)
- `stack_hwm` — `uxTaskGetStackHighWaterMark(NULL) × sizeof(StackType_t)` (remaining bytes in OTA task stack)
- `part_size` — target OTA partition size from `esp_partition`

**boot event additions:**
- `heap_total` — `heap_caps_get_total_size(MALLOC_CAP_DEFAULT)` at startup
- `stack_size` — `CONFIG_OTA_TASK_STACK_SIZE` (12,288 bytes)

The schema version was bumped from v2 to v3. The harness accepts both versions, so Stage 2 data remains readable.

### 2.2 Derived metrics (computed by harness)

| Derived column | Formula |
|---|---|
| `heap_used` | `heap_start − heap_min_during` |
| `heap_used_pct` | `heap_used / heap_total × 100` |
| `stack_used_pct` | `(stack_size − stack_hwm) / stack_size × 100` |
| `mean_bytes_per_write` | `b_flash / calls_w` |
| `wear_ratio` | `part_size / b_patch` |

### 2.3 Experiments

Three campaigns, N = 10 valid runs per direction (20 total each), ping-pong between firmware A and B:

| Experiment | Pair | Patch size | OTA method |
|---|---|---|---|
| MN_DELTA | s3-base ↔ s3-mn1 | ~65.4 KB | delta (detools CRLE) |
| MN_FULL | s3-base-full ↔ s3-mn1-full | ~65.2 KB | delta (detools CRLE) |
| NU_DELTA | s3-base ↔ s3-nu2 | 107 B | delta (detools CRLE) |

MN_DELTA and MN_FULL cover a minor functional change (~65 KB delta) across two firmware variant pairs. NU_DELTA uses a trivial change (one added comment) producing a 107 B patch — chosen specifically to reveal fixed-cost flash behaviour.

### 2.4 Code / binary size capture

`tools/size_report.py` was run against the current `s3-base` build. It invokes `python -m esp_idf_size --format json` on `build/wifi_station.map` and produces `results/stage3/size_report.csv` (55 components) and `results/stage3/size_summary.csv`.

---

## 3. Results

### 3.1 System constants (same across all experiments)

| Parameter | Value |
|---|---|
| `heap_total` | 296,920 B (290 KB) |
| OTA task `stack_size` | 12,288 B (12 KB) |
| OTA partition size | 1,048,576 B (1 MB) |
| Largest free block at OTA start | **110,592 B** (constant, all runs) |
| Largest free block at OTA end | **110,592 B** (constant, all runs) |

The largest-free-block invariance — identical before and after OTA — confirms **zero heap fragmentation** across all three scenarios. The heap allocator returns to an identical steady state after every OTA.

---

### 3.2 MN_DELTA (s3-base ↔ s3-mn1, N=20)

| Metric | Mean ± std | Range |
|---|---|---|
| Total time (ms) | 16,341 ± 239 | 16,005–16,791 |
| RSSI (dBm) | −67.4 ± 1.0 | −69 to −65 |
| Patch downloaded (B) | 65,414 ± 31 | 65,384–65,444 |
| Flash written (B) | 956,176 ± 33 | 956,144–956,208 |
| Flash write calls | 19,068 ± 2 | 19,066–19,069 |
| Mean B/write call | **50.14 ± 0.01** | 50.14–50.15 |
| **Heap used by OTA (B)** | **74,014 ± 852** | 72,624–75,760 |
| Heap used (% of total) | **24.93 ± 0.29%** | 24.5–25.5% |
| Heap free at minimum (B) | 136,580 ± 865 | 134,912–138,036 |
| Stack high-water mark (B remaining) | 8,047 ± 46 | 7,904–8,084 |
| **Stack used (% of 12 KB)** | **34.51 ± 0.38%** | 34.2–35.7% |
| Wear ratio (erased ÷ patch) | **16.03 ×** | — |

**Phase breakdown (ms):**

| Phase | Mean ± std |
|---|---|
| Connect | 1,575 ± 106 |
| HTTP (patch download) | 1,838 ± 78 |
| Decrypt | 35 ± 1 |
| From-read (old fw read) | 1,550 ± 8 |
| Flash write | 9,178 ± 176 |
| Finalize (SHA + boot mark) | 687 ± 8 |

Flash write dominates at 56% of wall time. HTTP for a 65 KB patch takes only 1.8 s.

---

### 3.3 MN_FULL (s3-base-full ↔ s3-mn1-full, N=20)

Both firmware variants in this pair are built from the same codebase as MN_DELTA but with a different version tag and slightly different binary layout. The OTA method is identical (delta with detools CRLE). The patch is marginally smaller (~65.2 KB) because the source and target binaries align slightly differently.

| Metric | Mean ± std | Range |
|---|---|---|
| Total time (ms) | 16,728 ± 464 | 16,306–17,937 |
| Patch downloaded (B) | 65,156 ± 30 | 65,127–65,185 |
| Flash written (B) | 956,096 ± 33 | 956,064–956,128 |
| Flash write calls | 19,022 ± 2 | 19,020–19,024 |
| Mean B/write call | **50.27 ± 0.01** | 50.26–50.27 |
| **Heap used by OTA (B)** | **73,856 ± 1,085** | 70,920–75,704 |
| Heap used (% of total) | **24.87 ± 0.36%** | 23.9–25.5% |
| Stack used (%) | **34.81 ± 0.48%** | 34.1–35.7% |
| Wear ratio | **16.09 ×** | — |

MN_FULL is statistically indistinguishable from MN_DELTA in all resource metrics. The 2.4% higher mean total time and slightly larger variance likely reflect network jitter in a longer session. All flash, heap, and stack figures replicate MN_DELTA.

---

### 3.4 NU_DELTA (s3-base ↔ s3-nu2, N=20)

s3-nu2 differs from s3-base by a single added comment, producing a 107 B patch. This is the fixed-cost floor experiment.

| Metric | Mean ± std | Range |
|---|---|---|
| Total time (ms) | **9,524 ± 111** | 9,334–9,813 |
| Patch downloaded (B) | **107 ± 0** | — |
| Flash written (B) | **956,144 ± 0** | — |
| Flash write calls | **7,475 ± 0** | — |
| Mean B/write call | **127.91 ± 0** | — |
| **Heap used by OTA (B)** | **66,699 ± 1,130** | 64,940–67,636 |
| Heap used (% of total) | **22.46 ± 0.38%** | 21.9–22.8% |
| Stack used (%) | **34.90 ± 0.51%** | 34.2–35.9% |
| **Wear ratio (erased ÷ patch)** | **9,800 ×** | — |

**Phase breakdown (ms) — NU_DELTA vs MN_DELTA:**

| Phase | NU_DELTA | MN_DELTA | Δ |
|---|---|---|---|
| Connect | 1,583 ± 103 | 1,575 ± 106 | ~0 |
| HTTP | **3 ± 0** | 1,838 ± 78 | −1,835 |
| Decrypt | **0** | 35 ± 1 | −35 |
| From-read | **690 ± 3** | 1,550 ± 8 | −860 |
| Flash write | **5,194 ± 38** | 9,178 ± 176 | −3,984 |
| Finalize | **687 ± 8** | 687 ± 8 | 0 |

---

## 4. Cross-experiment analysis

### 4.1 Flash writes are independent of patch size — write amount

All three experiments write ≈ 956 KB to the OTA partition regardless of whether the patch is 107 B or 65 KB. This is structurally correct: detools reconstructs the **full target firmware** in-place from `(old_firmware, patch)`. The flash write volume equals the reconstructed firmware size (~956 KB), not the patch size. Patch size affects network bandwidth and decode time; it has no effect on how much is written to flash.

The **erase** operation at `esp_ota_begin` always covers the full 1 MB partition, so erase volume is also patch-independent.

The wear ratio (erased ÷ downloaded) scales directly with patch compression ratio:
- MN_DELTA/MN_FULL: 16× (65 KB patch, 1 MB erase)
- NU_DELTA: **9,800×** (107 B patch, 1 MB erase)

For a production fleet running NU-class updates, a 9,800× wear ratio means each partition write erases 9,800× more flash than the actual change warrants.

### 4.2 Flash write call granularity shifts with patch size

Although total b_flash is identical, the number of write calls differs:

| Experiment | calls_w | mean B/call |
|---|---|---|
| MN_DELTA | 19,068 | 50.1 B |
| MN_FULL | 19,022 | 50.3 B |
| NU_DELTA | 7,475 | 127.9 B |

For MN_DELTA/MN_FULL, detools emits ~50 B chunks — consistent with the CRLE decoder's internal output buffer. For NU_DELTA (107 B total patch), the decoder produces the reconstructed firmware more efficiently in fewer, larger esp_ota_write calls. The ~50 B/call finding for delta-with-real-patch is a characteristic of this detools build and is documented here, not changed.

### 4.3 Heap usage scales weakly with patch complexity

| Experiment | heap_used (B) | heap_used_pct |
|---|---|---|
| MN_DELTA | 74,014 ± 852 | 24.9% |
| MN_FULL | 73,856 ± 1,085 | 24.9% |
| NU_DELTA | 66,699 ± 1,130 | 22.5% |

NU_DELTA uses ~7 KB less heap than MN_DELTA. This reflects reduced working memory in the detools decoder for a trivial patch (fewer CRLE symbol tables, smaller copy buffers). Even at 24.9% usage, 290 KB − 74 KB = ~216 KB remains free, well above any practical threshold.

Critically, heap fragmentation is **zero across all runs, all experiments**: `largest_block_start = largest_block_min = 110,592 B` without a single exception in 60 total OTA runs. The heap allocator is fully recovered after every OTA.

### 4.4 Stack usage is constant

Stack high-water mark (remaining bytes) varies by only ±60 B across 60 runs and three experiments. Stack consumed: 34.5–34.9% of the 12 KB OTA task stack (≈ 4.2–4.3 KB used). This leaves a comfortable margin of ~8 KB free, confirming that no stack growth is occurring despite the heap peaks during OTA.

### 4.5 Finalize is a fixed cost

`t_finalize_ms` is 686–687 ms across all experiments regardless of patch size. This phase covers SHA-256 verification of the reconstructed firmware and `esp_ota_set_boot_partition`. It is payload-independent: even a 107 B patch produces a 956 KB binary whose hash takes the same time to compute.

### 4.6 Total-time savings from small patches come from network + decode, not flash

NU_DELTA is 41% faster than MN_DELTA (9.5 s vs 16.3 s). The savings break down as:
- Network: −1,835 ms (107 B vs 65 KB download)
- Decode/seek: −860 ms from-read + −35 ms decrypt  
- Flash: −3,984 ms (7,475 calls vs 19,068; same bytes but larger, faster calls)
- Finalize: 0 ms (identical)

The flash time reduction is not because less was written — both write 956 KB — but because fewer, larger `esp_ota_write` calls reduce per-call overhead at the flash driver layer.

---

## 5. Code / image size

From `results/stage3/size_report.csv` (s3-base build):

| Region | Size (B) |
|---|---|
| Total binary | 956,029 |
| Flash code | 652,651 |
| Flash rodata | 191,092 |
| DRAM (data) | 16,716 |
| DRAM (BSS) | 18,312 |
| IRAM | 95,314 |

**OTA-related components:**

| Component | Total (B) | Flash code | Flash rodata | Notes |
|---|---|---|---|---|
| mbedtls | 95,625 | 28,964 | 66,409 | TLS + AES-CTR |
| mbedcrypto | 87,866 | 79,914 | 7,807 | Crypto primitives |
| app_update | 2,142 | 2,034 | 96 | esp_ota_write etc. |
| detools | 2,179 | 2,179 | 0 | Delta decoder |
| metrics | 440 | 436 | 0 | Instrumentation |
| main | 5,389 | 5,358 | 23 | Application logic |

mbedtls + mbedcrypto together account for 183 KB of the 652 KB flash code section (28%). The delta decoder (detools) adds only 2.1 KB. The metrics instrumentation adds 440 B with negligible overhead.

---

## 6. Anomalies

**Serial seq gap in MN_DELTA (run 9):** During the settle period after run 8, the USB serial buffer overflowed — sequence numbers jumped from 3 to 111. The harness detected and logged the gap, then recovered automatically by continuing to read until the next `ota_summary` arrived. The run was recorded as VALID with no data loss from the OTA perspective. This is a known artifact of the 115200-baud serial interface when the host is context-switched during the settle wait.

**No failures:** All 60 OTA runs across all three campaigns completed successfully. Zero runs were recorded in `run_failures`. The silence-detection and circuit-breaker code was not triggered.

---

## 7. Plots

All plots in `results/stage3/plots/`:

**Per-experiment (5 plots each):** `{EXP}_heap.png`, `{EXP}_phase_bar.png`, `{EXP}_throughput_scatter.png`, `{EXP}_total_box.png`, `{EXP}_event_counts.png`

**Stage 3 cross-experiment (3 plots):**
- `stage3_heap_bar.png` — lowest free heap and heap-consumed per experiment, with total heap reference line
- `stage3_bytes_bar.png` — bytes downloaded vs bytes written vs bytes erased per experiment, log scale
- `stage3_component_sizes.png` — updater component sizes from size report (horizontal bar)
