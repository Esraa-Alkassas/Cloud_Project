#!/usr/bin/env python3
"""
Stage 4 — cost_model.py
Pure arithmetic cost model for the serverless OTA pipeline.
No AWS API calls — inputs from measured Stage 2/3/4 data.

AWS pricing as of 2026-06-15, us-east-1:
  https://aws.amazon.com/lambda/pricing/
  https://aws.amazon.com/s3/pricing/
  https://aws.amazon.com/api-gateway/pricing/
"""

import json
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "results" / "stage4" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── AWS Pricing (us-east-1, 2026-06-15) ──────────────────────────────────────
# Lambda:  $0.20 per 1 M requests + $0.0000166667 per GB-second
# S3:      $0.023 per GB-month (standard storage)
#          $0.0004 per 1 000 GET requests
#          $0.005  per 1 000 PUT/COPY/POST requests
#          $0.09 per GB data-transfer-out (first 10 TB/month)
# API GW:  $1.00 per million HTTP API calls (HTTP API, us-east-1)
#          (REST API is $3.50/M but we use HTTP API based on SAM template)
# Free-tier (per month, first 12 months):
#   Lambda:  1 M requests, 400 000 GB-s
#   S3:      5 GB storage, 20 000 GET, 2 000 PUT
#   API GW:  1 M API calls (HTTP)

PRICING = {
    # Lambda
    "lambda_req_per_M":          0.20,         # $/M requests
    "lambda_gb_s":               0.0000166667, # $/GB-second
    # API Gateway (HTTP API)
    "apigw_req_per_M":           1.00,         # $/M calls
    # S3
    "s3_storage_gb_month":       0.023,        # $/GB-month
    "s3_get_per_1k":             0.0004,       # $/1 000 GETs
    "s3_put_per_1k":             0.005,        # $/1 000 PUTs
    "s3_transfer_out_gb":        0.09,         # $/GB transferred out
    # Free-tier caps (monthly)
    "free_lambda_req_M":         1.0,
    "free_lambda_gb_s":          400_000.0,
    "free_s3_storage_gb":        5.0,
    "free_s3_get_1k":            20.0,         # i.e. 20 000 GETs
    "free_s3_put_1k":            2.0,          # i.e. 2 000 PUTs
    "free_apigw_req_M":          1.0,
    "_pricing_url":  "https://aws.amazon.com/lambda/pricing/ "
                     "https://aws.amazon.com/s3/pricing/ "
                     "https://aws.amazon.com/api-gateway/pricing/",
    "_pricing_date": "2026-06-15",
    "_region":       "us-east-1",
}

# ── Measured inputs (from Stage 2/3/4 data) ──────────────────────────────────
# Lambda — from cloud_metrics.py lambda (N=753 invocations from campaign window)
LAMBDA_BILLED_MS_WARM   =  108.22  # mean billed duration, warm start (ms, N=705)
LAMBDA_BILLED_MS_COLD   =  898.52  # mean billed duration, cold start (ms, N=48; init+exec blended)
LAMBDA_MEM_MB           =  128.0   # configured memory

# Patch sizes (mean from Stage 2 campaigns)
DELTA_PATCH_BYTES       =  65_110  # MN_DELTA mean patch size (bytes)
FULL_IMAGE_BYTES        = 955_656  # mean full binary size (bytes)

# S3 access pattern per device check
S3_GETS_PER_CHECK       = 1        # manifest.json read
S3_GETS_PER_DELTA_OTA   = 1        # patch download (presigned URL → S3)
S3_GETS_PER_FULL_OTA    = 1        # full binary download (presigned URL → S3)

# Assumptions
CHECKS_PER_DEVICE_PER_HOUR = 2     # device polls every 31 s → ~116/hr; OTA rarely triggered
                                   # but we model for the check that triggers OTA
COLD_START_FRACTION     = 0.064    # 48/753 = 6.4% cold starts, measured from campaign logs


def lambda_cost_usd(billed_ms: float, mem_mb: float, n_requests: int) -> dict:
    gb_s = (mem_mb / 1024) * (billed_ms / 1000)
    total_gb_s = gb_s * n_requests
    req_cost   = (n_requests / 1_000_000) * PRICING["lambda_req_per_M"]
    dur_cost   = total_gb_s * PRICING["lambda_gb_s"]
    return {
        "per_request_gb_s": gb_s,
        "total_gb_s":       total_gb_s,
        "request_cost_usd": req_cost,
        "duration_cost_usd": dur_cost,
        "total_usd":        req_cost + dur_cost,
    }


