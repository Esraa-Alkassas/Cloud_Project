# Stage 2 Summary — On-Device OTA Timing

N = 20 valid runs per experiment (10 A→B + 10 B→A). All times in **ms**, throughputs in **kB/s**.  
Branch: `feat/stage2-timing` | Date: 2026-06-14

---

## Phase Breakdown (mean ± std, ms)

| Phase | MN_DELTA | MN_FULL | NU_DELTA | MJ_DELTA |
|-------|----------|---------|----------|----------|
| T_check (rtt_ms, update=1) | 2280 ± 81 | 2352 ± 179 | 2784 ± 954 | 2250 ± 81 |
| T_connect | 1544 ± 148 | 1626 ± 121 | 2309 ± 1735 | 1549 ± 255 |
| T_http | 2089 ± 243 | 34885 ± 3736 | 3 ± 1 | 3529 ± 614 |
| T_decrypt | 35 ± 1 | 302 ± 2 | 0 ± 0 | 61 ± 2 |
| T_from_read | 1599 ± 51 | 0 | 688 ± 2 | 2309 ± 5 |
| T_flash | 8945 ± 75 | 2170 ± 112 | 4995 ± 80 | 12478 ± 80 |
| T_seek | 0 | 0 | 0 | 0 |
| T_unaccounted | 335 ± 27 | 7 ± 1 | 184 ± 2 | 411 ± 3 |
| **T_apply_wall** | **13003 ± 239** | **37364 ± 3777** | **5870 ± 81** | **18788 ± 632** |
| T_finalize | 685 ± 9 | 681 ± 4 | 684 ± 7 | 684 ± 5 |
| **T_total** _(firmware, excl. check+reboot)_ | **16305 ± 384** | **40797 ± 3814** | **9993 ± 1690** | **22144 ± 609** |
| T_reboot _(harness)_ | 2304 ± 1533 | 2285 ± 1551 | 802 ± 17 | 805 ± 14 |
| T_boot_app | 60 ± 1 | 60 ± 1 | 61 ± 1 | 61 ± 0 |
| **T_downtime** | **2363 ± 1532** | **2345 ± 1551** | **862 ± 17** | **866 ± 14** |

---

## Throughput and Size

| Metric | MN_DELTA | MN_FULL | NU_DELTA | MJ_DELTA |
|--------|----------|---------|----------|----------|
| Patch size (bytes) | 65,110 ± 30 | 955,656 ± 41 | 113 | 115,098 ± 483 |
| Flash output (bytes) | 955,744 ± 33 | 955,656 ± 41 | 955,712 | 956,224 ± 525 |
| Download goodput (kB/s) | 31.5 ± 3.0 | 27.7 ± 2.8 | 37.1 ± 6.5 | 33.2 ± 3.7 |
| Flash throughput (kB/s) | 106.9 ± 0.9 | 441.6 ± 22.7 | 191.4 ± 3.1 | 76.6 ± 0.5 |

---

## Flash Share of Apply-Wall (delta only)

| Experiment | T_flash / T_apply_wall |
|------------|------------------------|
| MN_DELTA | 68.8% |
| NU_DELTA | 85.2% |
| MJ_DELTA | 66.4% |

---

## Delta vs Full (MN Scenario)

| | MN_DELTA | MN_FULL | Ratio |
|--|----------|---------|-------|
| T_total (ms) | 16,305 | 40,797 | 2.5× faster |
| Bytes transferred | 65,110 | 955,656 | 14.7× less data |
| Flash throughput (kB/s) | 106.9 | 441.6 | full is 4.1× faster to flash |

---

## Key Findings

- **Flash write is the delta-OTA bottleneck**: 67–85% of apply-wall time, driven by 29 K small `esp_ota_write` calls (≈ 32 B/call). Batching to 512 B/call is the highest-impact firmware optimisation available.
- **Delta is 2.5× faster end-to-end** than full-image OTA for the MN pair (16.3 s vs 40.8 s).
- **Minimum delta update floor ≈ 6.4 s** (from_read + flash + finalize), independent of network and patch size — demonstrated by NU_DELTA's 113-byte patch still taking 9.9 s total.
- **T_finalize is constant at 681–685 ms** across all experiments: SHA-256 of the ~955 KB OTA partition is a fixed cost of the ESP-IDF OTA API.
- **AES-128-CTR decrypt is negligible** (< 0.4% of apply-wall), making encryption effectively free.
- **T_unaccounted is 2.2–3.2%** of apply-wall (positive, all runs): good timing closure; the residual is detools diff-CPU overhead.
- **Download goodput 27–37 kB/s** is consistent with a single TLS connection over 2.4 GHz Wi-Fi at the measured RSSI range.
