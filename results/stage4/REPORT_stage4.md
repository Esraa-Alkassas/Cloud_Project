# Stage 4 Report — Cloud-Side Metrics

**Branch:** `feat/runtim-mesurments`  
**Date:** 2026-06-15  
**Region:** us-east-1  
**Author:** Omar Mokhtar  

---

## 1. Objective

Stage 4 quantifies the cloud orchestration cost of the serverless OTA pipeline — the axis that prior embedded-OTA work omits. Four questions are answered:

1. **Lambda performance** — warm vs cold start duration, memory footprint, device-observed RTT.
2. **CI patch-generation scaling** — how patch-gen time grows with the number of historical versions (the O(n) story).
3. **S3 storage growth** — current footprint and projection as the fleet history accumulates.
4. **Cost model** — $ per update and $ per 1,000-device rollout under measured inputs and published AWS pricing.

No firmware changes were made. No new campaigns were run. Device-side inputs (rtt_ms, patch sizes) were drawn from Stage 2/3 data.

---

## 2. Method & Tools

| Tool | Subcommand / script | What it does |
|------|---------------------|--------------|
| `tools/cloud_metrics.py lambda` | CloudWatch `filter_log_events` | Pulls all `REPORT` lines from `/aws/lambda/Firmware-Validator-Hash` over the campaign window (June 11–15 2026); parses Duration, Billed Duration, Memory Used, Init Duration |
| `tools/cloud_metrics.py storage` | S3 `list_objects_v2` | Lists all objects in `esp32-firmware-rewaa`; categorises as full binary / patch / manifest |
| `tools/patchgen_bench.py` | Local simulation | Times `detools create_patch -c crle` + AES-CTR encrypt for k = 1, 2, 4, 8, 16 prior versions using the Stage 1/3 corpus binaries |
| `tools/cost_model.py` | Pure arithmetic | Translates measured Lambda billed duration, S3 sizes, and Stage 2 patch sizes into $ per update and per 1,000-device rollout; cites AWS pricing page and date |
| `tools/plot_stage4.py` | matplotlib | Produces the three required plots |

### Defensive measures (§0.2)

- All boto3 calls use `Config(retries={"max_attempts": 5, "mode": "adaptive"})`.
- CloudWatch pagination is bounded to 20 pages (10 k events each); circuit-break on 3 consecutive empty pages.
- S3 listing is bounded to 10 pages (1 k keys each).
- Cost guard (§0.3): the stage issues **zero Lambda invocations** — all metrics are read from existing CloudWatch logs. S3 ops are read-only listings only.

---

## 3. Lambda Performance

### 3.1 Data collection

`cloud_metrics.py lambda` queried CloudWatch for the campaign window [2026-06-11, 2026-06-15]. **753 REPORT lines** were returned across 8 pagination pages.

- **705 warm starts** (no `Init Duration` in REPORT line)
- **48 cold starts** (`Init Duration` present; 6.4% of all invocations)

Raw CSV: `results/stage4/data/lambda_reports.csv`  
Summary JSON: `results/stage4/data/lambda_summary.json`

### 3.2 Warm starts

| Metric | Mean | Std | Min | Max |
|--------|------|-----|-----|-----|
| Duration (ms) | 107.7 | 94.1 | 25.5 | 424.1 |
| Billed Duration (ms) | 108.2 | 94.1 | 26.0 | 425.0 |
| Max Memory Used (MB) | 95.3 | 1.0 | 93 | 96 |

The high variance (std ≈ 87% of mean) in warm-start duration is expected: the Lambda reads `manifest.json` from S3 on every invocation, and S3 latency varies from ~20 ms to ~400 ms depending on connection reuse.

### 3.3 Cold starts

| Metric | Mean | Std | Min | Max |
|--------|------|-----|-----|-----|
| Init Duration (ms) | 563.6 | 120.7 | 439.5 | 1289.9 |
| Exec Duration (ms) | 334.4 | 21.0 | 283.9 | 396.9 |
| Billed Duration (ms) | 898.5 | 118.9 | 755.0 | 1604.0 |
| Max Memory Used (MB) | 95.4 | 1.1 | 92 | 96 |

Cold-start exec duration (334 ms) is **3.1×** the warm-start mean (108 ms). The cold-init phase (564 ms) dominates the total billed time (899 ms). The Lambda runtime is Python 3.14 with boto3 as the primary import, which accounts for the ~500 ms init overhead.

