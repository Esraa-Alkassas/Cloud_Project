#!/usr/bin/env python3
"""
bench_diff.py — Stage 1: patch-size / efficiency benchmark across diff tools.

Commands:
  bench_diff.py run   --corpus corpus/ --out results/stage1/ [--tools all] [--reps 5]
  bench_diff.py table --in results/stage1/results.csv --out results/stage1/
  bench_diff.py plot  --in results/stage1/results.csv --out results/stage1/plots/
  bench_diff.py check-enc --key 1234567890123456 --iv abcdefghijklmnop --file <path>

Tool tags: detools-crle  detools-none  detools-heatshrink  detools-lzma
           bsdiff  hdiffpatch  xdelta3-0  xdelta3-9
Pseudo:    full  full_gz
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── tool registry ─────────────────────────────────────────────────────────────
# fmt keys: {old}=old binary, {new}=new binary, {patch}=patch output, {out}=apply output

TOOL_DEFS = {
    "detools-crle": {
        "create": ["detools", "create_patch", "-c", "crle",        "{old}", "{new}", "{patch}"],
        "apply":  ["detools", "apply_patch",                        "{old}", "{patch}", "{out}"],
        "ver":    ["detools", "--version"],
        "note":   "deployed config — only crle compiled into ESP32 component",
    },
    "detools-none": {
        "create": ["detools", "create_patch", "-c", "none",        "{old}", "{new}", "{patch}"],
        "apply":  ["detools", "apply_patch",                        "{old}", "{patch}", "{out}"],
        "ver":    ["detools", "--version"],
        "note":   "differencing only, no compression",
    },
    "detools-heatshrink": {
        "create": ["detools", "create_patch", "-c", "heatshrink",  "{old}", "{new}", "{patch}"],
        "apply":  ["detools", "apply_patch",                        "{old}", "{patch}", "{out}"],
        "ver":    ["detools", "--version"],
        "note":   "device-deployable candidate (one component-CMakeLists line)",
    },
    "detools-lzma": {
        "create": ["detools", "create_patch", "-c", "lzma",        "{old}", "{new}", "{patch}"],
        "apply":  ["detools", "apply_patch",                        "{old}", "{patch}", "{out}"],
        "ver":    ["detools", "--version"],
        "note":   "NOT device-deployable — lzma decompressor absent from ESP32 component",
    },
    "bsdiff": {
        "create": ["bsdiff",  "{old}", "{new}", "{patch}"],
        "apply":  ["bspatch", "{old}", "{out}", "{patch}"],
        "ver":    None,
        "note":   "classic bsdiff 4.3 (bzip2-internal)",
    },
    "hdiffpatch": {
        "create": ["hdiffz",  "-f", "{old}", "{new}", "{patch}"],
        "apply":  ["hpatchz", "-f", "{old}", "{patch}", "{out}"],
        "ver":    ["hdiffz",  "--version"],
        "note":   "HDiffPatch default settings",
    },
    "xdelta3-0": {
        "create": ["xdelta3", "-e", "-0", "-f", "-s", "{old}", "{new}", "{patch}"],
        "apply":  ["xdelta3", "-d", "-f", "-s", "{old}", "{patch}", "{out}"],
        "ver":    ["xdelta3", "-V"],
        "note":   "VCDIFF, no secondary compression",
    },
    "xdelta3-9": {
        "create": ["xdelta3", "-e", "-9", "-f", "-s", "{old}", "{new}", "{patch}"],
        "apply":  ["xdelta3", "-d", "-f", "-s", "{old}", "{patch}", "{out}"],
        "ver":    ["xdelta3", "-V"],
        "note":   "VCDIFF + max secondary (LZMA) compression",
    },
}

PSEUDO_TOOLS = ["full", "full_gz"]
ALL_TOOLS    = list(TOOL_DEFS) + PSEUDO_TOOLS

RESULT_FIELDS = [
    "pair_old", "pair_new", "scenario", "description",
    "tool", "tool_version", "command_line", "note",
    "old_size_B", "new_size_B", "patch_size_B",
    "compression_factor", "reduction_pct",
    "gen_time_s_median", "gen_time_s_min", "gen_time_s_max",
    "gen_peak_mem_MB", "verified",
    "sha256_old", "sha256_new", "sha256_patch",
]

# ── helpers ───────────────────────────────────────────────────────────────────

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_available(name: str) -> bool:
    defn = TOOL_DEFS.get(name)
    if not defn:
        return name in PSEUDO_TOOLS
    binary = defn["create"][0]
    return shutil.which(binary) is not None


def get_tool_version(ver_cmd) -> str:
    if not ver_cmd:
        return "unknown"
    try:
        r = subprocess.run(ver_cmd, capture_output=True, text=True, timeout=5)
        out = (r.stdout + r.stderr).strip()
        return out.splitlines()[0][:80] if out else "unknown"
    except Exception:
        return "unknown"


def fill_cmd(template: list, old: str, new: str, patch: str, out: str) -> list:
    return [s.format(old=old, new=new, patch=patch, out=out) for s in template]


def measure_rss_mb(cmd: list) -> float | None:
    """Run cmd once via /usr/bin/time -v; return peak RSS in MB or None."""
    time_bin = "/usr/bin/time"
    if not os.path.exists(time_bin):
        return None
    r = subprocess.run([time_bin, "-v"] + cmd, capture_output=True)
    for line in r.stderr.decode("utf-8", errors="replace").splitlines():
        if "Maximum resident set size" in line:
            try:
                return int(line.split(":")[1].strip()) / 1024.0
            except (ValueError, IndexError):
                pass
    return None


def run_timed(cmd: list, reps: int) -> tuple[list | None, int, str]:
    """Run cmd reps times; return (wall_times_list, returncode, last_stderr)."""
    times, rc, err = [], 0, ""
    for _ in range(reps):
        t0 = time.perf_counter()
        r  = subprocess.run(cmd, capture_output=True)
        t1 = time.perf_counter()
        rc  = r.returncode
        err = r.stderr.decode("utf-8", errors="replace")
        if rc != 0:
            return None, rc, err
        times.append(t1 - t0)
    return times, 0, ""


def probe_detools_hdiff() -> bool:
    """Return True if installed detools supports --algorithm hdiffpatch."""
    try:
        r = subprocess.run(["detools", "create_patch", "--help"],
                           capture_output=True, text=True, timeout=5)
        return "hdiffpatch" in (r.stdout + r.stderr)
    except Exception:
        return False


def safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── core benchmark ────────────────────────────────────────────────────────────

def bench_pair_tool(tool: str, old_path: str, new_path: str,
                    pair_meta: dict, reps: int, patches_dir: str) -> dict | None:
    """Return one results-CSV row dict, or None if the tool is unavailable."""

    old_size  = os.path.getsize(old_path)
    new_size  = os.path.getsize(new_path)
    sha_old   = sha256_of(old_path)
    sha_new   = sha256_of(new_path)
    pair_old  = pair_meta["old"]
    pair_new  = pair_meta["new"]
    scenario  = pair_meta.get("scenario", "")
    desc      = pair_meta.get("description", pair_meta.get("note", ""))

    base = {
        "pair_old": pair_old, "pair_new": pair_new,
        "scenario": scenario, "description": desc,
        "old_size_B": old_size, "new_size_B": new_size,
        "sha256_old": sha_old,  "sha256_new": sha_new,
    }

    # ── pseudo-tool: full ─────────────────────────────────────────────────────
    if tool == "full":
        return {**base,
                "tool": "full", "tool_version": "n/a",
                "command_line": "full OTA (no delta)",
                "note": "baseline — ships entire new binary",
                "patch_size_B": new_size,
                "compression_factor": 1.0,
                "reduction_pct": 0.0,
                "gen_time_s_median": 0, "gen_time_s_min": 0, "gen_time_s_max": 0,
                "gen_peak_mem_MB": 0, "verified": True,
                "sha256_patch": sha_new}

    # ── pseudo-tool: full_gz ──────────────────────────────────────────────────
    if tool == "full_gz":
        gz_path = os.path.join(patches_dir,
                               f"{pair_old}_to_{pair_new}_full.gz")
        with open(new_path, "rb") as fin, \
             gzip.open(gz_path, "wb", compresslevel=9) as fout:
            shutil.copyfileobj(fin, fout)
        patch_size = os.path.getsize(gz_path)
        cf  = new_size / patch_size if patch_size else 0
        red = 100.0 * (1 - patch_size / new_size) if new_size else 0
        return {**base,
                "tool": "full_gz", "tool_version": "gzip-9",
                "command_line": "gzip -9 <new_binary>",
                "note": "trivial alternative — gzip of full image",
                "patch_size_B": patch_size,
                "compression_factor": round(cf, 4),
                "reduction_pct": round(red, 2),
                "gen_time_s_median": 0, "gen_time_s_min": 0, "gen_time_s_max": 0,
                "gen_peak_mem_MB": 0, "verified": True,
                "sha256_patch": sha256_of(gz_path)}

    # ── real tools ────────────────────────────────────────────────────────────
    if not tool_available(tool):
        print(f"    [{tool:20s}] SKIP — binary not found")
        return None

    defn        = TOOL_DEFS[tool]
    patch_name  = f"{pair_old}_to_{pair_new}_{tool}.bin"
    patch_path  = os.path.join(patches_dir, patch_name)
    create_cmd  = fill_cmd(defn["create"], old_path, new_path, patch_path, "")
    ver         = get_tool_version(defn["ver"])

    # Peak RSS (single run before timing loop to avoid overhead)
    rss = measure_rss_mb(create_cmd)

    # Timing
    times, rc, err = run_timed(create_cmd, reps)
    if rc != 0:
        short_err = err[:120].replace("\n", " ")
        print(f"    [{tool:20s}] CREATE FAILED: {short_err}")
        return {**base,
                "tool": tool, "tool_version": ver,
                "command_line": " ".join(create_cmd),
                "note": f"FAILED: {short_err}",
                "patch_size_B": 0,
                "compression_factor": 0, "reduction_pct": 0,
                "gen_time_s_median": 0, "gen_time_s_min": 0, "gen_time_s_max": 0,
                "gen_peak_mem_MB": rss or 0, "verified": False,
                "sha256_patch": ""}

    patch_size = os.path.getsize(patch_path)
    cf  = new_size / patch_size if patch_size else 0
    red = 100.0 * (1 - patch_size / new_size) if new_size else 0

    # Verify: apply patch to old, compare sha256 to new
    verified   = False
    tmp_fd, out_path = tempfile.mkstemp(suffix=".bin")
    os.close(tmp_fd)
    try:
        apply_cmd = fill_cmd(defn["apply"], old_path, new_path, patch_path, out_path)
        r = subprocess.run(apply_cmd, capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path):
            verified = sha256_of(out_path) == sha_new
        if not verified:
            err_snip = r.stderr.decode("utf-8", errors="replace")[:80]
            print(f"    [{tool:20s}] VERIFY FAILED  {err_snip}")
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    return {**base,
            "tool": tool, "tool_version": ver,
            "command_line": " ".join(create_cmd),
            "note": defn.get("note", ""),
            "patch_size_B": patch_size,
            "compression_factor": round(cf, 4),
            "reduction_pct": round(red, 2),
            "gen_time_s_median": round(statistics.median(times), 4),
            "gen_time_s_min":    round(min(times), 4),
            "gen_time_s_max":    round(max(times), 4),
            "gen_peak_mem_MB":   round(rss, 2) if rss is not None else "",
            "verified": verified,
            "sha256_patch": sha256_of(patch_path)}


# ── command: run ──────────────────────────────────────────────────────────────

def cmd_run(args):
    meta_path = os.path.join(args.corpus, "meta.json")
    if not os.path.exists(meta_path):
        sys.exit(f"ERROR: {meta_path} not found — run make_corpus.py build/pair first")
    with open(meta_path) as f:
        meta = json.load(f)

    pairs = meta.get("pairs", [])
    if not pairs:
        sys.exit("ERROR: no pairs in meta.json — run make_corpus.py pair first")

    # Tool selection
    if args.tools.lower() == "all":
        tools = list(TOOL_DEFS.keys())
        # Auto-detect detools-hdiff bonus row
        if probe_detools_hdiff():
            TOOL_DEFS["detools-hdiff"] = {
                "create": ["detools", "create_patch", "--algorithm", "hdiffpatch",
                           "{old}", "{new}", "{patch}"],
                "apply":  ["detools", "apply_patch", "{old}", "{out}", "{patch}"],
                "ver":    ["detools", "--version"],
                "note":   "bonus row — detools hdiffpatch algorithm",
            }
            tools.append("detools-hdiff")
        tools += PSEUDO_TOOLS
    else:
        tools = [t.strip() for t in args.tools.split(",")]
        bad = [t for t in tools if t not in ALL_TOOLS and t not in TOOL_DEFS]
        if bad:
            sys.exit(f"Unknown tools: {bad}\nAvailable: {ALL_TOOLS}")

    os.makedirs(args.out, exist_ok=True)
    patches_dir  = os.path.join(args.out, "patches")
    os.makedirs(patches_dir, exist_ok=True)
    results_path = os.path.join(args.out, "results.csv")
    write_header = not os.path.exists(results_path)

    with open(results_path, "a", newline="") as csvf:
        writer = csv.DictWriter(csvf, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        for pair in pairs:
            old_lbl  = pair["old"]
            new_lbl  = pair["new"]
            scenario = pair.get("scenario", "?")
            old_path = os.path.join(args.corpus, f"{old_lbl}.bin")
            new_path = os.path.join(args.corpus, f"{new_lbl}.bin")

            print(f"\n[{scenario}] {old_lbl} → {new_lbl}")

            if not os.path.exists(old_path):
                print(f"  SKIP: {old_path} not found"); continue
            if not os.path.exists(new_path):
                print(f"  SKIP: {new_path} not found"); continue

            for tool in tools:
                print(f"  {tool:22s} ...", end=" ", flush=True)
                row = bench_pair_tool(tool, old_path, new_path, pair,
                                      args.reps, patches_dir)
                if row:
                    writer.writerow(row)
                    csvf.flush()
                    cf  = row.get("compression_factor", "?")
                    red = row.get("reduction_pct", "?")
                    v   = "✓" if row.get("verified") else "✗"
                    print(f"cf={cf:<7}  red={red}%  {v}")

    print(f"\nResults → {results_path}")


# ── command: table ────────────────────────────────────────────────────────────

def cmd_table(args):
    if not os.path.exists(args.inp):
        sys.exit(f"ERROR: {args.inp} not found")
    rows = list(csv.DictReader(open(args.inp)))
    if not rows:
        sys.exit("No rows in results CSV")

    os.makedirs(args.out, exist_ok=True)

    scenarios = sorted({r["scenario"] for r in rows if r["scenario"]})
    tools     = list(dict.fromkeys(r["tool"] for r in rows))  # insertion-order dedup

    # Aggregate
    cf_data  = {t: {s: [] for s in scenarios} for t in tools}
    red_data = {t: {s: [] for s in scenarios} for t in tools}
    for row in rows:
        t  = row["tool"]
        s  = row["scenario"]
        cf = safe_float(row.get("compression_factor"))
        rd = safe_float(row.get("reduction_pct"))
        if t in cf_data and s in cf_data[t]:
            if cf is not None: cf_data[t][s].append(cf)
            if rd is not None: red_data[t][s].append(rd)

    def stat(vals):
        if not vals: return ("", "")
        m = statistics.mean(vals)
        s = statistics.stdev(vals) if len(vals) > 1 else 0.0
        return (round(m, 3), round(s, 3))

    def fmt(vals):
        m, s = stat(vals)
        if m == "": return ""
        return f"{m} ± {s}"

    # ── summary CSV ──────────────────────────────────────────────────────────
    sum_fields = ["tool"] + [f"cf_{s}_mean" for s in scenarios] + \
                            [f"cf_{s}_std"  for s in scenarios] + \
                            [f"red_{s}_mean" for s in scenarios] + \
                            [f"red_{s}_std"  for s in scenarios]
    sum_rows = []
    for t in tools:
        row = {"tool": t}
        for s in scenarios:
            m, sd = stat(cf_data[t][s])
            row[f"cf_{s}_mean"] = m
            row[f"cf_{s}_std"]  = sd
            m, sd = stat(red_data[t][s])
            row[f"red_{s}_mean"] = m
            row[f"red_{s}_std"]  = sd
        sum_rows.append(row)

    sum_csv = os.path.join(args.out, "summary.csv")
    with open(sum_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sum_fields)
        w.writeheader()
        w.writerows(sum_rows)
    print(f"Summary CSV → {sum_csv}")

    # ── Markdown table ────────────────────────────────────────────────────────
    def md_table(title: str, data: dict) -> list[str]:
        col_w = max(len(t) for t in tools) + 2
        lines = [f"## {title}", ""]
        hdr = f"| {'tool':{col_w}} |" + "".join(f" {s:>18} |" for s in scenarios)
        sep = f"|{'-'*(col_w+2)}|" + "|".join("-" * 20 for _ in scenarios) + "|"
        lines += [hdr, sep]
        for t in tools:
            row = f"| {t:{col_w}} |"
            for s in scenarios:
                row += f" {fmt(data[t][s]):>18} |"
            lines.append(row)
        return lines

    md_lines = (md_table("Compression Factor — mean ± std  (new_size / patch_size)", cf_data) +
                [""] +
                md_table("Reduction %  — mean ± std  (100 × (1 − patch/new))", red_data))

    md_path = os.path.join(args.out, "summary.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md_lines) + "\n")
    print(f"Markdown    → {md_path}")
    print()
    print("\n".join(md_lines))


# ── command: plot ─────────────────────────────────────────────────────────────

def cmd_plot(args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib not installed — pip install matplotlib")

    if not os.path.exists(args.inp):
        sys.exit(f"ERROR: {args.inp} not found")
    rows = list(csv.DictReader(open(args.inp)))
    if not rows:
        sys.exit("No rows in results CSV")

    os.makedirs(args.out, exist_ok=True)

    tools     = list(dict.fromkeys(r["tool"] for r in rows))
    scenarios = sorted({r["scenario"] for r in rows if r["scenario"]})
    pairs     = list(dict.fromkeys(f"{r['pair_old']}→{r['pair_new']}" for r in rows))

    COLORS = ["#2196F3","#4CAF50","#FF9800","#F44336","#9C27B0",
              "#00BCD4","#FF5722","#8BC34A","#FFC107","#607D8B"]
    SCEN_COLOR = {"NU": "#2196F3", "MN": "#4CAF50", "MJ": "#FF9800", "WC": "#F44336"}

    # ── Plot 1: grouped bar — patch_size_B per pair, grouped by tool, log Y ──
    fig, ax = plt.subplots(figsize=(max(10, len(pairs) * len(tools) * 0.18 + 2), 5))
    n = len(tools)
    width = 0.8 / n
    xs = list(range(len(pairs)))

    for i, tool in enumerate(tools):
        sizes = []
        for pair in pairs:
            ol, nl = pair.split("→", 1)
            vals = [safe_float(r["patch_size_B"])
                    for r in rows
                    if r["tool"] == tool and r["pair_old"] == ol and r["pair_new"] == nl]
            sizes.append(vals[0] if vals and vals[0] else 1)  # 1 so log scale works
        offsets = [x - 0.4 + (i + 0.5) * width for x in xs]
        ax.bar(offsets, sizes, width, label=tool, color=COLORS[i % len(COLORS)])

    ax.set_yscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels(pairs, rotation=35, ha="right", fontsize=7)
    ax.set_ylabel("Patch size (bytes, log scale)")
    ax.set_title("Patch size per pair × tool")
    ax.legend(fontsize=7, ncol=2)
    out1 = os.path.join(args.out, "patch_size_by_pair_and_tool.png")
    fig.tight_layout()
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print(f"Plot 1 → {out1}")

    # ── Plot 2: strip — compression_factor per scenario per tool ─────────────
    fig, ax = plt.subplots(figsize=(max(8, len(tools) * 1.4), 5))
    scen_offsets = {s: (i - (len(scenarios) - 1) / 2) * 0.22
                    for i, s in enumerate(sorted(scenarios))}

    for i, tool in enumerate(tools):
        xi = i
        for scen in scenarios:
            vals = [safe_float(r["compression_factor"])
                    for r in rows
                    if r["tool"] == tool and r["scenario"] == scen
                    and safe_float(r["compression_factor"]) is not None]
            if not vals:
                continue
            jx = xi + scen_offsets.get(scen, 0)
            ax.scatter([jx] * len(vals), vals,
                       color=SCEN_COLOR.get(scen, "#999"),
                       s=50, alpha=0.8, zorder=3,
                       label=scen if i == 0 else "")

    ax.set_xticks(range(len(tools)))
    ax.set_xticklabels(tools, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("Compression factor (new_size / patch_size)")
    ax.set_title("Compression factor by tool and scenario")
    handles = [plt.scatter([], [], color=c, s=50, label=s)
               for s, c in SCEN_COLOR.items() if s in scenarios]
    ax.legend(handles=handles, title="scenario")
    out2 = os.path.join(args.out, "compression_factor_by_scenario.png")
    fig.tight_layout()
    fig.savefig(out2, dpi=150)
    plt.close(fig)
    print(f"Plot 2 → {out2}")

    # ── Plot 3: gen_time bar — median across MN pairs ─────────────────────────
    mn_medians = []
    for tool in tools:
        vals = [safe_float(r["gen_time_s_median"])
                for r in rows
                if r["tool"] == tool and r["scenario"] == "MN"
                and safe_float(r["gen_time_s_median"]) not in (None, 0)]
        mn_medians.append(statistics.median(vals) if vals else 0)

    fig, ax = plt.subplots(figsize=(max(6, len(tools) * 0.9), 4))
    ax.bar(range(len(tools)), mn_medians,
           color=[COLORS[i % len(COLORS)] for i in range(len(tools))])
    ax.set_xticks(range(len(tools)))
    ax.set_xticklabels(tools, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("Patch gen time (s, median)")
    ax.set_title("Patch generation time — median across MN pairs")
    out3 = os.path.join(args.out, "gen_time_mn_pairs.png")
    fig.tight_layout()
    fig.savefig(out3, dpi=150)
    plt.close(fig)
    print(f"Plot 3 → {out3}")


# ── command: check-enc ────────────────────────────────────────────────────────

def cmd_check_enc(args):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        sys.exit("cryptography not installed — pip install cryptography")

    key = args.key.encode("utf-8")
    iv  = args.iv.encode("utf-8")
    if len(key) != 16:
        sys.exit(f"ERROR: key must be exactly 16 bytes, got {len(key)}")
    if len(iv) != 16:
        sys.exit(f"ERROR: iv must be exactly 16 bytes, got {len(iv)}")

    with open(args.file, "rb") as f:
        plaintext = f.read()
    pt_len = len(plaintext)

    cipher     = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
    encryptor  = cipher.encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    ct_len     = len(ciphertext)

    assert ct_len == pt_len, f"SIZE MISMATCH: pt={pt_len}  ct={ct_len}"

    print(f"check-enc result:")
    print(f"  file            = {args.file}")
    print(f"  plaintext_len   = {pt_len} bytes")
    print(f"  ciphertext_len  = {ct_len} bytes")
    print(f"  AES-128-CTR size-preserving: CONFIRMED  (ct_len == pt_len)")
    print(f"  sha256(pt) = {hashlib.sha256(plaintext).hexdigest()}")
    print(f"  sha256(ct) = {hashlib.sha256(ciphertext).hexdigest()}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="Benchmark all (pair × tool) combinations")
    sp.add_argument("--corpus", default="corpus/")
    sp.add_argument("--out",    default="results/stage1/")
    sp.add_argument("--tools",  default="all",
                    help="'all' or comma-separated subset, e.g. detools-crle,bsdiff")
    sp.add_argument("--reps",   type=int, default=5,
                    help="Timing repetitions per (pair, tool)")

    sp = sub.add_parser("table", help="Emit summary tables (CSV + Markdown)")
    sp.add_argument("--in",  dest="inp", required=True)
    sp.add_argument("--out", default="results/stage1/")

    sp = sub.add_parser("plot", help="Generate the three Stage 1 plots")
    sp.add_argument("--in",  dest="inp", required=True)
    sp.add_argument("--out", default="results/stage1/plots/")

    sp = sub.add_parser("check-enc", help="Verify AES-128-CTR is size-preserving")
    sp.add_argument("--key",  required=True, help="16-char ASCII key")
    sp.add_argument("--iv",   required=True, help="16-char ASCII IV")
    sp.add_argument("--file", required=True, help="Any patch or binary file to encrypt")

    args = p.parse_args()
    {"run": cmd_run, "table": cmd_table,
     "plot": cmd_plot, "check-enc": cmd_check_enc}[args.cmd](args)


if __name__ == "__main__":
    main()
