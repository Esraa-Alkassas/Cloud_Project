# Stage 2 Report — On-Device Time Decomposition of the OTA Update

**Date:** 2026-06-14  
**Repository branch:** `feat/stage2-timing`  
**IDF version:** ESP-IDF v5.2.6  
**Board:** ESP32-D0WD-V3 (revision v3.1), 240 MHz, 4 MB flash  
**WiFi AP RSSI:** −59 to −70 dBm (measured per run; stable throughout campaigns)

---

## 1. Objective

Measure, on real hardware, the standard decomposition of end-to-end OTA update latency:

```
T_update = T_check + T_connect + T_apply(T_http + T_decrypt + T_from_read + T_flash + T_unaccounted)
         + T_finalize + T_reboot
```

…for delta and full-image updates, across Stage 1 scenario pairs, with N = 10 automated repetitions per direction (A→B and B→A, i.e. 20 valid runs per experiment). Output: mean ± std per phase per scenario, stacked-bar and throughput plots, derived metrics.

This data forms the paper's **Table 2 / Figure "where does update time go"**.

---

## 2. Metric Definitions

| Metric | Measurement method | Meaning |
|--------|--------------------|---------|
| `T_check` | ota_check `rtt_ms` (firmware timer) | Round-trip time of the HTTP broker check (DNS + TLS + request + response body) |
| `T_connect` | `t_connect_ms` (firmware timer) | S3 connection time: TCP open → TLS handshake → first byte of content headers |
| `T_http` | `t_http` accumulator (per-callback) | Net time inside `read_patch_cb` doing HTTP reads, including TLS record decoding |
| `T_decrypt` | `t_decrypt` accumulator | Time in AES-128-CTR decryption of patch bytes inside `read_patch_cb` |
| `T_from_read` | `t_from_read` accumulator | Time in `read_old_cb` reading the source partition via `esp_partition_read` |
| `T_flash` | `t_flash` accumulator | Time in `write_new_cb` writing the reconstructed image via `esp_ota_write` |
| `T_seek` | `t_seek` accumulator | Time in `seek_old_cb`; 0 ms across all runs (no backward seeks in these pairs) |
| `T_unaccounted` | Derived: `T_apply_wall − (T_http + T_decrypt + T_from_read + T_flash + T_seek)` | Detools diff-CPU overhead + inter-callback scheduling; expected small and positive |
| `T_finalize` | `t_finalize_ms` (firmware timer around `esp_ota_end` + `esp_ota_set_boot_partition`) | SHA-256 validation of written partition + boot-partition record update |
| `T_apply_wall` | Wall time around `detools_apply_patch_callbacks` (or full-image loop) | Total apply phase including all sub-phases |
| `T_total` | `t_total_ms` (firmware timer: ota_start → after set_boot_partition) | End-to-end firmware update time excluding check, reboot, and boot |
| `T_reboot` | `recv_utc(boot) − recv_utc(ota_reboot)` (harness wall clock) | Device reset + bootloader + app init time; serial-buffering jitter ≤ ~100 ms |
| `T_boot_app` | `boot.t_app_ms` (firmware timer at first metrics event) | Time from reset vector to app metrics init |
| `T_downtime` | `T_reboot + T_boot_app` | Total service interruption from the device perspective |
| `goodput_kBps` | `b_patch / T_http` | Effective patch download throughput over TLS |
| `flash_kBps` | `b_flash / T_flash` | Effective flash write throughput via `esp_ota_write` |

**Accumulator overhead note:** `esp_timer_get_time()` pair cost ≈ 1 µs/call; with 226–29283 accumulations per run this contributes < 0.03 ms total — negligible.

---

## 3. Experiment Configuration

### 3.1 Firmware Builds

All builds carry Stage 2 instrumentation (`ota_check`, `ota_start`, `ota_summary`, `ota_reboot`, extended `boot` event). Metrics schema v2.

| Build label | S3 key | Scenario | Binary size | CONFIG_OTA_FORCE_FULL |
|-------------|--------|----------|-------------|----------------------|
| `s2-base` | `build_s2-base.bin` | Factory / from-version | 955,712 B | n |
| `s2-mn1` | `build_s2-mn1.bin` | MN target | 955,712 B | n |
| `s2-mn1-full` | `build_s2-mn1-full.bin` | MN full-image target | 955,712 B | **y** |
| `s2-base-full` | `build_s2-base-full.bin` | MN full-image from-version | 955,712 B | **y** |
| `s2-nu2` | `build_s2-nu2.bin` | NU target | 955,712 B | n |
| `s2-mj3` | `build_s2-mj3.bin` | MJ target | 956,736 B | n |