### 3.4 Device-observed RTT (from Stage 2/3 campaigns)

The `rtt_ms` column in Stage 2/3 CSVs captures the full round-trip the ESP32 sees from the `ota_check` HTTP request to response parsing.

| Metric | Value |
|--------|-------|
| N | 280 samples (across 4 experiments × 20 valid runs each) |
| Mean rtt_ms | 2 335 ± 376 ms |
| Min / Max | 2 043 / 5 652 ms |

**Network + API Gateway overhead: ~2 228 ms (95% of RTT)** — i.e., Lambda execution (108 ms warm) represents only 5% of the device's perceived latency. The dominant cost is TLS handshake + Wi-Fi + API Gateway.

> **Plot 1:** `plots/plot1_lambda_duration.png` — bar chart of warm exec, cold exec, cold (init+exec), with device rtt_ms overlaid as a dashed line.

---

## 4. CI Patch-Generation Scaling

### 4.1 Method

`patchgen_bench.py` sweeps k = 1, 2, 4, 8, 16 prior versions. For each k, it runs `detools.create_patch(..., compression="crle")` from each prior binary to the newest corpus binary (`wc-1.bin`, 924 KB), then AES-128-CTR encrypts the result using the same key/IV as CI. Three repetitions per k; timing is wall-clock of the full batch.

The 10-binary corpus (`corpus/bins/`) provides distinct prior versions; the pool is cycled for k > 9.

### 4.2 Results

| k | Time (s) | ± Std | Time / patch (s) | Mean patch (KB) |
|---|----------|-------|------------------|-----------------|
| 1 | 0.584 | 0.045 | 0.584 | 477.0 |
| 2 | 1.139 | 0.005 | 0.570 | 479.7 |
| 4 | 2.279 | 0.017 | 0.570 | 479.2 |
| 8 | 4.595 | 0.054 | 0.574 | 478.1 |
| 16 | 9.177 | 0.185 | 0.574 | 478.1 |

**Linear fit: t ≈ 0.574 × k** (R² ≈ 1.00) — confirms O(n) growth. The per-patch cost is constant at ~0.57 s regardless of k.

### 4.3 Projection

| k (prior versions) | Estimated time (s) | Estimated time (min) |
|--------------------|-------------------|----------------------|
| 25 | 14.3 | 0.24 |
| 50 | 28.7 | 0.48 |
| 100 | 57.4 | 0.96 |
| 200 | 114.7 | 1.91 |

At 40 CI versions (current bucket state), estimated patch-gen time ≈ **22.9 s**. This fits within a typical GitHub Actions job (which also includes build time). At 100 versions it approaches 1 minute of pure patch-gen CPU time per release — still tractable, but the O(n) accumulation is the dominant scaling concern for long-lived fleets.

> **Plot 2:** `plots/plot2_patchgen_scaling.png` — line chart of measured time vs k with linear projection to k = 100.

---

## 5. S3 Storage Growth

### 5.1 Current footprint

`cloud_metrics.py storage` ran one `list_objects_v2` pass over `esp32-firmware-rewaa` (8 pages, bounded). **867 objects found.**

| Category | Count | Total size |
|----------|-------|------------|
| Full binaries (CI builds) | 40 | 38.47 MB |
| Full binaries (corpus: s2-/s3-) | 11 | 10.03 MB |
| Delta patches | 814 | 431.32 MB |
| Manifests | 2 | 7.8 KB |
| **Grand total** | **867** | **479.82 MB** |

Average full binary: **985 KB**  
Average patch size: **543 KB**

With 40 CI versions, the bucket holds 814 patches = 40 × (40−1)/2 ≈ 780 (all-pairs)— the surplus 34 patches include corpus-to-corpus pairs generated during Stage 1/2 experiments.

### 5.2 Growth projection (model)

Growth is **O(n)** in binaries (linear) and **O(n²)** in patches (all-pairs per release):

| Versions | Full binaries | Patches (n(n-1)/2) | Total |
|----------|--------------|---------------------|-------|
| 10 | 9.6 MB | 45 × 543 KB = 23.8 MB | 33.5 MB |
| 20 | 19.2 MB | 190 × 543 KB = 100.7 MB | 119.9 MB |
| 50 | 48.1 MB | 1225 × 543 KB = 649.1 MB | 697.2 MB |
| 100 | 96.2 MB | 4950 × 543 KB = 2623 MB | 2719 MB |

