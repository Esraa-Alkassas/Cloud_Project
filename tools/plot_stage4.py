#!/usr/bin/env python3
"""
Stage 4 — generate the three required plots.
"""

import json
import csv
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

DATA_DIR  = Path(__file__).parent.parent / "results" / "stage4" / "data"
PLOTS_DIR = Path(__file__).parent.parent / "results" / "stage4" / "plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 11,
    "figure.dpi": 150,
})


# ── Plot 1: Lambda duration cold vs warm + device rtt_ms ─────────────────────
def plot_lambda_duration():
    with open(DATA_DIR / "lambda_summary.json") as f:
        s = json.load(f)

    warm_dur  = s["warm"]["duration_ms"]["mean"]
    warm_std  = s["warm"]["duration_ms"]["std"]
    cold_exec = s["cold"]["duration_ms"]["mean"]
    cold_std  = s["cold"]["duration_ms"]["std"]
    cold_init = s["cold"]["init_ms"]["mean"]
    cold_init_std = s["cold"]["init_ms"]["std"]
    rtt_mean  = s["device_rtt_ms"]["mean"]
    rtt_std   = s["device_rtt_ms"]["std"]

    fig, ax = plt.subplots(figsize=(8, 5))

    x = np.array([0, 1, 2])
    labels = ["Warm\n(exec)", "Cold\n(exec)", "Cold\n(init+exec)"]
    means  = [warm_dur, cold_exec, cold_init + cold_exec]
    errs   = [warm_std, cold_std, math.sqrt(cold_init_std**2 + cold_std**2)]
    colors = ["#4C72B0", "#DD8452", "#DD8452"]
    hatches = ["", "", "//"]

    bars = ax.bar(x, means, yerr=errs, capsize=5,
                  color=colors, edgecolor="black", linewidth=0.8, alpha=0.85,
                  hatch=[h for h in hatches])

    # Overlay rtt_ms as dashed line
    ax.axhline(rtt_mean, color="crimson", linestyle="--", linewidth=1.8,
               label=f"Device rtt_ms = {rtt_mean:.0f} ± {rtt_std:.0f} ms (N={s['device_rtt_ms']['n']})")
    ax.fill_between([-0.5, 2.5],
                    rtt_mean - rtt_std, rtt_mean + rtt_std,
                    color="crimson", alpha=0.08)

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Duration (ms)")
    ax.set_title("Lambda Execution Duration — Cold vs Warm Start\nwith Device-Observed RTT Overlay")
    ax.set_xlim(-0.6, 2.6)
    ax.set_ylim(0, max(means) * 1.35)
    ax.legend(fontsize=9)

    for i, (m, e) in enumerate(zip(means, errs)):
        ax.text(i, m + e + 10, f"{m:.0f}", ha="center", va="bottom", fontsize=9)

    warm_patch  = mpatches.Patch(color="#4C72B0", alpha=0.85, label=f"Warm (N={s['warm']['n']})")
    cold_patch  = mpatches.Patch(color="#DD8452", alpha=0.85, label=f"Cold (N={s['cold']['n']})")
    rtt_line    = mpatches.Patch(color="crimson", alpha=0.4, label="Device RTT (API GW + Lambda + net)")
    ax.legend(handles=[warm_patch, cold_patch, rtt_line,
                       mpatches.Patch(color="crimson", linestyle="--", fill=False,
                                      label=f"rtt_ms = {rtt_mean:.0f} ± {rtt_std:.0f} ms")],
              fontsize=8.5, loc="upper left")

    fig.tight_layout()
    out = PLOTS_DIR / "plot1_lambda_duration.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"[plot1] Saved {out}")


