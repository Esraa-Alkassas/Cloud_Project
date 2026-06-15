#!/usr/bin/env python3
"""
run_stage3_campaigns.py — Orchestrate all three Stage 3 OTA campaigns.

Runs MN_DELTA → MN_FULL → NU_DELTA in sequence.
For each campaign:
  1. Flash the A firmware to factory partition
  2. Set manifest delta_patches for this campaign pair
  3. Run harness.py campaign (N=10 valid runs each direction)
  4. Export CSV
  5. Generate per-experiment plots

Usage (from project root):
  python tools/run_stage3_campaigns.py [--port /dev/ttyUSB0] [--runs 10] [--settle 20]

Prerequisites:
  - All firmware binaries built and uploaded (run tools/build_stage3.py first)
  - AWS credentials in ~/.aws/credentials or env vars
"""

import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS      = os.path.join(REPO_ROOT, "tools")
DB         = os.path.join(REPO_ROOT, "results", "stage3", "runs.db")
OUT_DIR    = os.path.join(REPO_ROOT, "results", "stage3")
PLOTS_DIR  = os.path.join(REPO_ROOT, "results", "stage3", "plots")
BUCKET     = "esp32-firmware-rewaa"
BIN_DIR    = "/tmp"
ESPTOOL    = "esptool.py"
BAUD       = 115200

CAMPAIGNS = [
    {
        "experiment": "MN_DELTA",
        "version_a":  "s3-base",
        "version_b":  "s3-mn1",
        "flash_ver":  "s3-base",         # firmware to flash to factory before campaign
        "patch_ab":   "patch_s3-base_to_s3-mn1.bin",
        "patch_ba":   "patch_s3-mn1_to_s3-base.bin",
    },
    {
        "experiment": "MN_FULL",
        "version_a":  "s3-base-full",
        "version_b":  "s3-mn1-full",
        "flash_ver":  "s3-base-full",
        "patch_ab":   "patch_s3-base-full_to_s3-mn1-full.bin",
        "patch_ba":   "patch_s3-mn1-full_to_s3-base-full.bin",
    },
    {
        "experiment": "NU_DELTA",
        "version_a":  "s3-base",
        "version_b":  "s3-nu2",
        "flash_ver":  "s3-base",
        "patch_ab":   "patch_s3-base_to_s3-nu2.bin",
        "patch_ba":   "patch_s3-nu2_to_s3-base.bin",
    },
]


def info(msg):  print(f"  [campaign] {msg}")
def ok(msg):    print(f"  [OK]       {msg}")
def err(msg):   print(f"  [ERROR]    {msg}", file=sys.stderr)
def run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def flash_factory(port, firmware_path):
    """Flash firmware binary to factory partition (0x10000) and erase otadata."""
    info(f"Erasing otadata (0xd000)...")
    rc, out, er = run([ESPTOOL, "--chip", "esp32", "--port", port, "--baud", "460800",
                       "erase_region", "0xd000", "0x2000"])
    if rc != 0:
        err(f"erase_region failed: {er}")
        return False

    info(f"Flashing {firmware_path} to factory (0x10000)...")
    rc, out, er = run([ESPTOOL, "--chip", "esp32", "--port", port, "--baud", "460800",
                       "write_flash", "0x10000", firmware_path], timeout=60)
    if rc != 0:
        err(f"write_flash failed: {er}")
        return False

    ok(f"Factory firmware flashed: {os.path.basename(firmware_path)}")
    return True


def set_manifest_for_campaign(s3, campaign):
    """Update manifest so this campaign's delta_patches are active."""
    ver_a   = campaign["version_a"]
    ver_b   = campaign["version_b"]
    patch_ab = campaign["patch_ab"]
    patch_ba = campaign["patch_ba"]

    resp     = s3.get_object(Bucket=BUCKET, Key="manifest.json")
    manifest = json.loads(resp["Body"].read().decode())

    manifest.setdefault("delta_patches", {})[ver_a] = patch_ab
    manifest["delta_patches"][ver_b] = patch_ba
    manifest["latest_version"] = ver_b   # harness will flip as needed

    # Normalize dict-valued entries
    for k, v in list(manifest["delta_patches"].items()):
        if isinstance(v, dict):
            manifest["delta_patches"][k] = v.get("patch_key", v.get("key", ""))

    body = json.dumps(manifest, indent=2).encode()
    s3.put_object(Bucket=BUCKET, Key="manifest.json",
                  Body=body, ContentType="application/json")
    s3.put_object(Bucket=BUCKET, Key="manifest.backup.json",
                  Body=body, ContentType="application/json")
    ok(f"Manifest: latest={ver_b}, "
       f"patch[{ver_a}]={patch_ab}, patch[{ver_b}]={patch_ba}")


