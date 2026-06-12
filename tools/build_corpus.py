#!/usr/bin/env python3
"""
build_corpus.py — Build all corpus firmware binaries and register pairs.

Wraps make_corpus.py build --local-idf for every corpus entry, then writes
meta.json pairs so bench_diff.py run can consume them.

Usage (run from repo root, inside the ESP-IDF devcontainer):
  python tools/build_corpus.py [--out corpus/bins/] [--skip-existing] [--dry-run]

Output
------
  corpus/bins/<label>.bin           — firmware binaries
  corpus/bins/meta.json             — build records + pair list
  results/stage1/corpus_builds.json — detailed build manifest (timing, SHA256, etc.)

Tracking
--------
Every build is logged to results/stage1/build_<label>.log so you can audit
each idf.py run independently.  The manifest JSON has per-build:
  label, branch, git_sha, build_time_s, bin_size_B, sha256, status, log_file
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── corpus definition ─────────────────────────────────────────────────────────
# Edit this table if you add / rename corpus branches.

CORPUS = [
    {"label": "base",  "branch": "feat/runtim-mesurments", "scenario": None,  "description": "production baseline"},
    {"label": "nu-2",  "branch": "corpus/nu-2",            "scenario": "NU",  "description": "comment added to ota.c"},
    {"label": "nu-3",  "branch": "corpus/nu-3",            "scenario": "NU",  "description": "OTA success log string changed"},
    {"label": "mn-1",  "branch": "corpus/mn-1",            "scenario": "MN",  "description": "static attempt counter + 3 s reboot delay"},
    {"label": "mn-2",  "branch": "corpus/mn-2",            "scenario": "MN",  "description": "ota_check_heap_guard() function added"},
    {"label": "mn-3",  "branch": "corpus/mn-3",            "scenario": "MN",  "description": "heap_delta field in heartbeat"},
    {"label": "mj-1",  "branch": "corpus/mj-1",            "scenario": "MJ",  "description": "CONFIG_TELEMETRY_ENABLE=y"},
    {"label": "mj-2",  "branch": "corpus/mj-2",            "scenario": "MJ",  "description": "debug log + mbedtls debug enabled"},
    {"label": "mj-3",  "branch": "corpus/mj-3",            "scenario": "MJ",  "description": "diag.c/diag.h diagnostic subsystem"},
    {"label": "wc-1",  "branch": "corpus/wc-1",            "scenario": "WC",  "description": "compiler optimisation -O2"},
]

# Pairs for bench_diff.py (all against the same base)
PAIRS = [
    {"old": "base", "new": "nu-2", "scenario": "NU", "description": "comment added to ota.c"},
    {"old": "base", "new": "nu-3", "scenario": "NU", "description": "OTA success log string changed"},
    {"old": "base", "new": "mn-1", "scenario": "MN", "description": "static attempt counter + 3 s reboot delay"},
    {"old": "base", "new": "mn-2", "scenario": "MN", "description": "ota_check_heap_guard() function added"},
    {"old": "base", "new": "mn-3", "scenario": "MN", "description": "heap_delta field in heartbeat"},
    {"old": "base", "new": "mj-1", "scenario": "MJ", "description": "CONFIG_TELEMETRY_ENABLE=y"},
    {"old": "base", "new": "mj-2", "scenario": "MJ", "description": "debug log + mbedtls debug enabled"},
    {"old": "base", "new": "mj-3", "scenario": "MJ", "description": "diag.c/diag.h diagnostic subsystem"},
    {"old": "base", "new": "wc-1", "scenario": "WC", "description": "compiler optimisation -O2"},
]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha(branch, git_root):
    r = subprocess.run(["git", "rev-parse", branch],
                       capture_output=True, text=True, cwd=git_root)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out",          default="corpus/bins/",
                    help="Output directory for binaries and meta.json")
    ap.add_argument("--manifest-out", default="results/stage1/",
                    help="Directory for corpus_builds.json and per-build logs")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip labels whose .bin already exists")
    ap.add_argument("--dry-run",      action="store_true",
                    help="Print what would be built, then exit")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.manifest_out, exist_ok=True)

    git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, check=True).stdout.strip()
    make_corpus = os.path.join(git_root, "tools", "make_corpus.py")

    print(f"\n{'='*60}")
    print(f"  Corpus build plan — {len(CORPUS)} binaries")
    print(f"  Output  : {os.path.abspath(args.out)}")
    print(f"  Manifest: {os.path.abspath(args.manifest_out)}")
    print(f"{'='*60}")
    for i, entry in enumerate(CORPUS, 1):
        exists = os.path.exists(os.path.join(args.out, f"{entry['label']}.bin"))
        status = "EXISTS" if exists else "PENDING"
        print(f"  [{i:2d}/{len(CORPUS)}] {entry['label']:8s}  branch={entry['branch']:30s}  {status}")
    print()

    if args.dry_run:
        print("--dry-run: exiting without building.")
        return

    manifest = []
    start_all = time.monotonic()

    for i, entry in enumerate(CORPUS, 1):
        label  = entry["label"]
        branch = entry["branch"]
        out_bin = os.path.join(args.out, f"{label}.bin")
        log_file = os.path.join(args.manifest_out, f"build_{label}.log")

        sha = git_sha(branch, git_root)
        record = {
            "label":       label,
            "branch":      branch,
            "git_sha":     sha,
            "description": entry["description"],
            "bin_path":    os.path.abspath(out_bin),
            "log_file":    os.path.abspath(log_file),
        }

        if args.skip_existing and os.path.exists(out_bin):
            record.update({"status": "skipped", "build_time_s": 0,
                            "bin_size_B": os.path.getsize(out_bin),
                            "sha256": sha256_of(out_bin)})
            print(f"[{i:2d}/{len(CORPUS)}] {label:8s} — SKIPPED (already built)")
            manifest.append(record)
            continue

        print(f"\n[{i:2d}/{len(CORPUS)}] Building '{label}'  (branch: {branch}  sha: {sha[:12]})")
        print(f"         Log → {log_file}")

        t0 = time.monotonic()
        with open(log_file, "w") as logf:
            logf.write(f"# build_corpus.py  label={label}  branch={branch}  sha={sha}\n")
            logf.write(f"# started: {datetime.now(timezone.utc).isoformat()}\n\n")
            logf.flush()

            cmd = [
                sys.executable, make_corpus,
                "build",
                "--ref", branch,
                "--version-label", label,
                "--out", args.out,
                "--local-idf",
            ]
            proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                  cwd=git_root)

        elapsed = time.monotonic() - t0

        if proc.returncode != 0 or not os.path.exists(out_bin):
            print(f"         ✗ FAILED after {elapsed:.1f}s  (see {log_file})")
            record.update({"status": "failed", "build_time_s": round(elapsed, 1),
                           "bin_size_B": 0, "sha256": ""})
        else:
            size   = os.path.getsize(out_bin)
            digest = sha256_of(out_bin)
            print(f"         ✓ OK  {elapsed:.1f}s  {size/1024:.0f} KB  sha256={digest[:16]}…")
            record.update({"status": "ok", "build_time_s": round(elapsed, 1),
                           "bin_size_B": size, "sha256": digest})

        manifest.append(record)

        # Write manifest after each build so partial progress is visible
        manifest_path = os.path.join(args.manifest_out, "corpus_builds.json")
        with open(manifest_path, "w") as f:
            json.dump({"builds": manifest, "pairs": PAIRS}, f, indent=2)

    total = time.monotonic() - start_all
    ok    = sum(1 for r in manifest if r["status"] in ("ok", "skipped"))
    failed = sum(1 for r in manifest if r["status"] == "failed")

    print(f"\n{'='*60}")
    print(f"  Done in {total/60:.1f} min — {ok} ok, {failed} failed")
    print(f"  Manifest → {os.path.join(args.manifest_out, 'corpus_builds.json')}")
    print(f"{'='*60}\n")

    if failed:
        print(f"WARNING: {failed} build(s) failed. Check logs in {args.manifest_out}")
        sys.exit(1)

    # ── write meta.json for bench_diff.py ──────────────────────────────────
    meta_path = os.path.join(args.out, "meta.json")
    builds_meta = {}
    for r in manifest:
        if r["status"] in ("ok", "skipped"):
            builds_meta[r["label"]] = {
                "branch": r["branch"], "sha": r["git_sha"],
                "size": r["bin_size_B"], "sha256": r["sha256"],
            }

    meta = {"builds": builds_meta, "pairs": PAIRS}
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"meta.json → {meta_path}")


if __name__ == "__main__":
    main()