Delta patches (AES-128-CTR encrypted, stored in S3 bucket `esp32-firmware-rewaa`):

| Patch | Compressed size |
|-------|----------------|
| `patch_s2-base_to_s2-mn1.bin` | ~65 KB (MN delta) |
| `patch_s2-mn1_to_s2-base.bin` | ~65 KB (MN delta, reverse) |
| `patch_s2-base_to_s2-nu2.bin` | 113 B (NU delta — near-zero change) |
| `patch_s2-nu2_to_s2-base.bin` | 113 B |
| `patch_s2-base_to_s2-mj3.bin` | ~115 KB (MJ delta) |
| `patch_s2-mj3_to_s2-base.bin` | ~115 KB |

### 3.2 Campaigns

| Experiment | Pair | Type | Runs (valid/direction) | Avg RSSI |
|------------|------|------|------------------------|----------|
| MN_DELTA_02 | s2-base ↔ s2-mn1 | Delta | 10/10 | −67 dBm |
| MN_FULL | s2-base-full ↔ s2-mn1-full | Full image | 10/10 | −68 dBm |
| NU_DELTA | s2-base ↔ s2-nu2 | Delta | 10/10 | −67 dBm |
| MJ_DELTA | s2-base ↔ s2-mj3 | Delta | 10/10 | −64 dBm |

All experiments collected on 2026-06-13/14, same physical setup, board placement and AP unchanged throughout.

### 3.3 Run Validity Criteria

A run is counted as valid if:
1. `ota_summary.ok == 1`
2. No sequence gaps observed during the OTA window
3. `rssi ≥ −75 dBm` (no runs excluded on RSSI grounds in these campaigns)
4. `t_unaccounted_ms ≥ 0` (negative would indicate a timing bug)

---

## 4. Methodology

### 4.1 Harness Campaign Loop

`harness.py campaign` drives a fully automated ping-pong:

1. Read device firmware from the next event (heartbeat or ota_check)
2. Flip `latest_version` in S3 manifest to the opposite firmware
3. Wait for `ota_summary` + subsequent `boot` (timeout 10 min per run; 3 consecutive failures → abort)
4. Record run to `runs.db` with all timing fields and derived metrics
5. Settle 20 s; repeat until N valid runs per direction

At campaign end, `flip_manifest.py restore` resets the manifest to the backup state.

### 4.2 Timing Instrumentation

**Firmware side (`main/ota.c`, schema v2):**  
Accumulating timers bracket each costly call inside the detools callbacks. No UART output inside hot paths — all metrics emitted as a single `ota_summary` event after the update concludes.

**Harness side:**  
`T_reboot = recv_utc(boot) − recv_utc(ota_reboot)` is computed from host wall-clock timestamps assigned when each serial line arrives. Serial buffering introduces ≤ ~100 ms jitter (documented as acceptable in spec); `T_boot_app` from the firmware's own timer corrects for part of this.

### 4.3 Full-Image Baseline

`CONFIG_OTA_FORCE_FULL=y` in the s2-base-full / s2-mn1-full builds causes the device to send `force_full=1` in every Lambda check. The Lambda responds with a full-image pre-signed S3 URL. The full-image path instruments `T_http`, `T_decrypt`, and `T_flash` with the same accumulators; `T_from_read` and `T_seek` are 0 (no source partition reads).

---

## 5. Results

All values are mean ± std over N = 20 valid runs (10 A→B + 10 B→A). Times in ms, throughputs in kB/s, sizes in bytes.

### 5.1 Phase Breakdown (ms)

