# Stage 4 Summary — Cloud-Side Metrics

**Branch:** `feat/runtim-mesurments` | **Date:** 2026-06-15 | **Region:** us-east-1  
Lambda: `Firmware-Validator-Hash` (128 MB, Python 3.14) | Bucket: `esp32-firmware-rewaa`

---

## Lambda Performance (N = 753 invocations, campaign window June 11–15 2026)

| Metric | Warm starts (N=705) | Cold starts (N=48) |
|--------|---------------------|--------------------|
| Exec Duration (ms) | **107.7 ± 94.1** | **334.4 ± 21.0** |
| Init Duration (ms) | — | **563.6 ± 120.7** |
| Total Billed (ms) | **108.2 ± 94.1** | **898.5 ± 118.9** |
| Max Memory Used (MB) | **95.3 ± 1.0** | **95.4 ± 1.1** |
| Cold-start fraction | — | **6.4%** (48/753) |
| Cold / Warm exec ratio | — | **3.1×** |

### Device-Observed RTT (Stage 2/3, N=280 ota_check events)

| Metric | Value |
|--------|-------|
| Mean rtt_ms | **2 335 ± 376 ms** |
| Min / Max | 2 043 / 5 652 ms |
| Lambda share of RTT | ~5% (warm) |
| Network + API GW overhead | **~2 228 ms (95% of RTT)** |

---

## CI Patch-Generation Scaling

| k (prior versions) | Time (s) | ± Std |
|--------------------|----------|-------|
| 1 | 0.584 | 0.045 |
| 2 | 1.139 | 0.005 |
| 4 | 2.279 | 0.017 |
| 8 | 4.595 | 0.054 |
| 16 | 9.177 | 0.185 |

**Linear fit: t ≈ 0.574 × k** — confirmed O(n) growth. At k = 40 versions (current): ~23 s; at k = 100: ~57 s.

---

## S3 Storage (current, 2026-06-15)

| Category | Count | Size |
|----------|-------|------|
| Full binaries (CI) | 40 | 38.5 MB |
| Full binaries (corpus) | 11 | 10.0 MB |
| Delta patches | 814 | 431.3 MB |
| Manifests | 2 | 7.8 KB |
| **Grand total** | **867** | **479.8 MB** |

Avg full binary: **985 KB** | Avg patch: **543 KB**  
Patch growth is **O(n²)** — at 100 versions → ~2.7 GB total.

---

## Cost Model (MODEL ESTIMATES — us-east-1, 2026-06-15 AWS pricing)

### Per Device Per Update

| | Delta OTA | Full OTA |
|--|-----------|----------|
| Lambda | $0.0000009 | $0.0000009 |
| API Gateway | $0.0000010 | $0.0000010 |
| S3 GET (×2) | $0.0000008 | $0.0000008 |
| S3 Transfer | $0.0000055 | $0.0000801 |
| **TOTAL** | **$0.0000082** | **$0.0000824** |

### Per 1,000-Device Rollout

| | Delta OTA | Full OTA | Ratio |
|--|-----------|----------|-------|
| Raw cost | **$0.0078** | **$0.0824** | **10.6×** |
| After free tier | $0.0058 | $0.0804 | — |
| Data transferred | 0.061 GB | 0.890 GB | 14.7× |

**Key insight:** S3 egress dominates cost. Delta reduces the per-rollout transfer cost by 14.7× and total cost by 10.6×.

---

## Key Findings

- **Lambda execution is not the bottleneck.** Warm starts take 108 ms, but the device sees 2 335 ms RTT — 95% is network + TLS + API Gateway overhead.
- **Cold starts cost 8.3× more in billed duration** (899 ms vs 108 ms) but occur only 6.4% of the time, contributing modestly to the blended cost.
- **Patch-gen CI step scales linearly** (0.574 s/version). At 40 versions this is ~23 s of CI time; the O(n) growth is measurable but manageable below ~150 versions.
- **S3 patch storage grows O(n²)** under the all-pairs scheme (one patch per old→new pair per release). At 100 versions the patch store reaches ~2.6 GB — a retention policy becomes necessary at fleet-history scale.
- **Delta OTA is 10.6× cheaper per rollout** than full-image OTA, with the margin driven entirely by S3 egress ($0.0055 vs $0.0801 per 1 000 devices). Lambda and API Gateway costs are negligible and identical for both paths.
- **Free-tier credits cover ~$0.002 per 1 000-device rollout** regardless of delta/full, saving ~26% for delta and only ~2.4% for full — the transfer cost dwarfs all other components for full-image updates.

---

## Run Metadata

| Item | Value |
|------|-------|
| Campaign window queried | 2026-06-11 00:00 UTC → 2026-06-15 (now) |
| Lambda REPORT rows | 753 |
| Patchgen repeats per k | 3 |
| S3 objects listed | 867 |
| Cost model inputs | Stage 2 MN_DELTA patch sizes; measured Lambda billed durations |
| AWS pricing date | 2026-06-15 |
| Pricing source | See `tools/cost_model.py` PRICING dict |

## Output Files

| File | Description |
|------|-------------|
| `data/lambda_reports.csv` | Raw REPORT lines (753 rows) |
| `data/lambda_summary.json` | Warm/cold stats + rtt_ms stats |
| `data/s3_objects.csv` | All 867 S3 objects with category |
| `data/storage_summary.json` | S3 footprint by category |
| `data/patchgen_bench.csv` | Timing results for k sweep |
| `data/patchgen_summary.json` | Scaling summary + linear fit |
| `data/cost_model.json` | Full cost model (per-update + per-rollout) |
| `plots/plot1_lambda_duration.png` | Lambda cold vs warm + rtt_ms overlay |
| `plots/plot2_patchgen_scaling.png` | Patch-gen time vs k + O(n) fit |
| `plots/plot3_rollout_cost.png` | Cost per 1 000-device rollout breakdown |
