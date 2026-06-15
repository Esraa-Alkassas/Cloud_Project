#!/usr/bin/env python3
"""
build_stage3.py — Build all Stage 3 firmware binaries, upload to S3, create delta patches.

Builds:
  s3-base      — baseline (CONFIG_OTA_FORCE_FULL=n)
  s3-mn1       — mn-1 behavioral change (static attempt counter + 3s reboot delay)
  s3-nu2       — trivial comment change (creates tiny delta, shows fixed-cost floor)
  s3-base-full — same as s3-base with CONFIG_OTA_FORCE_FULL=y
  s3-mn1-full  — same as s3-mn1 with CONFIG_OTA_FORCE_FULL=y

Run from the repo root inside the ESP-IDF devcontainer.
Requires:  boto3, detools, cryptography  (pip install boto3 detools cryptography)

Usage:
  python tools/build_stage3.py [--skip-existing] [--dry-run]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

BUCKET    = "esp32-firmware-rewaa"
REGION    = "us-east-1"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OTA_C     = os.path.join(REPO_ROOT, "main", "ota.c")
VERSION_F = os.path.join(REPO_ROOT, "version.txt")
SDKCONFIG = os.path.join(REPO_ROOT, "sdkconfig")
BUILD_DIR = os.path.join(REPO_ROOT, "build")
BIN_OUT   = "/tmp"

AES_KEY = b"1234567890123456"
AES_IV  = b"abcdefghijklmnop"

SUBPROCESS_TIMEOUT = 300   # 5 min max for idf.py build

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def info(msg):  print(f"  [build] {msg}")
def ok(msg):    print(f"  [OK]    {msg}")
def err(msg):   print(f"  [ERROR] {msg}", file=sys.stderr)


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_build(label):
    """Run idf.py reconfigure + build; return True on success."""
    info(f"Building {label}...")
    for cmd in [["idf.py", "reconfigure"], ["idf.py", "build"]]:
        r = subprocess.run(cmd, cwd=REPO_ROOT, timeout=SUBPROCESS_TIMEOUT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            err(f"'{' '.join(cmd)}' failed for {label}:\n{r.stdout[-2000:]}")
            return False
    out_bin = os.path.join(BUILD_DIR, "wifi_station.bin")
    if not os.path.exists(out_bin):
        err(f"Expected binary not found: {out_bin}")
        return False
    dest = os.path.join(BIN_OUT, f"build_{label}.bin")
    shutil.copy2(out_bin, dest)
    ok(f"{label} → {dest}  ({os.path.getsize(dest)//1024} kB, sha256={sha256_of(dest)[:12]})")
    return True


def set_version(ver):
    write_file(VERSION_F, ver + "\n")
    info(f"version.txt → {ver}")


def set_force_full(enabled):
    content = read_file(SDKCONFIG)
    if enabled:
        content = re.sub(r"# CONFIG_OTA_FORCE_FULL is not set",
                         "CONFIG_OTA_FORCE_FULL=y", content)
        content = re.sub(r"CONFIG_OTA_FORCE_FULL=n",
                         "CONFIG_OTA_FORCE_FULL=y", content)
    else:
        content = re.sub(r"CONFIG_OTA_FORCE_FULL=y",
                         "# CONFIG_OTA_FORCE_FULL is not set", content)
    write_file(SDKCONFIG, content)
    info(f"CONFIG_OTA_FORCE_FULL → {'y' if enabled else 'n'}")


def apply_mn1_patch():
    """Apply the mn-1 behavioral change to ota.c in-place."""
    content = read_file(OTA_C)
    marker = "void trigger_delta_ota_update(void)\n{"
    patch  = ("void trigger_delta_ota_update(void)\n"
              "{\n"
              "    static int s_attempt = 0;\n"
              "    ++s_attempt;\n"
              "    ESP_LOGI(TAG, \"OTA attempt #%d\", s_attempt);\n")
    if "s_attempt" in content:
        info("mn-1 patch already applied")
        return
    content = content.replace(marker, patch, 1)
    # Extend reboot delay from 100ms to 3100ms
    content = content.replace(
        "vTaskDelay(pdMS_TO_TICKS(100)); /* flush UART before reset */",
        "vTaskDelay(pdMS_TO_TICKS(3100)); /* 100 ms UART flush + 3 s mn-1 reboot delay */"
    )
    write_file(OTA_C, content)
    info("mn-1 patch applied (attempt counter + 3s reboot delay)")


def revert_mn1_patch():
    """Remove the mn-1 behavioral change from ota.c."""
    content = read_file(OTA_C)
    patch   = ("void trigger_delta_ota_update(void)\n"
               "{\n"
               "    static int s_attempt = 0;\n"
               "    ++s_attempt;\n"
               "    ESP_LOGI(TAG, \"OTA attempt #%d\", s_attempt);\n")
    marker  = "void trigger_delta_ota_update(void)\n{"
    content = content.replace(patch, marker + "\n", 1)
    content = content.replace(
        "vTaskDelay(pdMS_TO_TICKS(3100)); /* 100 ms UART flush + 3 s mn-1 reboot delay */",
        "vTaskDelay(pdMS_TO_TICKS(100)); /* flush UART before reset */"
    )
    write_file(OTA_C, content)
    info("mn-1 patch reverted")


def apply_nu2_comment():
    """Apply the nu-2 trivial comment to ota.c."""
    content = read_file(OTA_C)
    marker  = "void trigger_delta_ota_update(void)\n{"
    if "/* Stage 3 nu-2" in content or "Poll the Lambda" in content:
        info("nu-2 comment already applied")
        return
    patched = marker.replace(
        "void trigger_delta_ota_update(void)\n{",
        "/* Stage 3 nu-2: trivial comment — creates minimal delta patch for fixed-cost floor test. */\n"
        "void trigger_delta_ota_update(void)\n{"
    )
    content = content.replace(marker, patched, 1)
    write_file(OTA_C, content)
    info("nu-2 comment applied")


def revert_nu2_comment():
    content = read_file(OTA_C)
    content = content.replace(
        "/* Stage 3 nu-2: trivial comment — creates minimal delta patch for fixed-cost floor test. */\n",
        ""
    )
    write_file(OTA_C, content)
    info("nu-2 comment reverted")


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def encrypt_bytes(data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    # AES-CTR: nonce_counter must be 16 bytes; we pad IV with zeros for high 8 bytes
    iv_padded = AES_IV + b"\x00" * (16 - len(AES_IV))
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CTR(iv_padded), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def upload_firmware(s3, ver, dry_run):
    local_path = os.path.join(BIN_OUT, f"build_{ver}.bin")
    s3_key     = f"build_{ver}.bin"
    if not os.path.exists(local_path):
        err(f"Binary not found: {local_path}")
        return False
    try:
        s3.head_object(Bucket=BUCKET, Key=s3_key)
        info(f"Already in S3: {s3_key}")
        return True
    except Exception:
        pass
    with open(local_path, "rb") as f:
        raw = f.read()
    enc = encrypt_bytes(raw)
    info(f"Uploading {s3_key} ({len(raw)//1024} kB unenc → {len(enc)//1024} kB enc)...")
    if not dry_run:
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=enc)
    ok(f"Uploaded {s3_key}")
    return True


def gen_and_upload_patch(s3, old_ver, new_ver, dry_run):
    import detools
    patch_key = f"patch_{old_ver}_to_{new_ver}.bin"
    try:
        s3.head_object(Bucket=BUCKET, Key=patch_key)
        info(f"Patch already in S3: {patch_key}")
        return patch_key
    except Exception:
        pass

    old_path   = os.path.join(BIN_OUT, f"build_{old_ver}.bin")
    new_path   = os.path.join(BIN_OUT, f"build_{new_ver}.bin")
    plain_path = f"/tmp/patch_{old_ver}_to_{new_ver}.plain"

    info(f"Generating delta patch {old_ver}→{new_ver}...")
    t0 = time.monotonic()
    with open(old_path, "rb") as fo, open(new_path, "rb") as fn, \
         open(plain_path, "wb") as fp:
        detools.create_patch(fo, fn, fp, compression="crle")
    elapsed = time.monotonic() - t0
    patch_size = os.path.getsize(plain_path)
    info(f"Patch generated in {elapsed:.1f}s: {patch_size} B plain")

    with open(plain_path, "rb") as f:
        plain_data = f.read()
    enc = encrypt_bytes(plain_data)
    info(f"Uploading {patch_key} ({len(enc)//1024} kB enc)...")
    if not dry_run:
        s3.put_object(Bucket=BUCKET, Key=patch_key, Body=enc)
    ok(f"Patch uploaded: {patch_key} ({patch_size} B plain)")
    return patch_key


def update_manifest(s3, patches_ab, patches_ba, dry_run):
    """
    Update S3 manifest so:
    - latest_version = s3-base (device will update to latest when manifest is flipped)
    - delta_patches includes all new s3 entries
    The harness campaign will flip latest_version per run.
    """
    try:
        resp    = s3.get_object(Bucket=BUCKET, Key="manifest.json")
        manifest = json.loads(resp["Body"].read().decode())
    except Exception:
        manifest = {"latest_version": "", "delta_patches": {}}

    # Backup current manifest
    body_backup = json.dumps(manifest, indent=2).encode()
    if not dry_run:
        s3.put_object(Bucket=BUCKET, Key="manifest.backup.json",
                      Body=body_backup, ContentType="application/json")
    info("manifest backed up → manifest.backup.json")

    # Register all new patches
    for (old_ver, new_ver), patch_key in {**patches_ab, **patches_ba}.items():
        manifest.setdefault("delta_patches", {})[old_ver] = patch_key

    # Normalize any dict-valued entries
    for k, v in list(manifest.get("delta_patches", {}).items()):
        if isinstance(v, dict):
            manifest["delta_patches"][k] = v.get("patch_key", v.get("key", ""))

    # Set latest_version to s3-base so device sees an update (harness will flip as needed)
    manifest["latest_version"] = "s3-base"

    body = json.dumps(manifest, indent=2).encode()
    if not dry_run:
        s3.put_object(Bucket=BUCKET, Key="manifest.json",
                      Body=body, ContentType="application/json")
        # Keep backup in sync with clean format
        s3.put_object(Bucket=BUCKET, Key="manifest.backup.json",
                      Body=body, ContentType="application/json")
    ok(f"manifest updated: latest=s3-base, {len(manifest['delta_patches'])} delta patches")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

BUILDS = [
    # (label,     force_full, apply_mn1, apply_nu2)
    ("s3-base",      False, False, False),
    ("s3-mn1",       False, True,  False),
    ("s3-nu2",       False, False, True),
    ("s3-base-full", True,  False, False),
    ("s3-mn1-full",  True,  True,  False),
]

# Patches to generate: (old, new) pairs for both directions
PATCH_PAIRS = [
    ("s3-base",      "s3-mn1"),       # MN_DELTA A→B
    ("s3-mn1",       "s3-base"),      # MN_DELTA B→A
    ("s3-base",      "s3-nu2"),       # NU_DELTA A→B
    ("s3-nu2",       "s3-base"),      # NU_DELTA B→A
    ("s3-base-full", "s3-mn1-full"),  # MN_FULL A→B
    ("s3-mn1-full",  "s3-base-full"), # MN_FULL B→A
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip builds where /tmp/build_<label>.bin already exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not upload to S3 or modify manifest")
    ap.add_argument("--no-patches", action="store_true",
                    help="Skip patch generation (only build + upload binaries)")
    args = ap.parse_args()

    # Save original file states for restore
    orig_version   = read_file(VERSION_F)
    orig_ota_c     = read_file(OTA_C)
    orig_sdkconfig = read_file(SDKCONFIG)

    # ── 0. Verify deps ──────────────────────────────────────────────────────
    info("Checking dependencies...")
    missing = []
    for pkg in ["boto3", "detools", "cryptography"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing and not args.dry_run:
        sys.exit(f"Missing packages: {missing}  (pip install {' '.join(missing)})")
    ok("Dependencies OK")

    # ── 1. Build each firmware ──────────────────────────────────────────────
    info("Starting firmware builds...")
    build_results = {}

    try:
        for label, force_full, do_mn1, do_nu2 in BUILDS:
            dest = os.path.join(BIN_OUT, f"build_{label}.bin")
            if args.skip_existing and os.path.exists(dest):
                info(f"Skipping {label} (binary exists at {dest})")
                build_results[label] = True
                continue

            # Apply patches
            set_version(label)
            set_force_full(force_full)
            if do_mn1: apply_mn1_patch()
            if do_nu2: apply_nu2_comment()

            success = run_build(label)
            build_results[label] = success

            # Revert patches
            if do_mn1: revert_mn1_patch()
            if do_nu2: revert_nu2_comment()

            if not success:
                err(f"Build failed for {label} — aborting")
                break
    finally:
        # Always restore original files
        write_file(VERSION_F, orig_version)
        write_file(OTA_C,     orig_ota_c)
        write_file(SDKCONFIG, orig_sdkconfig)
        info(f"Restored version.txt, ota.c, sdkconfig")

    # Check all builds succeeded
    failed = [k for k, v in build_results.items() if not v]
    if failed:
        sys.exit(f"Build failures: {failed}")
    ok(f"All {len(BUILDS)} builds complete")

    if args.dry_run:
        info("--dry-run: skipping S3 upload and manifest update")
        return

    # ── 2. Upload to S3 ────────────────────────────────────────────────────
    import boto3
    s3 = boto3.client("s3", region_name=REGION)
    info("Uploading firmware binaries to S3...")
    for label, _, _, _ in BUILDS:
        if not upload_firmware(s3, label, args.dry_run):
            sys.exit(f"Upload failed for {label}")
    ok("All binaries uploaded")

    if args.no_patches:
        info("--no-patches: skipping patch generation")
        return

    # ── 3. Generate and upload delta patches ──────────────────────────────
    info("Generating delta patches...")
    patches = {}
    for old_ver, new_ver in PATCH_PAIRS:
        patch_key = gen_and_upload_patch(s3, old_ver, new_ver, args.dry_run)
        patches[(old_ver, new_ver)] = patch_key
    ok(f"{len(patches)} patches generated and uploaded")

    # ── 4. Update manifest ─────────────────────────────────────────────────
    info("Updating S3 manifest...")
    patches_ab = {k: v for k, v in patches.items()}
    update_manifest(s3, patches_ab, {}, args.dry_run)

    print()
    ok("Stage 3 build + upload complete.")
    print()
    print("  Next steps:")
    print("  1. Flash s3-base to factory:  idf.py flash (with version.txt=s3-base)")
    print("  2. Run MN_DELTA campaign:     python tools/harness.py campaign ...")
    print("     --version-a s3-base --version-b s3-mn1")
    print("  3. Run MN_FULL campaign:      python tools/harness.py campaign ...")
    print("     --version-a s3-base-full --version-b s3-mn1-full")
    print("  4. Run NU_DELTA campaign:     python tools/harness.py campaign ...")
    print("     --version-a s3-base --version-b s3-nu2")


if __name__ == "__main__":
    main()