The O(n²) patch accumulation is the dominant storage concern at scale. A retention policy (keep only the last M versions' patches) would reduce this to O(M×n).

---

## 6. Cost Model

### 6.1 Inputs and assumptions

| Parameter | Value | Source |
|-----------|-------|--------|
| Lambda memory | 128 MB | AWS console |
| Warm billed duration | 108.2 ms | Measured (N=705) |
| Cold billed duration | 898.5 ms | Measured (N=48) |
| Cold-start fraction | 6.4% | Measured (48/753) |
| Blended billed duration | 158.8 ms | Weighted average |
| Delta patch size | 65 110 B | Stage 2 MN_DELTA mean |
| Full image size | 955 656 B | Stage 2 MN_FULL mean |
| AWS pricing | us-east-1, 2026-06-15 | See [pricing URLs in `cost_model.py`] |

Per-update model: 1 Lambda invocation (via API Gateway) + 1 S3 GET (manifest) + 1 S3 GET (download) + data-transfer-out for the payload.

### 6.2 Per-device per-update cost

| Component | Delta OTA | Full OTA |
|-----------|-----------|----------|
| Lambda | $0.000001 | $0.000001 |
| API Gateway | $0.000001 | $0.000001 |
| S3 GET (×2) | $0.000001 | $0.000001 |
| S3 Transfer out | $0.000005 | $0.000080 |
| **TOTAL** | **$0.000008** | **$0.000082** |
| Bytes transferred | 63.6 KB | 933.3 KB |

### 6.3 Per-1,000-device rollout

| Component | Delta OTA | Full OTA |
|-----------|-----------|----------|
| Lambda | $0.0005 | $0.0005 |
| API Gateway | $0.0010 | $0.0010 |
| S3 GET | $0.0008 | $0.0008 |
| S3 Transfer | $0.0055 | $0.0801 |
| **Raw total** | **$0.0078** | **$0.0824** |
| After free-tier credits | $0.0058 | $0.0804 |

**Delta OTA is 10.6× cheaper than full-image OTA** per rollout, driven almost entirely by the S3 transfer cost (63 KB vs 933 KB per device).

> **Plot 3:** `plots/plot3_rollout_cost.png` — stacked bar chart: delta vs full, broken into Lambda / API GW / S3 GET / S3 Transfer.

All figures are **MODEL ESTIMATES**. Free-tier calculation assumes this is the only workload in the billing month. Data transfer cost assumes S3 presigned URLs without CloudFront.

---

## 7. Notes for Write-Up (carried from stage_4.txt §8)

- **Stage 3 "MN_FULL" label:** The "MN_FULL" experiment in Stage 3 was a second delta pair (s3-base → s3-mn1 using the patching path), not a true full-image baseline. The full-image reference lives in Stage 2. Label accordingly in the paper.
- **Mean bytes/write discrepancy:** Stage 2 reports ~32 B/write; Stage 3 reports ~50 B/write. The difference reflects a narrower counting boundary in Stage 3 (only OTA-write calls, excluding metadata writes counted in Stage 2). Reconcile in one sentence in the methods section.

---

## 8. Definition-of-Done Checklist

- [x] `cloud_metrics.py lambda` pulls and summarises Lambda REPORT metrics (warm vs cold, duration, max memory used); device `rtt_ms` joined for context.
- [x] `patchgen_bench.py` produces the time-vs-k scaling curve (k up to 16), mirroring real CI create_patch + encrypt steps.
- [x] `cloud_metrics.py storage` reports S3 footprint by category with growth projection.
- [x] `cost_model.py` outputs per-update and per-1,000-rollout cost (delta vs full), with region/pricing/date and free-tier assumptions stated as a model.
- [x] All §0.2 supervision present (timeouts via retry config, pagination cap, circuit breaker on empty pages, cost guard §0.3 — zero Lambda invocations issued).
- [x] Three plots rendered: `plot1_lambda_duration.png`, `plot2_patchgen_scaling.png`, `plot3_rollout_cost.png`.
- [x] `REPORT_stage4.md` and `summary.md` written and consistent with CSVs/JSONs.
- [x] All work on `feat/runtim-mesurments`.

**Final commit:** (see `git log` on `feat/runtim-mesurments` after this commit)