def run_campaign(port, runs, settle, campaign):
    exp     = campaign["experiment"]
    ver_a   = campaign["version_a"]
    ver_b   = campaign["version_b"]
    cmd = [
        sys.executable, os.path.join(TOOLS, "harness.py"), "campaign",
        "--port", port, "--baud", str(BAUD),
        "--db", DB,
        "--experiment", exp,
        "--version-a", ver_a,
        "--version-b", ver_b,
        "--runs", str(runs),
        "--settle", str(settle),
        "--rssi-min", "-80",
    ]
    info(f"Starting {exp} campaign: {ver_a} ↔ {ver_b}  (N={runs}, settle={settle}s)")
    r = subprocess.run(cmd, cwd=REPO_ROOT)
    return r.returncode == 0


def export_and_plot(campaign):
    exp = campaign["experiment"]
    csv_path = os.path.join(OUT_DIR, f"{exp}.csv")
    info(f"Exporting {exp}...")
    rc, out, er = run([sys.executable, os.path.join(TOOLS, "harness.py"), "export",
                       "--db", DB, "--experiment", exp, "--out", csv_path])
    if rc != 0:
        err(f"Export failed: {er}")
    else:
        ok(f"Exported → {csv_path}")

    info(f"Plotting {exp}...")
    rc, out, er = run([sys.executable, os.path.join(TOOLS, "harness.py"), "plot",
                       "--db", DB, "--experiment", exp, "--out", PLOTS_DIR])
    if rc != 0:
        err(f"Plot failed: {er}")
    else:
        ok(f"Plots → {PLOTS_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port",    default="/dev/ttyUSB0")
    ap.add_argument("--runs",    type=int, default=10)
    ap.add_argument("--settle",  type=int, default=20)
    ap.add_argument("--only",    default=None,
                    help="Run only this experiment (e.g. MN_DELTA)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR,   exist_ok=True)
    os.makedirs(PLOTS_DIR, exist_ok=True)

    import boto3
    s3 = boto3.client("s3", region_name="us-east-1")

    campaigns = CAMPAIGNS
    if args.only:
        campaigns = [c for c in CAMPAIGNS if c["experiment"] == args.only]
        if not campaigns:
            sys.exit(f"Unknown experiment: {args.only}. Choices: {[c['experiment'] for c in CAMPAIGNS]}")

    for campaign in campaigns:
        exp  = campaign["experiment"]
        fver = campaign["flash_ver"]
        fbin = os.path.join(BIN_DIR, f"build_{fver}.bin")

        print(f"\n{'='*60}")
        print(f"  Campaign: {exp}")
        print(f"{'='*60}")

        if not os.path.exists(fbin):
            err(f"Firmware binary not found: {fbin} — run tools/build_stage3.py first")
            sys.exit(1)

        # 1. Flash factory firmware
        if not flash_factory(args.port, fbin):
            err(f"Flashing failed for {exp} — aborting")
            sys.exit(1)

        info("Waiting 8 s for factory boot...")
        time.sleep(8)

        # 2. Set manifest
        set_manifest_for_campaign(s3, campaign)

        # 3. Run campaign
        ok(f"Launching {exp} campaign (Ctrl-C will restore manifest)...")
        run_campaign(args.port, args.runs, args.settle, campaign)

        # 4. Export + plot
        export_and_plot(campaign)

        ok(f"{exp} done.")

    # Stage 3 cross-experiment plots
    exp_ids = ",".join(c["experiment"] for c in campaigns)
    size_csv = os.path.join(OUT_DIR, "size_report.csv")
    info("Generating Stage 3 cross-experiment plots...")
    rc, out, er = run([sys.executable, os.path.join(TOOLS, "harness.py"), "plot3",
                       "--db", DB,
                       "--experiments", exp_ids,
                       "--size-csv", size_csv,
                       "--out", PLOTS_DIR])
    if rc != 0:
        err(f"plot3 failed: {er}")
    else:
        ok(f"Stage 3 plots → {PLOTS_DIR}")

    print(f"\n{'='*60}")
    ok("All Stage 3 campaigns complete.")
    print(f"  Results: {OUT_DIR}")
    print(f"  DB:      {DB}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
