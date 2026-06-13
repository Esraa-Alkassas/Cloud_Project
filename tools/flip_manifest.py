#!/usr/bin/env python3
"""
flip_manifest.py — Safely inspect and flip latest_version in S3 manifest.json.

Requires boto3 and AWS credentials (env vars, ~/.aws/credentials, or IAM role).

Usage:
  flip_manifest.py show
  flip_manifest.py set-latest --version <ver>
  flip_manifest.py backup
  flip_manifest.py restore
"""

import argparse
import json
import sys

BUCKET = "esp32-firmware-rewaa"
MANIFEST_KEY = "manifest.json"
BACKUP_KEY = "manifest.backup.json"


def get_s3():
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 not installed — run: pip install boto3")
    return boto3.client("s3", region_name="us-east-1")


def fetch_manifest(s3) -> dict:
    try:
        resp = s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:
        sys.exit(f"ERROR fetching manifest: {e}")


def put_manifest(s3, manifest: dict):
    body = json.dumps(manifest, indent=4).encode("utf-8")
    s3.put_object(Bucket=BUCKET, Key=MANIFEST_KEY, Body=body,
                  ContentType="application/json")


def object_exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_show(_args):
    s3 = get_s3()
    manifest = fetch_manifest(s3)
    print(json.dumps(manifest, indent=2))


def cmd_set_latest(args):
    s3 = get_s3()
    ver = args.version
    build_key = f"build_{ver}.bin"

    if not object_exists(s3, build_key):
        sys.exit(
            f"ERROR: s3://{BUCKET}/{build_key} does not exist.\n"
            f"Cannot set latest_version to '{ver}' — upload the binary first."
        )

    manifest = fetch_manifest(s3)
    old_ver = manifest.get("latest_version", "<none>")
    manifest["latest_version"] = ver
    # delta_patches left completely untouched
    put_manifest(s3, manifest)
    print(f"latest_version: {old_ver!r} → {ver!r}")
    print("delta_patches unchanged.")


def cmd_backup(_args):
    s3 = get_s3()
    manifest = fetch_manifest(s3)
    body = json.dumps(manifest, indent=4).encode("utf-8")
    s3.put_object(Bucket=BUCKET, Key=BACKUP_KEY, Body=body,
                  ContentType="application/json")
    print(f"Backed up manifest.json → {BACKUP_KEY}  "
          f"(latest_version={manifest.get('latest_version')!r})")


def cmd_restore(_args):
    s3 = get_s3()
    if not object_exists(s3, BACKUP_KEY):
        sys.exit(f"ERROR: s3://{BUCKET}/{BACKUP_KEY} not found — run 'backup' first.")
    try:
        resp = s3.get_object(Bucket=BUCKET, Key=BACKUP_KEY)
        backup = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:
        sys.exit(f"ERROR reading backup: {e}")

    # Normalize delta_patches: Lambda expects plain string keys, not dicts
    patches = backup.get("delta_patches", {})
    for k, v in list(patches.items()):
        if isinstance(v, dict):
            patches[k] = v.get("patch_key", v.get("key", ""))

    put_manifest(s3, backup)
    print(f"Restored {BACKUP_KEY} → manifest.json  "
          f"(latest_version={backup.get('latest_version')!r})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Pretty-print current manifest.json from S3")

    p = sub.add_parser("set-latest",
                       help="Point latest_version at an existing build in S3")
    p.add_argument("--version", required=True,
                   help="Version string (must have build_<ver>.bin in S3)")

    sub.add_parser("backup",  help="Copy manifest.json → manifest.backup.json in S3")
    sub.add_parser("restore", help="Copy manifest.backup.json → manifest.json in S3")

    args = parser.parse_args()
    {
        "show":       cmd_show,
        "set-latest": cmd_set_latest,
        "backup":     cmd_backup,
        "restore":    cmd_restore,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
