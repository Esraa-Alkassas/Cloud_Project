#!/usr/bin/env python3
"""
Stage 4 — patchgen_bench.py
Benchmarks patch-generation time vs number of prior versions (k).
Mirrors the real CI create_patch + AES-CTR-encrypt step.
Sweep: k = 1, 2, 4, 8, 16.
"""

import argparse
import csv
import io
import json
import os
import random
import struct
import sys
import tempfile
import time
from pathlib import Path

# AES-CTR encrypt mirroring CI (from build_corpus.py / ota_pipeline.yml)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import detools

CORPUS_DIR  = Path(__file__).parent.parent / "corpus" / "bins"
OUT_DIR     = Path(__file__).parent.parent / "results" / "stage4" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# AES key/IV hardcoded as in CI (same as firmware Kconfig)
AES_KEY = bytes.fromhex("0123456789abcdef0123456789abcdef")
AES_IV  = bytes.fromhex("0123456789abcdef0123456789abcdef")

K_SWEEP = [1, 2, 4, 8, 16]


def aes_ctr_encrypt(data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(AES_KEY), modes.CTR(AES_IV))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def create_patch_bytes(old_path: Path, new_path: Path) -> bytes:
    """Create a detools patch (crle compression) and return raw bytes."""
    buf = io.BytesIO()
    with open(old_path, "rb") as old_f, open(new_path, "rb") as new_f:
        detools.create_patch(old_f, new_f, buf, compression="crle")
    return buf.getvalue()


def load_corpus() -> list[Path]:
    """Return sorted list of binary paths from corpus/bins/."""
    bins = sorted(CORPUS_DIR.glob("*.bin"))
    if not bins:
        sys.exit(f"[patchgen] No .bin files in {CORPUS_DIR}. Aborting.")
    return bins


def build_version_list(corpus: list[Path], k_max: int) -> list[Path]:
    """
    Build a list of k_max 'prior versions' by cycling through the corpus.
    The 'new' version is always corpus[-1]; prior versions are corpus[:-1]
    repeated/extended as needed.
    """
    prior_pool = corpus[:-1]
    if not prior_pool:
        # Only one binary; duplicate it slightly offset for a trivial patch
        prior_pool = [corpus[0]]
    extended: list[Path] = []
    while len(extended) < k_max:
        extended.extend(prior_pool)
    return extended[:k_max]


def bench_k(new_bin: Path, prior_versions: list[Path]) -> dict:
    """
    For a given set of prior versions, run:
      - detools create_patch (crle) from each prior to new_bin
      - AES-CTR encrypt the resulting patch
    Return timing dict.
    """
    total_start = time.perf_counter()
    patch_sizes = []
    encrypted_sizes = []

    for old_bin in prior_versions:
        patch_bytes = create_patch_bytes(old_bin, new_bin)
        enc_bytes   = aes_ctr_encrypt(patch_bytes)
        patch_sizes.append(len(patch_bytes))
        encrypted_sizes.append(len(enc_bytes))

    elapsed = time.perf_counter() - total_start
    return {
        "k":              len(prior_versions),
        "elapsed_s":      elapsed,
        "n_patches":      len(prior_versions),
        "mean_patch_b":   sum(patch_sizes) / len(patch_sizes) if patch_sizes else 0,
        "mean_enc_b":     sum(encrypted_sizes) / len(encrypted_sizes) if encrypted_sizes else 0,
        "total_patch_b":  sum(patch_sizes),
        "total_enc_b":    sum(encrypted_sizes),
    }


def main():
    ap = argparse.ArgumentParser(description="Stage 4 patch-generation scaling benchmark")
    ap.add_argument("--repeats", type=int, default=3,
                    help="Repetitions per k value (default 3)")
    ap.add_argument("--k-values", nargs="+", type=int, default=K_SWEEP,
                    help=f"k values to sweep (default: {K_SWEEP})")
    args = ap.parse_args()

    corpus = load_corpus()
    new_bin = corpus[-1]
    k_max = max(args.k_values)

    print(f"[patchgen] Corpus: {len(corpus)} bins in {CORPUS_DIR}")
    print(f"[patchgen] New binary: {new_bin.name} ({new_bin.stat().st_size/1024:.1f} KB)")
    print(f"[patchgen] k sweep: {args.k_values}, repeats={args.repeats}")

    prior_pool = build_version_list(corpus, k_max)
    print(f"[patchgen] Prior version pool: {[p.name for p in prior_pool]}")

    rows: list[dict] = []
    for k in args.k_values:
        prior_k = prior_pool[:k]
        rep_times = []
        for rep in range(args.repeats):
            r = bench_k(new_bin, prior_k)
            rep_times.append(r["elapsed_s"])
            print(f"  k={k:3d}  rep={rep+1}  elapsed={r['elapsed_s']:.3f}s  "
                  f"patches={r['n_patches']}  mean_patch={r['mean_patch_b']/1024:.1f}KB")
        mean_t = sum(rep_times) / len(rep_times)
        std_t  = (sum((t - mean_t)**2 for t in rep_times) / len(rep_times)) ** 0.5
        rows.append({
            "k":          k,
            "elapsed_s_mean": mean_t,
            "elapsed_s_std":  std_t,
            "elapsed_s_min":  min(rep_times),
            "elapsed_s_max":  max(rep_times),
            "n_patches":  k,
            "mean_patch_bytes": r["mean_patch_b"],
            "total_patch_bytes": r["total_patch_b"],
        })

    # Write CSV
    csv_path = OUT_DIR / "patchgen_bench.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n[patchgen] Wrote {csv_path}")

    # Write JSON summary
    json_path = OUT_DIR / "patchgen_summary.json"
    with open(json_path, "w") as f:
        json.dump({"sweep": rows, "new_binary": new_bin.name,
                   "corpus_size": len(corpus)}, f, indent=2)
    print(f"[patchgen] Wrote {json_path}")

    print("\n=== Patch-Generation Scaling ===")
    print(f"  {'k':>4}  {'time (s)':>10}  {'±std':>8}  {'time/patch (s)':>16}")
    for r in rows:
        tpp = r["elapsed_s_mean"] / r["k"] if r["k"] else 0
        print(f"  {r['k']:>4}  {r['elapsed_s_mean']:>10.3f}  "
              f"±{r['elapsed_s_std']:>6.3f}  {tpp:>16.3f}")

    # O(n) projection
    if len(rows) >= 2:
        # Fit linear: time ~ a*k
        k_vals = [r["k"] for r in rows]
        t_vals = [r["elapsed_s_mean"] for r in rows]
        a = sum(k*t for k,t in zip(k_vals,t_vals)) / sum(k*k for k in k_vals)
        print(f"\n  Linear fit: time ≈ {a:.4f} × k  (O(n) growth)")
        print(f"\n  Projection:")
        print(f"  {'k':>6}  {'est. time (s)':>14}  {'est. time (min)':>16}")
        for k_proj in [25, 50, 100, 200]:
            est = a * k_proj
            print(f"  {k_proj:>6}  {est:>14.1f}  {est/60:>16.2f}")


if __name__ == "__main__":
    main()