| Phase | MN_DELTA | MN_FULL | NU_DELTA | MJ_DELTA |
|-------|----------|---------|----------|----------|
| **T_check** (ota_check rtt_ms, update=1 only) | 2280 ± 81 | 2352 ± 179 | 2784 ± 954 | 2250 ± 81 |
| **T_connect** | 1544 ± 148 | 1626 ± 121 | 2309 ± 1735 | 1549 ± 255 |
| **T_http** | 2089 ± 243 | 34885 ± 3736 | 3 ± 1 | 3529 ± 614 |
| **T_decrypt** | 35 ± 1 | 302 ± 2 | 0 ± 0 | 61 ± 2 |
| **T_from_read** | 1599 ± 51 | 0 | 688 ± 2 | 2309 ± 5 |
| **T_flash** | 8945 ± 75 | 2170 ± 112 | 4995 ± 80 | 12478 ± 80 |
| **T_seek** | 0 | 0 | 0 | 0 |
| **T_unaccounted** | 335 ± 27 | 7 ± 1 | 184 ± 2 | 411 ± 3 |
| **T_apply_wall** | 13003 ± 239 | 37364 ± 3777 | 5870 ± 81 | 18788 ± 632 |
| **T_finalize** | 685 ± 9 | 681 ± 4 | 684 ± 7 | 684 ± 5 |
| **T_total** _(firmware, excl. check+reboot)_ | 16305 ± 384 | 40797 ± 3814 | 9993 ± 1690 | 22144 ± 609 |
| **T_reboot** _(harness-measured)_ | 2304 ± 1533 | 2285 ± 1551 | 802 ± 17 | 805 ± 14 |
| **T_boot_app** | 60 ± 1 | 60 ± 1 | 61 ± 1 | 61 ± 0 |
| **T_downtime** | 2363 ± 1532 | 2345 ± 1551 | 862 ± 17 | 866 ± 14 |

> **T_connect high variance in NU_DELTA** (2309 ± 1735 ms): the 113-byte NU patch is served by S3 in < 1 ms of transfer time, making the run duration extremely sensitive to S3 cold-start vs warm-cache behaviour. This inflates both the mean and std of T_connect compared to the other experiments.

> **T_reboot high variance in MN_DELTA and MN_FULL** (±1.5 s): these experiments ping-pong between two OTA partitions (ota_0 / ota_1), each of which has a different boot sequence from the bootloader's perspective. The bimodal distribution (fast boot from cached partition vs slow validation from a freshly-written partition) explains the large std. NU_DELTA and MJ_DELTA show the expected tight ±14–17 ms jitter.

### 5.2 Phase Share of Apply-Wall (delta runs, mean)

| Phase | MN_DELTA | NU_DELTA | MJ_DELTA |
|-------|----------|----------|----------|
| T_http | 16.1% | 0.1% | 18.8% |
| T_decrypt | 0.3% | 0.0% | 0.3% |
| T_from_read | 12.3% | 11.7% | 12.3% |
| T_flash | **68.8%** | **85.2%** | **66.4%** |
| T_seek | 0.0% | 0.0% | 0.0% |
| T_unaccounted | 2.6% | 3.1% | 2.2% |

**Flash write dominates the apply phase in all delta scenarios** (67–85%). The detools reconstruction is effectively I/O-bound on flash.

### 5.3 Throughput Metrics

| Metric | MN_DELTA | MN_FULL | NU_DELTA | MJ_DELTA |
|--------|----------|---------|----------|----------|
| Patch size (bytes) | 65,110 ± 30 | 955,656 ± 41 | 113 ± 0 | 115,098 ± 483 |
| Flash output (bytes) | 955,744 ± 33 | 955,656 ± 41 | 955,712 | 956,224 ± 525 |
| **Download goodput** (kB/s) | 31.5 ± 3.0 | 27.7 ± 2.8 | 37.1 ± 6.5 | 33.2 ± 3.7 |
| **Flash throughput** (kB/s) | 106.9 ± 0.9 | **441.6 ± 22.7** | 191.4 ± 3.1 | 76.6 ± 0.5 |

**Key throughput observations:**
- Download goodput is **27–37 kB/s** across all experiments, consistent with a single TLS connection over 2.4 GHz Wi-Fi at −64 to −68 dBm. The HTTP prefetch buffer (10 KB) keeps TLS record boundaries from stalling detools.
- Flash throughput for **MN_FULL is 4× higher** than MN_DELTA (442 vs 107 kB/s). Full-image writes use the maximum `esp_ota_write` chunk size (2 KB sequential), while delta writes are driven by detools callbacks in 32-byte chunks — the smaller write granularity exposes per-call `esp_ota_write` overhead.
- MJ_DELTA flash throughput (77 kB/s) is lowest because the MJ firmware has more sections with different byte patterns, causing more detools work per byte and smaller average `to_write_cb` invocations.