# ── Plot 2: Patch-generation time vs k ───────────────────────────────────────
def plot_patchgen_scaling():
    with open(DATA_DIR / "patchgen_summary.json") as f:
        s = json.load(f)

    rows = s["sweep"]
    k_vals  = [r["k"] for r in rows]
    t_means = [r["elapsed_s_mean"] for r in rows]
    t_stds  = [r["elapsed_s_std"]  for r in rows]

    # Linear fit
    k_arr = np.array(k_vals, dtype=float)
    t_arr = np.array(t_means, dtype=float)
    a = float(np.dot(k_arr, t_arr) / np.dot(k_arr, k_arr))

    # Projection
    k_proj = np.array([1, 2, 4, 8, 16, 25, 50, 100])
    t_proj = a * k_proj

    fig, ax = plt.subplots(figsize=(8, 5))

    ax.errorbar(k_vals, t_means, yerr=t_stds, fmt="o-", color="#4C72B0",
                capsize=5, linewidth=2, markersize=7, label="Measured (mean ± std, N=3 reps)")
    ax.plot(k_proj, t_proj, "--", color="gray", linewidth=1.5,
            label=f"Linear fit: t ≈ {a:.3f}×k  (O(n))")

    ax.set_xlabel("Number of prior versions (k)")
    ax.set_ylabel("Patch-generation time (s)")
    ax.set_title("CI Patch-Generation Time vs. Number of Prior Versions\n"
                 "(crle compress + AES-CTR encrypt per patch)")
    ax.set_xlim(0, 105)
    ax.set_xticks([0, 10, 20, 40, 60, 80, 100])
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.35)

    for k, t, s_ in zip(k_vals, t_means, t_stds):
        ax.annotate(f"{t:.2f}s", (k, t), textcoords="offset points",
                    xytext=(4, 6), fontsize=8)

    fig.tight_layout()
    out = PLOTS_DIR / "plot2_patchgen_scaling.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"[plot2] Saved {out}")


# ── Plot 3: Cost per 1,000-device rollout — delta vs full ────────────────────
def plot_rollout_cost():
    with open(DATA_DIR / "cost_model.json") as f:
        m = json.load(f)

    rollout = m["rollout_1000_devices"]
    delta   = rollout["delta"]
    full    = rollout["full"]

    components = ["Lambda", "API Gateway", "S3 GET", "S3 Transfer"]
    delta_vals = [
        delta["lambda_usd"],
        delta["apigw_usd"],
        delta["s3_get_usd"],
        delta["s3_xfer_usd"],
    ]
    full_vals = [
        full["lambda_usd"],
        full["apigw_usd"],
        full["s3_get_usd"],
        full["s3_xfer_usd"],
    ]

    x = np.arange(len(components))
    w = 0.35
    colors = ["#4C72B0", "#DD8452", "#55A868", "#C44E52"]

    fig, ax = plt.subplots(figsize=(9, 5))

    bottom_d = np.zeros(len(components))
    bottom_f = np.zeros(len(components))
    for i, (comp, dv, fv, col) in enumerate(zip(components, delta_vals, full_vals, colors)):
        ax.bar(0, dv, w, bottom=bottom_d[0], color=col, edgecolor="white",
               linewidth=0.5, label=comp)
        ax.bar(1, fv, w, bottom=bottom_f[0], color=col, edgecolor="white",
               linewidth=0.5)
        bottom_d[0] += dv
        bottom_f[0] += fv

    # Total labels
    ax.text(0, delta["total_usd"] + 0.0002, f"${delta['total_usd']:.4f}",
            ha="center", va="bottom", fontsize=10, fontweight="bold", color="#4C72B0")
    ax.text(1, full["total_usd"] + 0.0002, f"${full['total_usd']:.4f}",
            ha="center", va="bottom", fontsize=10, fontweight="bold", color="#DD8452")

    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Delta OTA", "Full-Image OTA"])
    ax.set_ylabel("Cost (USD)")
    ax.set_title("Cost per 1,000-Device Rollout (MODEL ESTIMATE)\n"
                 "us-east-1, 2026-06-15 AWS pricing")
    ax.legend(title="Component", fontsize=9, loc="upper left")
    ax.set_xlim(-0.5, 1.5)

    ax.annotate(f"Delta is {full['total_usd']/delta['total_usd']:.1f}× cheaper",
                xy=(0.5, max(delta["total_usd"], full["total_usd"]) * 0.6),
                fontsize=10, ha="center", color="darkgreen",
                fontweight="bold")

    fig.tight_layout()
    out = PLOTS_DIR / "plot3_rollout_cost.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"[plot3] Saved {out}")


if __name__ == "__main__":
    plot_lambda_duration()
    plot_patchgen_scaling()
    plot_rollout_cost()
    print("All plots done.")
