# Stage 3 Summary — On-Device Resource Footprint

**Branch:** feat/stage2-timing | **Date:** 2026-06-14/15 | **Board:** ESP32-D0WD-V3 | **ESP-IDF:** v5.2

## Run Metadata

| | MN_DELTA | MN_FULL | NU_DELTA |
|---|---|---|---|
| Firmware pair | s3-base ↔ s3-mn1 | s3-base-full ↔ s3-mn1-full | s3-base ↔ s3-nu2 |
| OTA method | delta (detools) | delta (detools) | delta (detools) |
| N valid runs | 20 (10 each dir) | 20 (10 each dir) | 20 (10 each dir) |
| RSSI (dBm) | −67.4 ± 1.0 | −67.7 ± 0.9 | −68.0 ± 0.8 |
| Patch size (B) | 65,414 ± 31 | 65,156 ± 30 | **107** (fixed) |

---

## Key Metric Tables (mean ± std)

### Memory

| Metric | MN_DELTA | MN_FULL | NU_DELTA |
|---|---|---|---|
| heap_total (B) | 296,920 | 296,920 | 296,920 |
| heap_start (B) | 210,594 ± 144 | 210,591 ± 140 | 210,617 ± 201 |
| **heap_used (B)** | **74,014 ± 852** | **73,856 ± 1,085** | **66,699 ± 1,130** |
| **heap_used_pct** | **24.9 ± 0.3%** | **24.9 ± 0.4%** | **22.5 ± 0.4%** |
| heap_min_during (B) | 136,580 ± 865 | 136,734 ± 1,121 | 143,918 ± 1,174 |
| largest_block_start (B) | 110,592 (fixed) | 110,592 (fixed) | 110,592 (fixed) |
| largest_block_min (B) | 110,592 (fixed) | 110,592 (fixed) | 110,592 (fixed) |
| stack_size (B) | 12,288 | 12,288 | 12,288 |
| stack_hwm remaining (B) | 8,047 ± 46 | 8,010 ± 59 | 7,999 ± 63 |
| **stack_used_pct** | **34.5 ± 0.4%** | **34.8 ± 0.5%** | **34.9 ± 0.5%** |

### Flash

| Metric | MN_DELTA | MN_FULL | NU_DELTA |
|---|---|---|---|
| b_patch (B) | 65,414 ± 31 | 65,156 ± 30 | **107** |
| **b_flash / b_written (B)** | **956,176 ± 33** | **956,096 ± 33** | **956,144** |
| part_size / b_erased (B) | 1,048,576 | 1,048,576 | 1,048,576 |
| calls_w | 19,068 ± 2 | 19,022 ± 2 | **7,475** |
| **mean B/write call** | **50.14** | **50.27** | **127.91** |
| **wear_ratio** | **16.03 ×** | **16.09 ×** | **9,800 ×** |

### Timing (ms)

| Metric | MN_DELTA | MN_FULL | NU_DELTA |
|---|---|---|---|
| **t_total_ms** | **16,341 ± 239** | **16,728 ± 464** | **9,524 ± 111** |
| t_connect_ms | 1,575 ± 106 | 1,672 ± 162 | 1,583 ± 103 |
| t_http (download) | 1,838 ± 78 | 1,894 ± 240 | **3 ± 0** |
| t_decrypt | 35 ± 1 | 35 ± 1 | **0** |
| t_from_read | 1,550 ± 8 | 1,549 ± 7 | **690 ± 3** |
| t_flash | 9,178 ± 176 | 9,314 ± 125 | **5,194 ± 38** |
| **t_finalize_ms** | **687 ± 8** | **686 ± 6** | **687 ± 8** |

### Binary size (s3-base build)

| Component | Total (B) | Flash code (B) |
|---|---|---|
| **Total binary** | **956,029** | 652,651 |
| mbedtls | 95,625 | 28,964 |
| mbedcrypto | 87,866 | 79,914 |
| detools | 2,179 | 2,179 |
| app_update | 2,142 | 2,034 |
| metrics | 440 | 436 |

---

## Key Findings

- **Flash writes are patch-size-independent.** All three experiments write ≈ 956 KB to the OTA partition, regardless of whether the patch is 107 B or 65 KB. detools reconstructs the full target firmware on-device; the write volume equals the reconstructed binary size, not the patch.

- **Erase is also fixed-cost.** `esp_ota_begin` always erases the full 1 MB partition. Combined with fixed write volume, the **wear ratio** is entirely determined by patch size: 16× for 65 KB patches, and **9,800×** for the 107 B NU_DELTA patch.

- **OTA consumes ~25% of total heap** (74 KB out of 296 KB) for MN-class patches, falling to ~22.5% for the trivial NU patch. In all cases the minimum free heap stays above 134 KB, and the heap recovers completely after OTA — **zero fragmentation in 60 runs**.

- **Stack usage is 34–35% of the 12 KB OTA task stack** (~4.2 KB used), stable across all patch sizes and both directions. ~8 KB headroom remains.

- **Finalize (SHA-256 + boot mark) is a fixed 687 ms** regardless of patch size — it hashes the full reconstructed binary, not the patch.

- **Small patches do save wall time** (NU_DELTA 41% faster: 9.5 s vs 16.3 s for MN_DELTA), but the savings come from network and decode time, not from flash. Even a 107 B patch takes 5.2 s of flash writes and 687 ms of finalize.

- **MN_FULL replicates MN_DELTA** in all resource metrics: heap, stack, flash volume, and timing are statistically indistinguishable. Different firmware variant pair, same OTA footprint.

- **No fragmentation, no failures.** `largest_block_start = largest_block_min = 110,592 B` in every single run. Zero entries in `run_failures`. The silence-detection and circuit-breaker paths were not triggered.
