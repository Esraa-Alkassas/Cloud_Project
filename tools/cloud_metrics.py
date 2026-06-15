#!/usr/bin/env python3
"""
Stage 4 — cloud_metrics.py
Subcommands:
  lambda   Pull CloudWatch REPORT lines for the OTA Lambda, parse warm/cold.
  storage  List S3 objects, sum by category, project growth.
"""

import argparse
import csv
import json
import math
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

REGION = "us-east-1"
BUCKET = "esp32-firmware-rewaa"
LOG_GROUP = "/aws/lambda/Firmware-Validator-Hash"

# Cost guard
MAX_CW_PAGES = 20        # each page ~10 000 events → ample coverage
MAX_S3_PAGES = 10

RETRY_CFG = Config(
    region_name=REGION,
    retries={"max_attempts": 5, "mode": "adaptive"},
)

OUT_DIR = Path(__file__).parent.parent / "results" / "stage4" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# CloudWatch helpers
# ──────────────────────────────────────────────────────────────────────────────

REPORT_RE = re.compile(
    r"REPORT RequestId:\s+(?P<req_id>\S+)"
    r"\s+Duration:\s+(?P<duration_ms>[\d.]+)\s+ms"
    r"\s+Billed Duration:\s+(?P<billed_ms>[\d.]+)\s+ms"
    r"\s+Memory Size:\s+(?P<mem_size_mb>\d+)\s+MB"
    r"\s+Max Memory Used:\s+(?P<mem_used_mb>\d+)\s+MB"
    r"(?:\s+Init Duration:\s+(?P<init_ms>[\d.]+)\s+ms)?"
)


def parse_report(line: str) -> dict | None:
    m = REPORT_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    return {
        "request_id":    d["req_id"],
        "duration_ms":   float(d["duration_ms"]),
        "billed_ms":     float(d["billed_ms"]),
        "mem_size_mb":   int(d["mem_size_mb"]),
        "mem_used_mb":   int(d["mem_used_mb"]),
        "init_ms":       float(d["init_ms"]) if d["init_ms"] else None,
        "cold_start":    d["init_ms"] is not None,
    }


def pull_lambda_reports(start_ms: int, end_ms: int, allow_high_volume: bool = False) -> list[dict]:
    logs = boto3.client("logs", config=RETRY_CFG)
    rows: list[dict] = []
    page_count = 0
    cap = MAX_CW_PAGES if not allow_high_volume else MAX_CW_PAGES * 10
    token = None
    consecutive_empty = 0

    print(f"[lambda] Pulling REPORT lines from {LOG_GROUP} …")
    while True:
        if page_count >= cap:
            print(f"[lambda] Page cap ({cap}) reached — stopping pagination.")
            break
        kwargs: dict = {
            "logGroupName": LOG_GROUP,
            "filterPattern": "REPORT",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 10_000,
        }
        if token:
            kwargs["nextToken"] = token

        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            print(f"[lambda] Log group {LOG_GROUP!r} not found. Exiting.", file=sys.stderr)
            break
        except Exception as exc:
            print(f"[lambda] CloudWatch error (page {page_count}): {exc}", file=sys.stderr)
            break

        page_count += 1
        events = resp.get("events", [])
        parsed = 0
        for ev in events:
            r = parse_report(ev["message"])
            if r:
                r["timestamp_ms"] = ev["timestamp"]
                rows.append(r)
                parsed += 1

        if parsed == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        print(f"[lambda]   page {page_count}: {len(events)} events, {parsed} REPORT rows "
              f"(total so far: {len(rows)})")

        token = resp.get("nextToken")
        if not token:
            break
        if consecutive_empty >= 3:
            print("[lambda] 3 consecutive empty pages — stopping.")
            break

    return rows


def rtt_ms_from_stage_data() -> list[float]:
    """Pull device-observed rtt_ms from Stage 2/3 CSVs (ota_check events)."""
    rtts: list[float] = []
    root = Path(__file__).parent.parent
    for csvf in sorted(root.glob("results/stage*/MN_DELTA*.csv")) + \
                sorted(root.glob("results/stage*/MN_FULL*.csv")) + \
                sorted(root.glob("results/stage*/NU_DELTA*.csv")) + \
                sorted(root.glob("results/stage*/MJ_DELTA*.csv")):
        try:
            with open(csvf) as f:
                for row in csv.DictReader(f):
                    if row.get("ev") == "ota_check":
                        val = row.get("rtt_ms", "")
                        if val:
                            try:
                                rtts.append(float(val))
                            except ValueError:
                                pass
        except Exception as exc:
            print(f"[lambda] Warning: could not read {csvf}: {exc}", file=sys.stderr)
    return rtts