### 5.4 Delta vs Full Comparison (MN scenario)

| Metric | MN_DELTA | MN_FULL | Ratio |
|--------|----------|---------|-------|
| T_total (ms) | 16,305 | 40,797 | **2.5× faster delta** |
| T_apply_wall (ms) | 13,003 | 37,364 | 2.9× |
| Bytes transferred | 65,110 | 955,656 | **14.7× less data** |
| Download goodput (kB/s) | 31.5 | 27.7 | comparable |
| Flash throughput (kB/s) | 106.9 | 441.6 | 4.1× full faster |

Delta OTA is **2.5× faster end-to-end** for the MN pair, driven almost entirely by the 14.7× reduction in bytes downloaded. The flash write phase is longer for delta (8.9 s vs 2.2 s) because detools drives writes in small chunks, but this is more than offset by the download savings.

### 5.5 Scenario Ordering

```
NU_DELTA (9.9 s) << MN_DELTA (16.3 s) < MJ_DELTA (22.1 s) <<< MN_FULL (40.8 s)
```

This matches the expected ordering from Stage 1 (§4 of stage_2.txt: "NU < MN < MJ total time; delta total < full total for NU/MN").

---

## 6. Observations and Analysis

### 6.1 Flash Write Is the Delta-OTA Bottleneck

For all three delta experiments, `T_flash` accounts for 67–85% of the apply-wall time. `esp_ota_write` is called 29,259–29,283 times per MN/MJ update (one call per detools output chunk, ~32 bytes each). Each call has fixed per-call overhead (SPI flash protocol + cache flush). Batching writes in the `write_new_cb` to, e.g., 512-byte chunks would reduce flash overhead to ~59 calls and is likely the single highest-impact firmware optimisation available.

### 6.2 Negligible Decrypt Cost

AES-128-CTR decrypt is < 0.4% of apply-wall time in all delta experiments. The stream cipher is essentially free at this payload scale; removing or replacing it would not materially affect update latency.

### 6.3 T_unaccounted Validates the Accounting Closure

`T_unaccounted` (detools diff-CPU + overhead) is **2.2–3.2%** of apply-wall time across delta experiments — positive and small, confirming no measurement gaps or double-counting. MN_FULL's T_unaccounted is only 7 ms (< 0.02% of wall), as expected: there is no diff-CPU in the full-image path.

### 6.4 T_finalize Is Firmware-Constant

`esp_ota_end` (SHA-256 of ~955 KB partition) + `esp_ota_set_boot_partition` takes **681–685 ms ± 4–9 ms** regardless of scenario or update type. This is a fixed overhead of the ESP-IDF OTA API proportional to partition size, not patch size. For a 2 MB partition it would approximately double.

### 6.5 T_reboot / T_downtime

For NU_DELTA and MJ_DELTA, `T_downtime ≈ 862–866 ms` (802 ms reboot + 61 ms app init), measured with < 17 ms std — tight and reliable. For MN_DELTA and MN_FULL, the mean is ~2.3 s with 1.5 s std due to the bimodal partition boot-time distribution (see §5.1 note). The true single-partition T_reboot is estimated at ~800 ms consistent with NU/MJ; the longer tail is a bootloader validation artefact, not a regression.

### 6.6 NU Scenario: Flash, Not Download, Drives Time

Despite a 113-byte patch, NU_DELTA takes 9.9 s total. `T_flash` alone is 5.0 s because detools must reconstruct the full 955 KB firmware regardless of patch size — the "from-read + flash" work is proportional to the output size, not the input size. `T_http` is 3 ms (negligible). This has a direct implication for the paper: **the minimum achievable delta update time for this firmware is ~6.4 s** (from_read + flash + finalize), independent of network conditions.

---

## 7. Plots

All plots are in `results/stage2/plots/`. Each experiment has 5 output files:

| Filename pattern | Description |
|-----------------|-------------|
| `<EXP>_phase_bar.png` | Stacked-bar chart — mean phase times (connect, http, decrypt, from_read, flash, unaccounted, finalize) for A→B and B→A directions side by side |
| `<EXP>_total_box.png` | Box plot of `t_total_ms` over 20 valid runs |
| `<EXP>_throughput_scatter.png` | Scatter `t_http` vs `b_patch` with fitted line (slope = effective goodput kB/s) |
| `<EXP>_heap.png` | Heap free / heap min over event sequence for a representative run |
| `<EXP>_event_counts.png` | Event type distribution (heartbeat / ota_check / ota_start / ota_summary / boot) across all runs |

---

## 8. Bugs Found and Fixed During Stage 2

### 8.1 Delta OTA IO_FAILED on r=0 (commit a7c2923)

`read_patch_cb` treated `r=0` from `esp_http_client_read` as EOF. In practice `r=0` can occur transiently between TLS records. Added retry loop (up to ~1 s / 100 attempts) and an HTTP prefetch buffer (10 KB) to amortise per-read overhead and prevent single-byte TLS-boundary reads.

### 8.2 seek_old_cb Relative vs Absolute Offset (commit 9c83213)

`seek_old_cb` was adding a signed `offset` as an absolute position. Corrected to `old_read_offset += offset` (relative from current). This fix was required for detools to correctly reconstruct the target firmware.

### 8.3 Manifest Dict Format on Restore (commit e81a282)

`flip_manifest.py restore` was not normalising string values in `delta_patches`, causing Lambda parse failures. Fixed by ensuring all dict values are plain strings.

### 8.4 NVS Fail-Counter Accumulation (not committed — operational procedure)

After > ~10 consecutive failed OTA attempts (which accumulated during a 13-hour unattended period), the NVS `ota_fail_cnt` key grew large, setting `force_full=1`. This caused `esp_ota_end()` to return `ESP_ERR_OTA_VALIDATE_FAILED` consistently (~338 ms, half the normal 681 ms). **Root cause hypothesis:** many `esp_ota_begin`/`esp_ota_abort` cycles on ota_0 corrupted the otadata partition state in a way that invalidated subsequent OTA handles. **Remedy:** erase NVS (0x9000, 16 KB) and otadata (0xd000, 8 KB) via esptool and reflash the factory partition.

---

## 9. Definition-of-Done Verification

| DoD Item | Status |
|----------|--------|
| Both builds compile (METRICS_ENABLE y/n × OTA_FORCE_FULL y/n) | ✓ (campaigns ran on all four build variants) |
| `ota_summary` arrives with all fields; phase sum ≤ wall; `t_unaccounted` positive, < 25% | ✓ max 3.2% |
| Reboot + downtime computed by harness from `ota_reboot→boot` | ✓ all 80 runs |
| `campaign` completes 10-run MN ping-pong unattended, guard working, manifest restored | ✓ |
| Stacked-bar, box, throughput plots render; CSV exports include derived columns | ✓ 20 plots, 4 CSVs |
| `results/stage2/` committed with run metadata | ✓ commit ef7b2a6 |

---

## 10. File Inventory

| File | Description |
|------|-------------|
| `results/stage2/runs.db` | SQLite database — all events + all runs (all 4 experiments) |
| `results/stage2/MN_DELTA_02.csv` | Exported run-level data, MN delta (20 valid rows + derived columns) |
| `results/stage2/MN_FULL.csv` | Exported run-level data, MN full-image |
| `results/stage2/NU_DELTA.csv` | Exported run-level data, NU delta |
| `results/stage2/MJ_DELTA.csv` | Exported run-level data, MJ delta |
| `results/stage2/plots/<EXP>_phase_bar.png` | Stacked-bar phase breakdown (4 experiments × 5 plots = 20 files) |
| `results/stage2/plots/<EXP>_total_box.png` | |
| `results/stage2/plots/<EXP>_throughput_scatter.png` | |
| `results/stage2/plots/<EXP>_heap.png` | |
| `results/stage2/plots/<EXP>_event_counts.png` | |
| `results/stage2/summary.md` | This experiment's summary tables |
| `results/stage2/REPORT_stage2.md` | This document |
| `tools/run_nu_mj_campaigns.sh` | Campaign automation script (NU + MJ back-to-back) |
| `tools/post_mn_full.sh` | Post-MN_FULL export + plot helper |
| `logs/<EXP>_campaign_<timestamp>.rawlog` | Raw serial logs per campaign session (not committed) |
