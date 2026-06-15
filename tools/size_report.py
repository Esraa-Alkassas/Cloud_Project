#!/usr/bin/env python3
"""
size_report.py — Capture ESP-IDF binary size metrics into CSVs using esp_idf_size.

Parses the build map file (build/wifi_station.map) and writes:
  - results/stage3/size_summary.csv  — total + per-region breakdown
  - results/stage3/size_report.csv   — per-component sizes (focus: ota/detools/mbedtls/metrics)

Must be run from the repo root (or with --build-dir and --out set).

Usage:
  python tools/size_report.py [--build-dir build] [--out results/stage3]
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time

FOCUS_COMPONENTS = {"ota", "detools", "mbedtls", "metrics", "main"}
SUBPROCESS_TIMEOUT = 60   # seconds


def run_size_cmd(map_file, extra_args, label):
    cmd = [sys.executable, "-m", "esp_idf_size", "--format", "json"] + extra_args + [map_file]
    print(f"  [size_report] {label}: {' '.join(cmd)}")
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=SUBPROCESS_TIMEOUT, text=True)
        elapsed = time.monotonic() - t0
        if r.returncode != 0:
            print(f"  [size_report] {label} error (rc={r.returncode}):\n{r.stderr[:500]}")
            return None
        print(f"  [size_report] {label} done in {elapsed:.1f}s")
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        print(f"  [size_report] {label} timed out after {SUBPROCESS_TIMEOUT}s")
        return None
    except json.JSONDecodeError as e:
        print(f"  [size_report] {label} JSON parse error: {e}")
        return None
    except FileNotFoundError as e:
        print(f"  [size_report] esp_idf_size not found: {e}")
        return None


def _extract_component_name(archive_name):
    """
    Turn an archive filename into a short component name.
    e.g. 'libmbedtls.a'   → 'mbedtls'
         'libmain.a'       → 'main'
         'libdetools.a'    → 'detools'
         'libmetrics.a'    → 'metrics'
    """
    import re
    name = archive_name.split("/")[-1]        # drop path prefix
    name = re.sub(r'^lib', '', name)           # strip lib prefix
    name = re.sub(r'\.a$', '', name)           # strip .a suffix
    return name.strip() or archive_name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", default="build",
                    help="ESP-IDF build directory (default: build)")
    ap.add_argument("--out", default="results/stage3",
                    help="Output directory for CSVs (default: results/stage3)")
    args = ap.parse_args()

    build_dir = os.path.abspath(args.build_dir)
    map_file  = os.path.join(build_dir, "wifi_station.map")
    if not os.path.exists(map_file):
        sys.exit(f"ERROR: map file not found: {map_file}\n"
                 f"Build the firmware first: idf.py build")

    os.makedirs(args.out, exist_ok=True)

    # ── Total size breakdown ─────────────────────────────────────────────────
    total_data = run_size_cmd(map_file, [], "total size")
    if total_data is None:
        total_data = {}

    summary_fields = [
        ("used_dram",         total_data.get("used_dram",    0)),
        ("dram_data",         total_data.get("dram_data",    0)),
        ("dram_bss",          total_data.get("dram_bss",     0)),
        ("used_iram",         total_data.get("used_iram",    0)),
        ("flash_code",        total_data.get("flash_code",   0)),
        ("flash_rodata",      total_data.get("flash_rodata", 0)),
        ("total_size",        total_data.get("total_size",   0)),
    ]

    summary_csv = os.path.join(args.out, "size_summary.csv")
    with open(summary_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["region", "bytes"])
        for name, val in summary_fields:
            w.writerow([name, val])
    print(f"  [size_report] Summary → {summary_csv}")
    for name, val in summary_fields:
        print(f"    {name:25s}  {val:>9d} B  ({val/1024:.1f} kB)")

    # ── Per-archive/component breakdown ──────────────────────────────────────
    arch_data = run_size_cmd(map_file, ["--archives"], "per-archive size")
    if arch_data is None:
        arch_data = {}

    # Aggregate by component name (archives with same component-name sum together)
    agg = {}
    for archive, fields in arch_data.items():
        if not isinstance(fields, dict):
            continue
        comp = _extract_component_name(archive)
        if comp not in agg:
            agg[comp] = {"component": comp, "total_bytes": 0,
                         "flash_code": 0, "flash_rodata": 0,
                         "dram_data": 0, "dram_bss": 0, "iram": 0}
        agg[comp]["total_bytes"]  += fields.get("flash_total", 0) + fields.get("ram_st_total", 0)
        agg[comp]["flash_code"]   += fields.get(".flash.text", 0)
        agg[comp]["flash_rodata"] += fields.get(".flash.rodata", 0)
        agg[comp]["dram_data"]    += fields.get(".dram0.data", 0)
        agg[comp]["dram_bss"]     += fields.get(".dram0.bss",  0)
        agg[comp]["iram"]         += fields.get(".iram0.text", 0)

    components = list(agg.values())

    def _sort_key(c):
        name = c["component"].lower()
        is_focus = any(f in name for f in FOCUS_COMPONENTS)
        return (0 if is_focus else 1, -c["total_bytes"])

    components.sort(key=_sort_key)

    report_csv = os.path.join(args.out, "size_report.csv")
    with open(report_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "component", "total_bytes", "flash_code", "flash_rodata",
            "dram_data", "dram_bss", "iram"
        ])
        w.writeheader()
        w.writerows(components)
    print(f"\n  [size_report] Component CSV → {report_csv} ({len(components)} components)")

    # Print focus components
    print("\n  Focus components (updater stack):")
    for c in components:
        if any(f in c["component"].lower() for f in FOCUS_COMPONENTS):
            print(f"    {c['component']:25s}  total={c['total_bytes']:>8d} B  "
                  f"code={c['flash_code']:>7d}  rodata={c['flash_rodata']:>7d}  "
                  f"iram={c['iram']:>7d}  bss={c['dram_bss']:>7d}")

    print(f"\n  [size_report] Done.")


if __name__ == "__main__":
    main()