def apigw_cost_usd(n_requests: int) -> float:
    return (n_requests / 1_000_000) * PRICING["apigw_req_per_M"]


def s3_transfer_cost_usd(bytes_out: int) -> float:
    return (bytes_out / (1024**3)) * PRICING["s3_transfer_out_gb"]


def s3_get_cost_usd(n_gets: int) -> float:
    return (n_gets / 1000) * PRICING["s3_get_per_1k"]


def per_update_cost(patch_bytes: int, is_delta: bool, label: str) -> dict:
    """Cost for a single OTA update (one device, one event)."""
    # Lambda: 1 check request (blend warm/cold)
    billed = (LAMBDA_BILLED_MS_COLD * COLD_START_FRACTION
              + LAMBDA_BILLED_MS_WARM * (1 - COLD_START_FRACTION))
    lam = lambda_cost_usd(billed, LAMBDA_MEM_MB, 1)

    # API Gateway: 1 call
    gw  = apigw_cost_usd(1)

    # S3: 1 GET manifest + 1 GET patch/binary
    s3g  = s3_get_cost_usd(S3_GETS_PER_CHECK + 1)

    # Data transfer out: patch or full binary
    xfer = s3_transfer_cost_usd(patch_bytes)

    total = lam["total_usd"] + gw + s3g + xfer
    return {
        "label":            label,
        "lambda_usd":       lam["total_usd"],
        "apigw_usd":        gw,
        "s3_get_usd":       s3g,
        "s3_transfer_usd":  xfer,
        "total_usd":        total,
        "bytes_transferred": patch_bytes,
    }


def rollout_cost(per_device: dict, n_devices: int) -> dict:
    """Scale a per-update cost to N devices."""
    return {
        "n_devices":    n_devices,
        "label":        per_device["label"],
        "lambda_usd":   per_device["lambda_usd"]      * n_devices,
        "apigw_usd":    per_device["apigw_usd"]       * n_devices,
        "s3_get_usd":   per_device["s3_get_usd"]      * n_devices,
        "s3_xfer_usd":  per_device["s3_transfer_usd"] * n_devices,
        "total_usd":    per_device["total_usd"]        * n_devices,
        "data_out_gb":  per_device["bytes_transferred"] * n_devices / (1024**3),
    }


def free_tier_delta(rollout: dict) -> dict:
    """Estimate savings from free-tier credits for a monthly rollout."""
    # Only meaningful in free-tier month; assume fleet does exactly this rollout once/month
    ft = PRICING
    lam_req_free  = ft["free_lambda_req_M"]  * 1_000_000  # requests
    lam_gb_s_free = ft["free_lambda_gb_s"]
    apigw_free    = ft["free_apigw_req_M"]   * 1_000_000
    s3_get_free   = ft["free_s3_get_1k"]     * 1_000

    n = rollout["n_devices"]
    lam_req_saving  = min(n, lam_req_free)
    lam_dur_saving  = (min(n, lam_req_free) *
                       (LAMBDA_MEM_MB / 1024) * (LAMBDA_BILLED_MS_WARM / 1000) *
                       PRICING["lambda_gb_s"])
    gw_saving       = min(n, apigw_free) / 1_000_000 * PRICING["apigw_req_per_M"]
    s3_get_saving   = min(n * (S3_GETS_PER_CHECK + 1), s3_get_free) / 1000 * PRICING["s3_get_per_1k"]
    total_saving    = lam_dur_saving + gw_saving + s3_get_saving

    return {
        "lambda_duration_saving_usd": lam_dur_saving,
        "apigw_saving_usd":           gw_saving,
        "s3_get_saving_usd":          s3_get_saving,
        "total_saving_usd":           total_saving,
        "after_free_tier_usd":        max(0, rollout["total_usd"] - total_saving),
    }