def cmd_lambda(args):
    # Campaign window: Stage 2/3 ran June 11–15 2026 UTC
    # Epoch for June 11 00:00 UTC 2026 → 1781136000
    # Epoch for June 16 00:00 UTC 2026 → 1781568000 + 86400
    start_ms = args.start_ms or 1_781_136_000_000
    end_ms   = args.end_ms   or int(time.time() * 1000)

    rows = pull_lambda_reports(start_ms, end_ms, allow_high_volume=args.allow_high_volume)
    if not rows:
        print("[lambda] No REPORT lines found in window. Exiting.")
        return

    # Write raw CSV
    csv_path = OUT_DIR / "lambda_reports.csv"
    fieldnames = ["timestamp_ms", "request_id", "duration_ms", "billed_ms",
                  "mem_size_mb", "mem_used_mb", "init_ms", "cold_start"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"[lambda] Wrote {len(rows)} rows → {csv_path}")

    cold = [r for r in rows if r["cold_start"]]
    warm = [r for r in rows if not r["cold_start"]]

    def stats(vals):
        if not vals:
            return {"n": 0, "mean": float("nan"), "std": float("nan"),
                    "min": float("nan"), "max": float("nan")}
        n = len(vals)
        mean = sum(vals) / n
        std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / n) if n > 1 else 0.0
        return {"n": n, "mean": mean, "std": std, "min": min(vals), "max": max(vals)}

    warm_dur  = stats([r["duration_ms"] for r in warm])
    cold_dur  = stats([r["duration_ms"] for r in cold])
    warm_bill = stats([r["billed_ms"]   for r in warm])
    cold_bill = stats([r["billed_ms"]   for r in cold])
    warm_mem  = stats([r["mem_used_mb"] for r in warm])
    cold_mem  = stats([r["mem_used_mb"] for r in cold])
    cold_init = stats([r["init_ms"]     for r in cold])

    # Device-observed rtt_ms from Stage 2/3 campaigns
    rtts = rtt_ms_from_stage_data()
    rtt_stats = stats(rtts)

    summary = {
        "warm": {
            "n":            warm_dur["n"],
            "duration_ms":  warm_dur,
            "billed_ms":    warm_bill,
            "mem_used_mb":  warm_mem,
        },
        "cold": {
            "n":            cold_dur["n"],
            "duration_ms":  cold_dur,
            "billed_ms":    cold_bill,
            "mem_used_mb":  cold_mem,
            "init_ms":      cold_init,
        },
        "device_rtt_ms": rtt_stats,
    }

    summary_path = OUT_DIR / "lambda_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[lambda] Summary → {summary_path}")

    print("\n=== Lambda Performance Summary ===")
    print(f"  Warm starts: N={warm_dur['n']}")
    print(f"    Duration:      {warm_dur['mean']:.2f} ± {warm_dur['std']:.2f} ms")
    print(f"    Billed:        {warm_bill['mean']:.2f} ± {warm_bill['std']:.2f} ms")
    print(f"    Max Mem Used:  {warm_mem['mean']:.1f} ± {warm_mem['std']:.1f} MB")
    print(f"  Cold starts: N={cold_dur['n']}")
    if cold_dur["n"] > 0:
        print(f"    Init Duration: {cold_init['mean']:.2f} ± {cold_init['std']:.2f} ms")
        print(f"    Duration:      {cold_dur['mean']:.2f} ± {cold_dur['std']:.2f} ms")
        print(f"    Billed:        {cold_bill['mean']:.2f} ± {cold_bill['std']:.2f} ms")
        total_cold = [r["init_ms"] + r["duration_ms"] for r in cold]
        tc = stats(total_cold)
        print(f"    Total (init+exec): {tc['mean']:.2f} ± {tc['std']:.2f} ms")
        warm_mean = warm_dur["mean"] if warm_dur["n"] else float("nan")
        print(f"    Cold/Warm ratio (exec): {cold_dur['mean']/warm_mean:.1f}×")
    print(f"  Device-observed rtt_ms: N={rtt_stats['n']}")
    print(f"    Mean: {rtt_stats['mean']:.1f} ± {rtt_stats['std']:.1f} ms")
    print(f"    (includes API GW + Lambda + network)")
    if warm_dur["n"] > 0:
        overhead = rtt_stats["mean"] - warm_dur["mean"]
        print(f"    Network+GW overhead est: {overhead:.1f} ms "
              f"({overhead/rtt_stats['mean']*100:.0f}% of rtt)")


# ──────────────────────────────────────────────────────────────────────────────
# S3 storage
# ──────────────────────────────────────────────────────────────────────────────

