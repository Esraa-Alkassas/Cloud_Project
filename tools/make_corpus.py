#!/usr/bin/env python3
"""
make_corpus.py — Build firmware version pairs and generate delta patches.

Usage:
  make_corpus.py build  --ref <git-ref> --version-label <label> --out corpus/
  make_corpus.py pair   --old corpus/<A>.bin --new corpus/<B>.bin --out corpus/pairs/ \\
                        [--scenario NU|MN|MJ]
  make_corpus.py report --corpus corpus/ --out corpus/corpus.csv

Note: 'pair' uses unencrypted binaries. AES-CTR is size-preserving, so patch
sizes are identical to what the pipeline produces; this just skips the crypto step.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


META_FILE = "meta.json"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_meta(corpus_dir: str) -> dict:
    p = os.path.join(corpus_dir, META_FILE)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"builds": {}, "pairs": []}


def save_meta(corpus_dir: str, meta: dict):
    p = os.path.join(corpus_dir, META_FILE)
    with open(p, "w") as f:
        json.dump(meta, f, indent=2)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def cmd_build(args):
    corpus_dir = args.out
    os.makedirs(corpus_dir, exist_ok=True)
    label = args.version_label

    out_bin = os.path.join(corpus_dir, f"{label}.bin")
    if os.path.exists(out_bin):
        print(f"Already exists: {out_bin}  (skipping build)")
        return

    # Resolve git root
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=True)
    git_root = result.stdout.strip()

    # Resolve the ref to a full SHA
    result = subprocess.run(["git", "rev-parse", args.ref],
                            capture_output=True, text=True, check=True,
                            cwd=git_root)
    sha = result.stdout.strip()
    print(f"Building ref={args.ref} ({sha[:12]}) as label={label}")

    with tempfile.TemporaryDirectory(prefix="corpus_wt_") as wt_dir:
        # Create a git worktree at the ref
        subprocess.run(["git", "worktree", "add", "--detach", wt_dir, sha],
                       check=True, cwd=git_root)
        try:
            # Write version.txt so PROJECT_VER matches the label
            with open(os.path.join(wt_dir, "version.txt"), "w") as f:
                f.write(label)

            # Build inside devcontainer image
            print("Running docker build (this may take a few minutes)…")
            subprocess.run([
                "docker", "run", "--rm",
                "-v", f"{wt_dir}:/workspace",
                "espressif/idf:release-v5.2",
                "bash", "-c",
                "cd /workspace && . /opt/esp/idf/export.sh 2>/dev/null && idf.py build",
            ], check=True)

            built_bin = os.path.join(wt_dir, "build", "wifi_station.bin")
            if not os.path.exists(built_bin):
                sys.exit(f"ERROR: build succeeded but {built_bin} not found")

            size = os.path.getsize(built_bin)
            digest = sha256_of(built_bin)

            # Get sdkconfig hash for reproducibility tracing
            sdk_path = os.path.join(wt_dir, "sdkconfig")
            sdk_hash = sha256_of(sdk_path) if os.path.exists(sdk_path) else ""

            shutil.copy2(built_bin, out_bin)
            print(f"Copied → {out_bin}  ({size} bytes)")

            meta = load_meta(corpus_dir)
            meta["builds"][label] = {
                "ref":          args.ref,
                "sha":          sha,
                "size":         size,
                "sha256":       digest,
                "sdkconfig_sha256": sdk_hash,
            }
            save_meta(corpus_dir, meta)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", wt_dir],
                           cwd=git_root, capture_output=True)


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------

def cmd_pair(args):
    os.makedirs(args.out, exist_ok=True)

    old_path  = args.old
    new_path  = args.new
    old_label = Path(old_path).stem
    new_label = Path(new_path).stem
    patch_name = f"patch_{old_label}_to_{new_label}.bin"
    patch_path = os.path.join(args.out, patch_name)

    print(f"Creating patch: {old_label} → {new_label}")
    subprocess.run(
        ["detools", "create_patch", "-c", "crle", old_path, new_path, patch_path],
        check=True,
    )

    old_size   = os.path.getsize(old_path)
    new_size   = os.path.getsize(new_path)
    patch_size = os.path.getsize(patch_path)

    record = {
        "old":              old_label,
        "new":              new_label,
        "patch_file":       patch_name,
        "scenario":         args.scenario,
        "old_size":         old_size,
        "new_size":         new_size,
        "patch_size":       patch_size,
        "compression_factor": round(new_size / patch_size, 3) if patch_size else None,
        "patch_pct":          round(patch_size / new_size * 100, 2) if new_size else None,
        "sha256_old":       sha256_of(old_path),
        "sha256_new":       sha256_of(new_path),
        "sha256_patch":     sha256_of(patch_path),
        "note": "AES-CTR encryption is size-preserving; patch size equals encrypted patch size",
    }

    # Record in the corpus meta of the parent directory
    corpus_dir = str(Path(args.out).parent)
    meta = load_meta(corpus_dir)
    meta.setdefault("pairs", [])
    # Replace existing entry for same old→new if present
    meta["pairs"] = [p for p in meta["pairs"]
                     if not (p["old"] == old_label and p["new"] == new_label)]
    meta["pairs"].append(record)
    save_meta(corpus_dir, meta)

    print(f"Patch: {patch_size} bytes  "
          f"compression_factor={record['compression_factor']}  "
          f"patch_pct={record['patch_pct']}%")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(args):
    corpus_dir = args.corpus
    meta = load_meta(corpus_dir)
    pairs = meta.get("pairs", [])
    if not pairs:
        print("No pairs found in meta.json")
        return

    fields = ["old", "new", "scenario", "old_size", "new_size",
              "patch_size", "compression_factor", "patch_pct",
              "sha256_old", "sha256_new", "sha256_patch"]

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(pairs)

    print(f"Report: {len(pairs)} pair(s) → {args.out}")
    for p in pairs:
        print(f"  {p['old']} → {p['new']}  "
              f"patch={p['patch_size']}B  "
              f"cf={p['compression_factor']}  "
              f"scenario={p['scenario']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="Build a firmware binary from a git ref")
    p.add_argument("--ref",           required=True, help="git ref (branch, tag, SHA)")
    p.add_argument("--version-label", required=True, help="Label for version.txt (e.g. v1)")
    p.add_argument("--out",           default="corpus/", help="Output directory")

    p = sub.add_parser("pair", help="Generate a delta patch between two binaries")
    p.add_argument("--old",      required=True, help="Path to old .bin")
    p.add_argument("--new",      required=True, help="Path to new .bin")
    p.add_argument("--out",      required=True, help="Output directory for patch file")
    p.add_argument("--scenario", default=None,
                   help="Scenario label (NU, MN, MJ, etc.) for the report")

    p = sub.add_parser("report", help="Generate CSV report of all pairs")
    p.add_argument("--corpus", required=True, help="Corpus directory with meta.json")
    p.add_argument("--out",    required=True, help="Output CSV path")

    args = parser.parse_args()
    {"build": cmd_build, "pair": cmd_pair, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