def main():
    delta_per  = per_update_cost(DELTA_PATCH_BYTES, True,  "delta")
    full_per   = per_update_cost(FULL_IMAGE_BYTES,  False, "full")

    n_fleet = 1_000
    delta_1k = rollout_cost(delta_per, n_fleet)
    full_1k  = rollout_cost(full_per,  n_fleet)
    delta_ft = free_tier_delta(delta_1k)
    full_ft  = free_tier_delta(full_1k)

    result = {
        "pricing":     PRICING,
        "inputs": {
            "lambda_billed_ms_warm":  LAMBDA_BILLED_MS_WARM,
            "lambda_billed_ms_cold":  LAMBDA_BILLED_MS_COLD,
            "lambda_mem_mb":          LAMBDA_MEM_MB,
            "delta_patch_bytes":      DELTA_PATCH_BYTES,
            "full_image_bytes":       FULL_IMAGE_BYTES,
            "cold_start_fraction":    COLD_START_FRACTION,
        },
        "per_update": {
            "delta": delta_per,
            "full":  full_per,
        },
        "rollout_1000_devices": {
            "delta":          delta_1k,
            "full":           full_1k,
            "delta_free_tier": delta_ft,
            "full_free_tier":  full_ft,
        },
        "_note": ("All figures are MODEL ESTIMATES. "
                  "Assumes blended warm/cold Lambda, one check+one download per device. "
                  "Free-tier calculation assumes this is the only workload in the billing month. "
                  "Data transfer assumes all downloads go via S3 presigned URLs (no CloudFront)."),
    }

    out_path = Path(__file__).parent.parent / "results" / "stage4" / "data" / "cost_model.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[cost] Wrote {out_path}")

    def usd(v): return f"${v:.6f}"
    def usd4(v): return f"${v:.4f}"

    print("\n=== Cost Model — OTA Pipeline (us-east-1, 2026-06-15) ===")
    print("  [All figures are MODEL ESTIMATES — see _note in JSON]")
    print()
    print(f"  Blended Lambda billed duration (5% cold): "
          f"{LAMBDA_BILLED_MS_COLD*COLD_START_FRACTION + LAMBDA_BILLED_MS_WARM*(1-COLD_START_FRACTION):.1f} ms")
    print()

    print("  ── Per Device Per Update ──")
    print(f"  {'Component':<22}  {'Delta OTA':>14}  {'Full OTA':>14}")
    for comp, dk, fk in [
        ("Lambda", "lambda_usd", "lambda_usd"),
        ("API Gateway", "apigw_usd", "apigw_usd"),
        ("S3 GET", "s3_get_usd", "s3_get_usd"),
        ("S3 Transfer", "s3_transfer_usd", "s3_transfer_usd"),
    ]:
        print(f"  {comp:<22}  {usd(delta_per[dk]):>14}  {usd(full_per[fk]):>14}")
    print(f"  {'TOTAL':<22}  {usd(delta_per['total_usd']):>14}  {usd(full_per['total_usd']):>14}")
    print(f"  {'Bytes transferred':<22}  {delta_per['bytes_transferred']/1024:>12.1f} KB"
          f"  {full_per['bytes_transferred']/1024:>12.1f} KB")
    print()

    print(f"  ── 1,000-Device Rollout (raw cost) ──")
    print(f"  {'Component':<22}  {'Delta OTA':>14}  {'Full OTA':>14}")
    for comp, dk, fk in [
        ("Lambda", "lambda_usd", "lambda_usd"),
        ("API Gateway", "apigw_usd", "apigw_usd"),
        ("S3 GET", "s3_get_usd", "s3_get_usd"),
        ("S3 Transfer", "s3_xfer_usd", "s3_xfer_usd"),
    ]:
        print(f"  {comp:<22}  {usd4(delta_1k[dk]):>14}  {usd4(full_1k[fk]):>14}")
    print(f"  {'TOTAL':<22}  {usd4(delta_1k['total_usd']):>14}  {usd4(full_1k['total_usd']):>14}")
    print(f"  {'Data out':<22}  {delta_1k['data_out_gb']:>13.3f}GB  {full_1k['data_out_gb']:>13.3f}GB")
    print()

    print(f"  ── After Free-Tier Credits (first 12 months) ──")
    print(f"  {'Delta OTA':<10}: raw={usd4(delta_1k['total_usd'])}  "
          f"saving={usd4(delta_ft['total_saving_usd'])}  "
          f"net={usd4(delta_ft['after_free_tier_usd'])}")
    print(f"  {'Full OTA':<10}: raw={usd4(full_1k['total_usd'])}  "
          f"saving={usd4(full_ft['total_saving_usd'])}  "
          f"net={usd4(full_ft['after_free_tier_usd'])}")
    print()
    ratio = full_per["total_usd"] / delta_per["total_usd"] if delta_per["total_usd"] else float("nan")
    print(f"  Full/Delta cost ratio: {ratio:.1f}×  "
          f"(driven by {FULL_IMAGE_BYTES//1024} KB vs {DELTA_PATCH_BYTES//1024} KB transfer)")


if __name__ == "__main__":
    main()