def cmd_storage(args):
    s3 = boto3.client("s3", config=RETRY_CFG)
    objects: list[dict] = []
    page_count = 0
    token = None

    print(f"[storage] Listing s3://{BUCKET} …")
    while True:
        if page_count >= MAX_S3_PAGES:
            print(f"[storage] Page cap ({MAX_S3_PAGES}) reached.")
            break
        kwargs: dict = {"Bucket": BUCKET, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            resp = s3.list_objects_v2(**kwargs)
        except Exception as exc:
            print(f"[storage] S3 list error: {exc}", file=sys.stderr)
            break
        page_count += 1
        for obj in resp.get("Contents", []):
            objects.append({"key": obj["Key"], "size_bytes": obj["Size"]})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    print(f"[storage] Found {len(objects)} objects.")

    # Categorise
    full_bins  = [o for o in objects if re.match(r"build_.+\.bin$", o["key"])]
    patches    = [o for o in objects if re.match(r"patch_.+\.bin$", o["key"])]
    manifests  = [o for o in objects if "manifest" in o["key"]]
    other      = [o for o in objects if o not in full_bins + patches + manifests]

    def tot(lst): return sum(o["size_bytes"] for o in lst)

    # Write CSV
    csv_path = OUT_DIR / "s3_objects.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["key", "size_bytes", "category"])
        w.writeheader()
        for o in objects:
            cat = ("full_binary" if o in full_bins
                   else "patch"        if o in patches
                   else "manifest"     if o in manifests
                   else "other")
            w.writerow({**o, "category": cat})
    print(f"[storage] Wrote {csv_path}")

    # Version count (unique build_ prefixes, excluding s2-/s3- corpus)
    ci_bins   = [o for o in full_bins if re.match(r"build_\d+-\w+\.bin$", o["key"])]
    corpus_bins = [o for o in full_bins if o not in ci_bins]
    n_versions = len(ci_bins)
    n_patches  = len(patches)

    # Per-version averages
    avg_full   = tot(ci_bins)  / max(n_versions, 1)
    avg_patch  = tot(patches)  / max(n_patches,  1)

    result = {
        "total_objects":  len(objects),
        "full_binaries":  {"count": len(full_bins),    "total_bytes": tot(full_bins)},
        "ci_binaries":    {"count": n_versions,        "total_bytes": tot(ci_bins)},
        "corpus_bins":    {"count": len(corpus_bins),  "total_bytes": tot(corpus_bins)},
        "patches":        {"count": n_patches,         "total_bytes": tot(patches)},
        "manifests":      {"count": len(manifests),    "total_bytes": tot(manifests)},
        "other":          {"count": len(other),        "total_bytes": tot(other)},
        "grand_total_bytes": tot(objects),
        "avg_full_bin_bytes": avg_full,
        "avg_patch_bytes":    avg_patch,
        "n_ci_versions": n_versions,
        "n_patches":     n_patches,
    }

    summary_path = OUT_DIR / "storage_summary.json"
    with open(summary_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[storage] Summary → {summary_path}")

    def fmt_kb(b): return f"{b/1024:.1f} KB"
    def fmt_mb(b): return f"{b/1024/1024:.2f} MB"

    print("\n=== S3 Storage Summary ===")
    print(f"  Total objects:      {len(objects)}")
    print(f"  Full binaries:      {len(full_bins):3d}  ({fmt_mb(tot(full_bins))})")
    print(f"    CI builds:        {n_versions:3d}  ({fmt_mb(tot(ci_bins))})")
    print(f"    Corpus (s2/s3):   {len(corpus_bins):3d}  ({fmt_mb(tot(corpus_bins))})")
    print(f"  Delta patches:      {n_patches:3d}  ({fmt_mb(tot(patches))})")
    print(f"  Manifests:          {len(manifests):3d}  ({fmt_kb(tot(manifests))})")
    print(f"  Grand total:        {fmt_mb(tot(objects))}")
    print(f"\n  Avg full binary:    {fmt_kb(avg_full)}")
    print(f"  Avg patch size:     {fmt_kb(avg_patch)}")
    print(f"\n  CI versions so far: {n_versions}")
    print(f"  Patches per version: {n_patches/max(n_versions,1):.1f}  "
          f"(each release creates patches from every prior → O(n) cumulative)")

    # Growth projection: O(n) patch count, O(n) binary count
    print("\n  --- Growth projection (model) ---")
    print(f"  {'Versions':>10}  {'Full bins':>12}  {'Patches':>10}  {'Total':>12}")
    for k in [10, 20, 50, 100]:
        n_p = k * (k - 1) // 2  # all-pairs: n*(n-1)/2 patches
        full_sz = k * avg_full
        patch_sz = n_p * avg_patch
        print(f"  {k:>10}  {fmt_mb(full_sz):>12}  {n_p:>10}  {fmt_mb(full_sz+patch_sz):>12}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Stage 4 cloud metrics")
    ap.add_argument("--allow-high-volume", action="store_true",
                    help="Override the 200-invocation cost guard (required explicitly)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_lam = sub.add_parser("lambda", help="Pull Lambda REPORT metrics from CloudWatch")
    p_lam.add_argument("--start-ms", type=int, default=0,
                       help="CloudWatch window start (epoch ms). Default: June 11 2026 UTC")
    p_lam.add_argument("--end-ms",   type=int, default=0,
                       help="CloudWatch window end   (epoch ms). Default: now")

    sub.add_parser("storage", help="Analyse S3 storage footprint")

    args = ap.parse_args()
    if args.cmd == "lambda":
        cmd_lambda(args)
    elif args.cmd == "storage":
        cmd_storage(args)


if __name__ == "__main__":
    main()
