#!/usr/bin/env python3
"""
harness.py — Serial capture, storage, and plotting for ESP32 ##M## metric lines.

IMPORTANT: Only ONE process can hold the serial port at a time.
Close `idf.py monitor` before running `capture` or `tail`.

Usage:
  harness.py capture --port /dev/ttyUSB0 --baud 115200 --db runs.db \\
                     --experiment EXP_ID [--scenario MN1] [--note "..."] [--duration 600]
  harness.py export  --db runs.db --experiment EXP_ID --out exp.csv
  harness.py plot    --db runs.db --experiment EXP_ID --out plots/
  harness.py plot3   --db runs.db --experiments MN_DELTA,MN_FULL,NU_DELTA \\
                     [--size-csv results/stage3/size_report.csv] --out plots/
  harness.py tail    --port /dev/ttyUSB0 [--baud 115200]
  harness.py campaign --port /dev/ttyUSB0 --db runs.db --experiment EXP \\
                      --version-a VER_A --version-b VER_B --runs 10 [--settle 20]
                      [--rssi-min -75]
"""

import argparse
import csv
import json
import os
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timezone

METRIC_PREFIX = "##M## "
SCHEMA_VERSIONS = (2, 3)   # accept both v2 (Stage 2) and v3 (Stage 3)

PHASE_COLS = ["t_http", "t_decrypt", "t_from_read", "t_flash", "t_seek"]
PHASE_LABELS = ["HTTP download", "AES decrypt", "Flash read (old)", "Flash write (new)", "Seek"]
PHASE_COLORS = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3"]

# §0.2 supervision constants
SILENCE_IDLE_S  = 60    # board considered hung if no serial line for this long while idle
SILENCE_OTA_S   = 30    # stricter silence window while an OTA is in flight
SILENCE_CAP_S   = 120   # silence window for standalone capture sessions


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            id          TEXT PRIMARY KEY,
            started_utc REAL,
            scenario    TEXT,
            note        TEXT,
            fw_old      TEXT,
            fw_new      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_id       TEXT,
            recv_utc     REAL,
            dev          TEXT,
            fw           TEXT,
            seq          INTEGER,
            t_us         INTEGER,
            ev           TEXT,
            payload_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parse_errors (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_id   TEXT,
            recv_utc REAL,
            raw      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_failures (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_id   TEXT,
            recv_utc REAL,
            reason   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            exp_id               TEXT,
            run_num              INTEGER,
            direction            TEXT,
            fw_from              TEXT,
            fw_to                TEXT,
            ok                   INTEGER,
            is_delta             INTEGER,
            err                  INTEGER,
            t_total_ms           REAL,
            t_connect_ms         REAL,
            t_apply_wall_ms      REAL,
            t_http               REAL,
            t_decrypt            REAL,
            t_from_read          REAL,
            t_flash              REAL,
            t_seek               REAL,
            t_finalize_ms        REAL,
            b_patch              INTEGER,
            b_flash              INTEGER,
            b_from               INTEGER,
            calls_r              INTEGER,
            calls_w              INTEGER,
            rssi                 INTEGER,
            heap_min_during      INTEGER,
            t_reboot_ms          REAL,
            t_boot_app_ms        REAL,
            t_downtime_ms        REAL,
            goodput_kBps         REAL,
            flash_kBps           REAL,
            t_unaccounted_ms     REAL,
            recv_utc_summary     REAL,
            recv_utc_reboot      REAL,
            recv_utc_boot        REAL,
            valid                INTEGER,
            notes                TEXT
        )
    """)
    conn.commit()
    return conn


def insert_experiment(conn, exp_id, scenario, note):
    conn.execute(
        "INSERT OR IGNORE INTO experiments (id, started_utc, scenario, note) VALUES (?,?,?,?)",
        (exp_id, time.time(), scenario, note),
    )
    conn.commit()


def insert_event(conn, exp_id, recv_utc, parsed):
    dev  = parsed.get("dev", "")
    fw   = parsed.get("fw", "")
    seq  = parsed.get("seq")
    t_us = parsed.get("t_us")
    ev   = parsed.get("ev", "")
    extra = {k: v for k, v in parsed.items()
             if k not in ("v", "seq", "t_us", "fw", "dev", "ev")}
    conn.execute(
        "INSERT INTO events (exp_id, recv_utc, dev, fw, seq, t_us, ev, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (exp_id, recv_utc, dev, fw, seq, t_us, ev, json.dumps(extra)),
    )
    conn.commit()


def insert_error(conn, exp_id, recv_utc, raw):
    conn.execute(
        "INSERT INTO parse_errors (exp_id, recv_utc, raw) VALUES (?,?,?)",
        (exp_id, recv_utc, raw),
    )
    conn.commit()


def insert_run_failure(conn, exp_id, reason):
    conn.execute(
        "INSERT INTO run_failures (exp_id, recv_utc, reason) VALUES (?,?,?)",
        (exp_id, time.time(), reason),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

def parse_metric_line(line: str):
    """Return parsed dict or raise ValueError."""
    if not line.startswith(METRIC_PREFIX):
        raise ValueError("not a metric line")
    payload = line[len(METRIC_PREFIX):].strip()
    obj = json.loads(payload)
    if obj.get("v") not in SCHEMA_VERSIONS:
        raise ValueError(f"unknown schema version {obj.get('v')}")
    return obj


# ---------------------------------------------------------------------------
# Derived-column computation
# ---------------------------------------------------------------------------

def compute_derived_for_summary(summary_payload: dict,
                                recv_utc_reboot: float,
                                recv_utc_boot: float,
                                boot_payload: dict) -> dict:
    """Compute all derived columns given the three events for one run."""
    d = {}
    t_http  = summary_payload.get("t_http",  0) or 0
    t_flash = summary_payload.get("t_flash", 0) or 0
    t_apply = summary_payload.get("t_apply_wall_ms", 0) or 0
    t_dec   = summary_payload.get("t_decrypt",   0) or 0
    t_from  = summary_payload.get("t_from_read", 0) or 0
    t_seek  = summary_payload.get("t_seek",  0) or 0
    b_patch = summary_payload.get("b_patch", 0) or 0
    b_flash = summary_payload.get("b_flash", 0) or 0

    # throughput
    d["goodput_kBps"] = round(b_patch / t_http, 2) if t_http > 0 else None
    d["flash_kBps"]   = round(b_flash / t_flash, 2) if t_flash > 0 else None

    # reboot/downtime (harness wall-clock difference, sub-100 ms serial jitter)
    if recv_utc_reboot and recv_utc_boot:
        d["t_reboot_ms"] = round((recv_utc_boot - recv_utc_reboot) * 1000, 1)
    else:
        d["t_reboot_ms"] = None

    t_app_ms = (boot_payload or {}).get("t_app_ms", None)
    d["t_boot_app_ms"] = t_app_ms
    if d["t_reboot_ms"] is not None and t_app_ms is not None:
        d["t_downtime_ms"] = round(d["t_reboot_ms"] + t_app_ms, 1)
    else:
        d["t_downtime_ms"] = None

    # unaccounted time (diff-CPU + scheduling overhead)
    phase_sum = t_http + t_dec + t_from + t_flash + t_seek
    d["t_unaccounted_ms"] = round(t_apply - phase_sum, 1) if t_apply > 0 else None
    if d["t_unaccounted_ms"] is not None and t_apply > 0:
        frac = d["t_unaccounted_ms"] / t_apply
        if d["t_unaccounted_ms"] < 0:
            print(f"  [WARN] t_unaccounted negative ({d['t_unaccounted_ms']} ms) — check timers")
        elif frac > 0.25:
            print(f"  [WARN] t_unaccounted {frac:.0%} of wall time — investigate")

    # --- Stage 3 v3 resource-footprint derived columns ---
    heap_start = summary_payload.get("heap_start")
    heap_min   = summary_payload.get("heap_min_during")
    heap_total = (boot_payload or {}).get("heap_total")
    stack_size = (boot_payload or {}).get("stack_size")
    stack_hwm  = summary_payload.get("stack_hwm")
    calls_w    = summary_payload.get("calls_w") or 0
    part_size  = summary_payload.get("part_size")

    if heap_start is not None and heap_min is not None:
        d["heap_used"] = int(heap_start) - int(heap_min)
        if heap_total:
            d["heap_used_pct"] = round(d["heap_used"] / heap_total * 100, 2)
        else:
            d["heap_used_pct"] = None
    else:
        d["heap_used"] = None
        d["heap_used_pct"] = None

    if stack_size and stack_hwm is not None:
        d["stack_used_pct"] = round((stack_size - stack_hwm) / stack_size * 100, 2)
    else:
        d["stack_used_pct"] = None

    if b_flash and calls_w:
        d["mean_bytes_per_write"] = round(b_flash / calls_w, 2)
    else:
        d["mean_bytes_per_write"] = None

    if part_size and b_patch:
        d["wear_ratio"] = round(part_size / b_patch, 2)
    else:
        d["wear_ratio"] = None

    return d


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_capture(args):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed — run: pip install pyserial")

    os.makedirs("logs", exist_ok=True)
    ts_str   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rawlog   = os.path.join("logs", f"{args.experiment}_{ts_str}.rawlog")
    conn     = open_db(args.db)
    insert_experiment(conn, args.experiment, args.scenario, args.note)

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1, dsrdtr=False, rtscts=False)
    except serial.SerialException as e:
        msg = str(e)
        if "Permission" in msg or "busy" in msg.lower() or "Device or resource busy" in msg:
            sys.exit(f"ERROR: Cannot open {args.port} — close idf.py monitor first.\n{msg}")
        sys.exit(f"ERROR opening serial port: {msg}")

    print(f"Capturing on {args.port} @ {args.baud} → {rawlog}")
    print(f"Experiment: {args.experiment}  Duration: {args.duration or '∞'}s")
    print("Press Ctrl-C to stop early.\n")

    deadline = (time.monotonic() + args.duration) if args.duration else None
    last_seq  = None
    event_count = 0
    last_line_time = time.monotonic()

    try:
        with open(rawlog, "w", encoding="utf-8", errors="replace") as logf:
            while True:
                if deadline and time.monotonic() >= deadline:
                    print(f"\nDuration reached. {event_count} metric events stored.")
                    break

                # §0.2 silence detection for capture
                silence = time.monotonic() - last_line_time
                if silence > SILENCE_CAP_S:
                    print(f"\n  [capture] No data for {silence:.0f}s — attempting serial re-open")
                    try:
                        ser.close()
                        time.sleep(1)
                        ser = serial.Serial(args.port, args.baud, timeout=1,
                                            dsrdtr=False, rtscts=False)
                        last_line_time = time.monotonic()
                        print("  [capture] Serial re-opened")
                    except Exception as e:
                        print(f"  [capture] Re-open failed: {e} — stopping")
                        break

                try:
                    raw = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    continue
                if not raw:
                    continue
                last_line_time = time.monotonic()
                logf.write(raw + "\n")
                logf.flush()
                recv_utc = time.time()

                if METRIC_PREFIX not in raw:
                    continue

                idx = raw.find(METRIC_PREFIX)
                line = raw[idx:]

                try:
                    parsed = parse_metric_line(line)
                except Exception as exc:
                    print(f"  [WARN] parse error: {exc} | raw: {raw[:120]}")
                    insert_error(conn, args.experiment, recv_utc, raw)
                    continue

                seq = parsed.get("seq", 0)
                ev  = parsed.get("ev", "")
                if ev == "boot":
                    if last_seq is not None:
                        print(f"  [NOTICE] Device rebooted (seq reset {last_seq} → {seq})")
                    last_seq = seq
                elif last_seq is not None and seq != last_seq + 1:
                    print(f"  [NOTICE] seq gap: expected {last_seq + 1}, got {seq}"
                          f" (dropped {seq - last_seq - 1} lines)")
                    last_seq = seq
                else:
                    last_seq = seq

                insert_event(conn, args.experiment, recv_utc, parsed)
                event_count += 1
                print(f"  [{ev:14s}] seq={seq} t_us={parsed.get('t_us')} fw={parsed.get('fw')}")

    except KeyboardInterrupt:
        print(f"\nStopped. {event_count} metric events stored.")
    finally:
        ser.close()
        conn.close()


def cmd_export(args):
    conn = open_db(args.db)
    rows = conn.execute(
        "SELECT * FROM events WHERE exp_id=? ORDER BY id", (args.experiment,)
    ).fetchall()
    if not rows:
        print(f"No events found for experiment '{args.experiment}'")
        conn.close()
        return

    # Build event index for derived-column join
    events_list = [dict(r) for r in rows]
    for r in events_list:
        try:
            r["_payload"] = json.loads(r.get("payload_json") or "{}")
        except Exception:
            r["_payload"] = {}

    # Collect all payload keys + derived column names
    all_payload_keys = set()
    for r in events_list:
        all_payload_keys.update(r["_payload"].keys())

    derived_keys = ["goodput_kBps", "flash_kBps", "t_reboot_ms", "t_boot_app_ms",
                    "t_downtime_ms", "t_unaccounted_ms",
                    # Stage 3 v3 derived
                    "heap_used", "heap_used_pct", "stack_used_pct",
                    "mean_bytes_per_write", "wear_ratio"]
    all_payload_keys.update(derived_keys)

    base_cols = ["id", "exp_id", "recv_utc", "dev", "fw", "seq", "t_us", "ev"]
    extra_cols = sorted(all_payload_keys)
    fieldnames = base_cols + extra_cols

    # Build lookup structures for derived column joins
    reboot_by_seq = {}   # seq → recv_utc for ota_reboot events
    boot_events   = []   # list of (recv_utc, payload) sorted by recv_utc

    for r in events_list:
        if r["ev"] == "ota_reboot":
            reboot_by_seq[r["seq"]] = r["recv_utc"]
        if r["ev"] == "boot":
            boot_events.append((r["recv_utc"], r["_payload"]))

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in events_list:
            record = {k: r.get(k, "") for k in base_cols}
            record.update({k: r["_payload"].get(k, "") for k in extra_cols})

            # Compute derived columns for ota_summary rows
            if r["ev"] == "ota_summary":
                # Find the most recent ota_reboot before this event
                recv_utc_reboot = None
                for seq_r, utc_r in reboot_by_seq.items():
                    if utc_r < r["recv_utc"]:
                        if recv_utc_reboot is None or utc_r > recv_utc_reboot:
                            recv_utc_reboot = utc_r

                # Find first boot event after this summary
                recv_utc_boot = None
                boot_payload  = {}
                for utc_b, payload_b in boot_events:
                    if utc_b > r["recv_utc"]:
                        recv_utc_boot = utc_b
                        boot_payload  = payload_b
                        break

                derived = compute_derived_for_summary(
                    r["_payload"], recv_utc_reboot, recv_utc_boot, boot_payload)
                for k, v in derived.items():
                    record[k] = v if v is not None else ""

            writer.writerow({k: record.get(k, "") for k in fieldnames})

    print(f"Exported {len(rows)} events → {args.out}")
    conn.close()


def _load_ota_summaries(conn, exp_id):
    """Return list of dicts with all ota_summary payload fields."""
    rows = conn.execute(
        "SELECT recv_utc, payload_json FROM events"
        " WHERE exp_id=? AND ev='ota_summary' ORDER BY id",
        (exp_id,),
    ).fetchall()
    results = []
    for r in rows:
        try:
            d = json.loads(r["payload_json"] or "{}")
            d["_recv_utc"] = r["recv_utc"]
            results.append(d)
        except Exception:
            pass
    return results


def _load_boot_events(conn, exp_id):
    """Return list of (recv_utc, payload) for boot events."""
    rows = conn.execute(
        "SELECT recv_utc, payload_json FROM events"
        " WHERE exp_id=? AND ev='boot' ORDER BY id",
        (exp_id,),
    ).fetchall()
    results = []
    for r in rows:
        try:
            d = json.loads(r["payload_json"] or "{}")
            results.append((r["recv_utc"], d))
        except Exception:
            pass
    return results


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def cmd_plot(args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        sys.exit("matplotlib/numpy not installed — run: pip install matplotlib numpy")

    os.makedirs(args.out, exist_ok=True)
    conn = open_db(args.db)
    exp  = args.experiment

    # --- Heap plot ---
    rows = conn.execute(
        "SELECT t_us, payload_json FROM events WHERE exp_id=? AND ev='heartbeat' ORDER BY id",
        (exp,),
    ).fetchall()

    if rows:
        t_s, heap_free, heap_min = [], [], []
        for row in rows:
            try:
                extra = json.loads(row["payload_json"] or "{}")
                t_s.append(row["t_us"] / 1e6)
                heap_free.append(extra.get("heap_free", 0))
                heap_min.append(extra.get("heap_min", 0))
            except Exception:
                pass

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(t_s, [v / 1024 for v in heap_free], label="heap_free (KB)")
        ax.plot(t_s, [v / 1024 for v in heap_min],  label="heap_min (KB)", linestyle="--")
        ax.set_xlabel("t since boot (s)")
        ax.set_ylabel("Heap (KB)")
        ax.set_title(f"Heap — {exp}")
        ax.legend()
        p = os.path.join(args.out, f"{exp}_heap.png")
        fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
        print(f"Heap plot → {p}")

    # --- Stage 2: stacked bar of mean phase times ---
    summaries = _load_ota_summaries(conn, exp)
    if summaries:
        _plot_phase_bar(summaries, exp, args.out)
        _plot_total_box(summaries, exp, args.out)
        _plot_throughput_scatter(summaries, exp, args.out)

    # --- Event count summary ---
    ev_rows = conn.execute(
        "SELECT ev, COUNT(*) AS cnt FROM events WHERE exp_id=? GROUP BY ev ORDER BY cnt DESC",
        (exp,),
    ).fetchall()

    if ev_rows:
        evs   = [r["ev"]  for r in ev_rows]
        cnts  = [r["cnt"] for r in ev_rows]
        fig, ax = plt.subplots(figsize=(max(6, len(evs) * 1.4), 4))
        ax.bar(evs, cnts)
        ax.set_xlabel("Event type")
        ax.set_ylabel("Count")
        ax.set_title(f"Event counts — {exp}")
        p = os.path.join(args.out, f"{exp}_event_counts.png")
        fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
        print(f"Event count plot → {p}")

    conn.close()


def _plot_phase_bar(summaries, exp, out_dir):
    """Stacked bar of mean phase times for all ota_summary events."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return

    delta_rows = [s for s in summaries if s.get("is_delta") == 1 and s.get("ok") == 1]
    full_rows  = [s for s in summaries if s.get("is_delta") == 0 and s.get("ok") == 1]

    groups = []
    if delta_rows:
        groups.append(("Delta", delta_rows))
    if full_rows:
        groups.append(("Full", full_rows))
    if not groups:
        return

    fig, ax = plt.subplots(figsize=(max(4, len(groups) * 3), 5))
    x = np.arange(len(groups))
    width = 0.5
    bottoms = np.zeros(len(groups))

    for col, label, color in zip(PHASE_COLS, PHASE_LABELS, PHASE_COLORS):
        means = [np.mean([r.get(col, 0) or 0 for r in rows]) for _, rows in groups]
        ax.bar(x, means, width, bottom=bottoms, label=label, color=color)
        bottoms += np.array(means)

    # Add t_unaccounted (diff-CPU) as last segment
    def _unaccounted(r):
        wall  = r.get("t_apply_wall_ms", 0) or 0
        phsum = sum(r.get(c, 0) or 0 for c in PHASE_COLS)
        return max(0.0, wall - phsum)

    unac_means = [np.mean([_unaccounted(r) for r in rows]) for _, rows in groups]
    ax.bar(x, unac_means, width, bottom=bottoms, label="Diff-CPU / overhead", color="#937860")

    ax.set_xticks(x)
    ax.set_xticklabels([g[0] for g in groups])
    ax.set_ylabel("Time (ms)")
    ax.set_title(f"OTA phase breakdown (mean) — {exp}")
    ax.legend(loc="upper right", fontsize=8)
    p = os.path.join(out_dir, f"{exp}_phase_bar.png")
    fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
    print(f"Phase bar → {p}")


def _plot_total_box(summaries, exp, out_dir):
    """Box plot of t_total_ms per update type."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    delta_totals = [s["t_total_ms"] for s in summaries
                    if s.get("is_delta") == 1 and s.get("ok") == 1 and s.get("t_total_ms")]
    full_totals  = [s["t_total_ms"] for s in summaries
                    if s.get("is_delta") == 0 and s.get("ok") == 1 and s.get("t_total_ms")]

    data, labels = [], []
    if delta_totals:
        data.append(delta_totals); labels.append("Delta")
    if full_totals:
        data.append(full_totals);  labels.append("Full")
    if not data:
        return

    fig, ax = plt.subplots(figsize=(max(4, len(data) * 2.5), 5))
    ax.boxplot(data, labels=labels, patch_artist=True)
    ax.set_ylabel("t_total_ms")
    ax.set_title(f"Total OTA time distribution — {exp}")
    p = os.path.join(out_dir, f"{exp}_total_box.png")
    fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
    print(f"Box plot → {p}")


def _plot_throughput_scatter(summaries, exp, out_dir):
    """Scatter of t_http vs b_patch with fitted line."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return

    pts = [(s["b_patch"] / 1024, s["t_http"])
           for s in summaries
           if s.get("b_patch") and s.get("t_http") and s.get("ok") == 1]
    if len(pts) < 2:
        return

    bx, ty = zip(*pts)
    bx, ty = np.array(bx), np.array(ty)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.scatter(bx, ty, s=40, zorder=3)

    # Linear fit
    coeffs = np.polyfit(bx, ty, 1)
    x_fit  = np.linspace(bx.min(), bx.max(), 100)
    ax.plot(x_fit, np.polyval(coeffs, x_fit), "r--",
            label=f"fit: {1/coeffs[0]:.1f} kB/s" if coeffs[0] > 0 else "fit")

    ax.set_xlabel("Patch size (kB)")
    ax.set_ylabel("HTTP download time (ms)")
    ax.set_title(f"Effective HTTP throughput — {exp}")
    ax.legend()
    p = os.path.join(out_dir, f"{exp}_throughput_scatter.png")
    fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
    print(f"Throughput scatter → {p}")


# ---------------------------------------------------------------------------
# Stage 3 cross-experiment plots (plot3)
# ---------------------------------------------------------------------------

def cmd_plot3(args):
    """Generate the three Stage 3 cross-experiment plots."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        sys.exit("matplotlib/numpy not installed — run: pip install matplotlib numpy")

    os.makedirs(args.out, exist_ok=True)
    conn = open_db(args.db)
    exp_ids = [e.strip() for e in args.experiments.split(",") if e.strip()]

    # Gather per-experiment aggregate data
    exp_data = {}
    for exp_id in exp_ids:
        summaries  = _load_ota_summaries(conn, exp_id)
        boot_evs   = _load_boot_events(conn, exp_id)
        ok_sums    = [s for s in summaries if s.get("ok") == 1]

        heap_total = None
        for _, bp in boot_evs:
            if bp.get("heap_total"):
                heap_total = bp["heap_total"]
                break

        heap_min_vals       = [s.get("heap_min_during") for s in ok_sums if s.get("heap_min_during")]
        heap_start_vals     = [s.get("heap_start")      for s in ok_sums if s.get("heap_start")]
        b_patch_vals        = [s.get("b_patch")         for s in ok_sums if s.get("b_patch")]
        b_flash_vals        = [s.get("b_flash")         for s in ok_sums if s.get("b_flash")]
        part_size_vals      = [s.get("part_size")       for s in ok_sums if s.get("part_size")]
        heap_used_vals      = []
        for s in ok_sums:
            hs = s.get("heap_start")
            hm = s.get("heap_min_during")
            if hs is not None and hm is not None:
                heap_used_vals.append(hs - hm)

        exp_data[exp_id] = {
            "heap_total":  heap_total,
            "heap_min":    _mean(heap_min_vals),
            "heap_start":  _mean(heap_start_vals),
            "heap_used":   _mean(heap_used_vals),
            "b_patch":     _mean(b_patch_vals),
            "b_flash":     _mean(b_flash_vals),
            "part_size":   _mean(part_size_vals),
            "n_ok":        len(ok_sums),
        }

    conn.close()

    labels = exp_ids
    x = np.arange(len(labels))
    width = 0.35

    # ── Plot 1: Heap bar (lowest free heap + heap consumed + total reference) ──
    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 2.5), 5))

    heap_min_kb   = [exp_data[e]["heap_min"]  / 1024 if exp_data[e]["heap_min"]  else 0
                     for e in labels]
    heap_used_kb  = [exp_data[e]["heap_used"] / 1024 if exp_data[e]["heap_used"] else 0
                     for e in labels]
    heap_total_kb = [exp_data[e]["heap_total"] / 1024 if exp_data[e]["heap_total"] else None
                     for e in labels]

    bars1 = ax.bar(x - width/2, heap_min_kb,  width, label="Lowest free heap (KB)",   color="#4C72B0")
    bars2 = ax.bar(x + width/2, heap_used_kb, width, label="Heap consumed by OTA (KB)", color="#C44E52")

    # Reference line: total heap (use first non-None value as it's constant)
    ht = next((h for h in heap_total_kb if h), None)
    if ht:
        ax.axhline(ht, color="gray", linestyle="--", linewidth=1.5,
                   label=f"Total heap ({ht:.0f} KB)")

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Heap (KB)")
    ax.set_title("Memory footprint during OTA update")
    ax.legend(fontsize=9)
    p1 = os.path.join(args.out, "stage3_heap_bar.png")
    fig.tight_layout(); fig.savefig(p1, dpi=120); plt.close(fig)
    print(f"Heap bar → {p1}")

    # ── Plot 2: Bytes bar (download / write / erase) — log scale ──
    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 2.5), 5))

    b_patch_kb    = [exp_data[e]["b_patch"]   / 1024 if exp_data[e]["b_patch"]    else 1e-3
                     for e in labels]
    b_flash_kb    = [exp_data[e]["b_flash"]   / 1024 if exp_data[e]["b_flash"]    else 1e-3
                     for e in labels]
    part_size_kb  = [exp_data[e]["part_size"] / 1024 if exp_data[e]["part_size"]  else 1e-3
                     for e in labels]

    w3 = width * 0.9
    ax.bar(x - w3,    b_patch_kb,   w3, label="Downloaded (kB)",      color="#4C72B0")
    ax.bar(x,         b_flash_kb,   w3, label="Written to flash (kB)", color="#DD8452")
    ax.bar(x + w3,    part_size_kb, w3, label="Erased (partition, kB)", color="#C44E52")

    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Bytes (log scale)")
    ax.set_title("OTA data-volume breakdown — fixed-erase cost vs patch size")
    ax.legend(fontsize=9)
    p2 = os.path.join(args.out, "stage3_bytes_bar.png")
    fig.tight_layout(); fig.savefig(p2, dpi=120); plt.close(fig)
    print(f"Bytes bar → {p2}")

    # ── Plot 3: Component sizes from size_report.csv ──
    if args.size_csv and os.path.exists(args.size_csv):
        _plot_component_sizes(args.size_csv, args.out)
    else:
        print(f"  [plot3] size_csv not found ({args.size_csv}) — skipping component plot")


def _plot_component_sizes(size_csv: str, out_dir: str):
    """Horizontal bar of updater component sizes from size_report.csv."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    components = {}
    with open(size_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("component", "").strip()
            try:
                total = int(row.get("total_bytes", 0) or 0)
            except (ValueError, TypeError):
                total = 0
            if name and total > 0:
                components[name] = total

    if not components:
        print("  [plot3] size_report.csv has no component data — skipping")
        return

    # Sort by size descending
    items = sorted(components.items(), key=lambda x: x[1], reverse=True)
    names  = [i[0] for i in items]
    sizes  = [i[1] / 1024 for i in items]   # kB

    fig, ax = plt.subplots(figsize=(8, max(4, len(names) * 0.45)))
    bars = ax.barh(names[::-1], sizes[::-1], color="#4C72B0")
    ax.set_xlabel("Size (kB)")
    ax.set_title("Updater component flash footprint")
    for bar, sz in zip(bars, sizes[::-1]):
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                f"{sz:.1f} kB", va="center", fontsize=8)
    p = os.path.join(out_dir, "stage3_component_sizes.png")
    fig.tight_layout(); fig.savefig(p, dpi=120); plt.close(fig)
    print(f"Component sizes → {p}")


def cmd_tail(args):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed — run: pip install pyserial")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1, dsrdtr=False, rtscts=False)
    except serial.SerialException as e:
        msg = str(e)
        if "Permission" in msg or "busy" in msg.lower() or "Device or resource busy" in msg:
            sys.exit(f"ERROR: Cannot open {args.port} — close idf.py monitor first.\n{msg}")
        sys.exit(f"ERROR opening serial port: {msg}")

    print(f"Tailing {args.port} @ {args.baud}  (Ctrl-C to quit)\n")
    try:
        while True:
            try:
                raw = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                continue
            if not raw:
                continue
            if METRIC_PREFIX in raw:
                idx = raw.find(METRIC_PREFIX)
                line = raw[idx:]
                try:
                    parsed = parse_metric_line(line)
                    ev  = parsed.get("ev", "?")
                    seq = parsed.get("seq", "?")
                    t   = parsed.get("t_us", 0)
                    extra = {k: v for k, v in parsed.items()
                             if k not in ("v", "seq", "t_us", "fw", "dev", "ev")}
                    print(f"  \033[32m[{ev:14s}]\033[0m seq={seq:>5}  t={t/1e6:9.2f}s  {extra}")
                except Exception:
                    print(f"  \033[33m[METRIC?]\033[0m {raw}")
            else:
                print(f"  \033[90m{raw}\033[0m")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


# ---------------------------------------------------------------------------
# Campaign command
# ---------------------------------------------------------------------------

def _flip_manifest(version: str, flip_script: str):
    """Call flip_manifest.py set-latest --version <version>."""
    result = subprocess.run(
        [sys.executable, flip_script, "set-latest", "--version", version],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"flip_manifest failed:\n{result.stderr.strip()}")
    print(f"  [manifest] set-latest → {version}")


def _restore_manifest(flip_script: str):
    try:
        result = subprocess.run(
            [sys.executable, flip_script, "restore"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"  [manifest] restore warning: {result.stderr.strip()}")
        else:
            print("  [manifest] restored")
    except Exception as e:
        print(f"  [manifest] restore error: {e}")


def cmd_campaign(args):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed — run: pip install pyserial")

    flip_script = os.path.join(os.path.dirname(__file__), "flip_manifest.py")
    if not os.path.exists(flip_script):
        sys.exit(f"flip_manifest.py not found at {flip_script}")

    os.makedirs("logs", exist_ok=True)
    ts_str   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rawlog   = os.path.join("logs", f"{args.experiment}_campaign_{ts_str}.rawlog")
    conn     = open_db(args.db)
    insert_experiment(conn, args.experiment, getattr(args, "scenario", None),
                      f"campaign A={args.version_a} B={args.version_b} N={args.runs}")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1, dsrdtr=False, rtscts=False)
    except serial.SerialException as e:
        msg = str(e)
        if "Permission" in msg or "busy" in msg.lower() or "Device or resource busy" in msg:
            sys.exit(f"ERROR: Cannot open {args.port} — close idf.py monitor first.\n{msg}")
        sys.exit(f"ERROR opening serial port: {msg}")

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    STATE_IDLE         = "IDLE"
    STATE_NEED_FLIP    = "NEED_FLIP"
    STATE_WAITING_OTA  = "WAITING_OTA"
    STATE_WAITING_BOOT = "WAITING_BOOT"

    state          = STATE_IDLE
    fw_current     = None
    run_num        = 0
    consecutive_failures = 0
    circuit_breaker_limit = getattr(args, "max_failures", 3)
    runs_a2b       = 0
    runs_b2a       = 0
    target_runs    = args.runs

    # Per-run bookkeeping
    summary_payload  = {}
    recv_utc_summary = None
    recv_utc_reboot  = None
    boot_payload_cur = {}
    direction        = None

    # Timing
    state_entered_at = time.monotonic()
    last_line_time   = time.monotonic()
    OTA_TIMEOUT      = 10 * 60    # 10 min hard wall-clock per run

    last_seq  = None
    log_f     = open(rawlog, "w", encoding="utf-8", errors="replace")

    def _read_line():
        try:
            return ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            return ""

    def _record_run(ok, fw_from, fw_to, direction, summary_p,
                    recv_utc_s, recv_utc_r, recv_utc_b, boot_p, notes=""):
        if ok and recv_utc_r and recv_utc_b:
            derived = compute_derived_for_summary(summary_p, recv_utc_r, recv_utc_b, boot_p)
        else:
            derived = {k: None for k in ["goodput_kBps", "flash_kBps", "t_reboot_ms",
                                          "t_boot_app_ms", "t_downtime_ms", "t_unaccounted_ms",
                                          "heap_used", "heap_used_pct", "stack_used_pct",
                                          "mean_bytes_per_write", "wear_ratio"]}
        valid = 1 if (ok
                      and summary_p.get("rssi", -999) >= args.rssi_min
                      and derived.get("t_unaccounted_ms") is not None
                      and derived.get("t_unaccounted_ms", -1) >= 0) else 0
        conn.execute("""
            INSERT INTO runs (
                exp_id, run_num, direction, fw_from, fw_to,
                ok, is_delta, err,
                t_total_ms, t_connect_ms, t_apply_wall_ms,
                t_http, t_decrypt, t_from_read, t_flash, t_seek,
                t_finalize_ms, b_patch, b_flash, b_from, calls_r, calls_w,
                rssi, heap_min_during,
                t_reboot_ms, t_boot_app_ms, t_downtime_ms,
                goodput_kBps, flash_kBps, t_unaccounted_ms,
                recv_utc_summary, recv_utc_reboot, recv_utc_boot,
                valid, notes
            ) VALUES (
                ?,?,?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?
            )""",
            (args.experiment, run_num, direction, fw_from, fw_to,
             1 if ok else 0,
             summary_p.get("is_delta"), summary_p.get("err"),
             summary_p.get("t_total_ms"), summary_p.get("t_connect_ms"),
             summary_p.get("t_apply_wall_ms"),
             summary_p.get("t_http"), summary_p.get("t_decrypt"),
             summary_p.get("t_from_read"), summary_p.get("t_flash"),
             summary_p.get("t_seek"),
             summary_p.get("t_finalize_ms"),
             summary_p.get("b_patch"), summary_p.get("b_flash"), summary_p.get("b_from"),
             summary_p.get("calls_r"), summary_p.get("calls_w"),
             summary_p.get("rssi"), summary_p.get("heap_min_during"),
             derived.get("t_reboot_ms"), derived.get("t_boot_app_ms"),
             derived.get("t_downtime_ms"),
             derived.get("goodput_kBps"), derived.get("flash_kBps"),
             derived.get("t_unaccounted_ms"),
             recv_utc_s, recv_utc_r, recv_utc_b,
             valid, notes)
        )
        conn.commit()
        if not ok:
            insert_run_failure(conn, args.experiment, notes)
        status = "VALID" if valid else "EXCLUDED"
        print(f"\n  [run {run_num}] {direction}: ok={ok} valid={valid} ({status})"
              f"  t_total={summary_p.get('t_total_ms')} ms"
              f"  rssi={summary_p.get('rssi')} dBm")

    print(f"Campaign: {args.experiment}")
    print(f"  version-a = {args.version_a}")
    print(f"  version-b = {args.version_b}")
    print(f"  target    = {target_runs} completed runs per direction")
    print(f"  rssi-min  = {args.rssi_min} dBm")
    print(f"  settle    = {args.settle} s")
    print(f"  rawlog    = {rawlog}")
    print(f"  Ctrl-C to abort and restore manifest.\n")

    try:
        while True:
            # Circuit breaker
            if consecutive_failures >= circuit_breaker_limit:
                print(f"\nERROR: {circuit_breaker_limit} consecutive failures — aborting campaign.")
                break
            if runs_a2b >= target_runs and runs_b2a >= target_runs:
                print(f"\nCampaign complete: {runs_a2b} A→B runs, {runs_b2a} B→A runs.")
                break

            raw = _read_line()
            now = time.monotonic()

            # §0.2 silence detection
            if raw:
                last_line_time = now

            silence_limit   = SILENCE_OTA_S if state in (STATE_WAITING_OTA, STATE_WAITING_BOOT) \
                              else SILENCE_IDLE_S
            silence_elapsed = now - last_line_time
            if silence_elapsed > silence_limit:
                print(f"\n  [campaign] SILENCE {silence_elapsed:.0f}s in {state} — "
                      f"attempting serial recovery")
                try:
                    ser.close()
                    time.sleep(2)
                    ser = serial.Serial(args.port, args.baud, timeout=1,
                                        dsrdtr=False, rtscts=False)
                    last_line_time = now
                    print("  [campaign] Serial re-opened")
                except Exception as e:
                    print(f"  [campaign] Serial recovery failed: {e}")

                # If mid-OTA, record as timed-out failure
                if state in (STATE_WAITING_OTA, STATE_WAITING_BOOT):
                    run_num += 1
                    _record_run(False, fw_current or "?",
                                args.version_b if fw_current == args.version_a else args.version_a,
                                direction or "?", summary_payload,
                                recv_utc_summary, recv_utc_reboot, None, {}, "silence_timeout")
                    consecutive_failures += 1
                    state = STATE_IDLE
                    state_entered_at = now

                last_line_time = now
                continue

            if raw:
                log_f.write(raw + "\n")
                log_f.flush()
                recv_utc = time.time()

                if METRIC_PREFIX in raw:
                    idx  = raw.find(METRIC_PREFIX)
                    line = raw[idx:]
                    try:
                        parsed = parse_metric_line(line)
                    except Exception as exc:
                        insert_error(conn, args.experiment, recv_utc, raw)
                        parsed = None

                    if parsed:
                        ev  = parsed.get("ev", "")
                        seq = parsed.get("seq", 0)
                        fw  = parsed.get("fw", "")
                        extra = {k: v for k, v in parsed.items()
                                 if k not in ("v", "seq", "t_us", "fw", "dev", "ev")}
                        insert_event(conn, args.experiment, recv_utc, parsed)

                        if ev == "boot":
                            if last_seq is not None:
                                print(f"\n  [NOTICE] Device rebooted (seq {last_seq} → {seq})")
                            last_seq = seq
                            boot_payload_cur = extra
                        else:
                            if last_seq is not None and seq != last_seq + 1:
                                print(f"\n  [NOTICE] seq gap {last_seq+1}…{seq}")
                            last_seq = seq

                        print(f"  [{ev:14s}] seq={seq:>5} fw={fw} {extra}", end="\r")

                        # ---- State machine ----
                        if state == STATE_IDLE:
                            if fw and fw in (args.version_a, args.version_b):
                                fw_current = fw
                                state = STATE_NEED_FLIP
                                state_entered_at = now

                        elif state == STATE_NEED_FLIP:
                            target = (args.version_b if fw_current == args.version_a
                                      else args.version_a)
                            if fw_current == args.version_a and runs_a2b >= target_runs:
                                state = STATE_IDLE
                            elif fw_current == args.version_b and runs_b2a >= target_runs:
                                state = STATE_IDLE
                            else:
                                direction = (f"{args.version_a}→{args.version_b}"
                                             if fw_current == args.version_a
                                             else f"{args.version_b}→{args.version_a}")
                                _flip_manifest(target, flip_script)
                                summary_payload  = {}
                                recv_utc_summary = None
                                recv_utc_reboot  = None
                                boot_payload_cur = {}
                                state = STATE_WAITING_OTA
                                state_entered_at = now
                                print(f"\n  [campaign] Waiting for OTA {direction} "
                                      f"(timeout {OTA_TIMEOUT}s)")

                        elif state == STATE_WAITING_OTA:
                            if ev == "ota_summary":
                                summary_payload  = extra
                                recv_utc_summary = recv_utc
                                run_num += 1
                                if extra.get("ok") == 1:
                                    state = STATE_WAITING_BOOT
                                    state_entered_at = now
                                    print(f"\n  [campaign] ota_summary ok=1, waiting for boot")
                                else:
                                    _record_run(False, fw_current,
                                                args.version_b if fw_current == args.version_a
                                                else args.version_a,
                                                direction, summary_payload,
                                                recv_utc_summary, None, None, {}, "ota_failed")
                                    consecutive_failures += 1
                                    state = STATE_IDLE
                                    state_entered_at = now
                            elif ev == "ota_reboot":
                                recv_utc_reboot = recv_utc

                        elif state == STATE_WAITING_BOOT:
                            if ev == "ota_reboot":
                                recv_utc_reboot = recv_utc
                            elif ev == "boot":
                                fw_new     = fw
                                boot_p     = extra
                                recv_utc_b = recv_utc
                                ok_run     = (fw_new in (args.version_a, args.version_b)
                                              and fw_new != fw_current)
                                fw_target  = (args.version_b if fw_current == args.version_a
                                              else args.version_a)
                                _record_run(ok_run, fw_current, fw_target,
                                            direction, summary_payload,
                                            recv_utc_summary, recv_utc_reboot, recv_utc_b,
                                            boot_p)
                                if ok_run:
                                    consecutive_failures = 0
                                    if fw_current == args.version_a:
                                        runs_a2b += 1
                                    else:
                                        runs_b2a += 1
                                else:
                                    consecutive_failures += 1

                                fw_current = fw_new
                                boot_payload_cur = extra
                                print(f"\n  [campaign] settle {args.settle} s ...")
                                time.sleep(args.settle)
                                state = STATE_NEED_FLIP
                                state_entered_at = now
                                last_line_time   = time.monotonic()

            # Per-run watchdog (OTA_TIMEOUT)
            if state in (STATE_WAITING_OTA, STATE_WAITING_BOOT):
                elapsed = now - state_entered_at
                if elapsed > OTA_TIMEOUT:
                    print(f"\n  [campaign] TIMEOUT in state {state} after {elapsed:.0f}s")
                    run_num += 1
                    _record_run(False, fw_current or "?",
                                args.version_b if (fw_current == args.version_a) else args.version_a,
                                direction or "?", summary_payload,
                                recv_utc_summary, recv_utc_reboot, None, {}, "watchdog_timeout")
                    consecutive_failures += 1
                    state = STATE_IDLE
                    state_entered_at = now

    except KeyboardInterrupt:
        print("\nCampaign interrupted by user.")
    finally:
        log_f.close()
        try:
            ser.close()
        except Exception:
            pass
        _restore_manifest(flip_script)
        conn.close()

    # Summary
    _db = sqlite3.connect(args.db)
    all_runs   = _db.execute("SELECT COUNT(*) FROM runs WHERE exp_id=?",        (args.experiment,)).fetchone()[0]
    valid_runs = _db.execute("SELECT COUNT(*) FROM runs WHERE exp_id=? AND valid=1", (args.experiment,)).fetchone()[0]
    _db.close()
    print(f"\nRuns recorded: {all_runs}  valid: {valid_runs}  (check DB for excluded breakdown)")
    print(f"DB: {args.db}  Experiment: {args.experiment}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # capture
    p = sub.add_parser("capture", help="Capture serial metrics into SQLite")
    p.add_argument("--port",       default="/dev/ttyUSB0")
    p.add_argument("--baud",       type=int, default=115200)
    p.add_argument("--db",         required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--scenario",   default=None)
    p.add_argument("--note",       default=None)
    p.add_argument("--duration",   type=int, default=None,
                   help="Stop after N seconds (default: run until Ctrl-C)")

    # export
    p = sub.add_parser("export", help="Export experiment events to CSV")
    p.add_argument("--db",         required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--out",        required=True)

    # plot
    p = sub.add_parser("plot", help="Generate PNG plots for an experiment")
    p.add_argument("--db",         required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--out",        default="plots/")

    # plot3 — Stage 3 cross-experiment plots
    p = sub.add_parser("plot3", help="Stage 3 cross-experiment resource-footprint plots")
    p.add_argument("--db",          required=True)
    p.add_argument("--experiments", required=True,
                   help="Comma-separated experiment IDs (e.g. MN_DELTA,MN_FULL,NU_DELTA)")
    p.add_argument("--size-csv",    default=None, dest="size_csv",
                   help="Path to size_report.csv for component-size plot")
    p.add_argument("--out",         default="plots/")

    # tail
    p = sub.add_parser("tail", help="Pretty-print live serial output (no DB)")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)

    # campaign
    p = sub.add_parser("campaign", help="Automated N-run OTA ping-pong campaign")
    p.add_argument("--port",         default="/dev/ttyUSB0")
    p.add_argument("--baud",         type=int, default=115200)
    p.add_argument("--db",           required=True)
    p.add_argument("--experiment",   required=True)
    p.add_argument("--version-a",    required=True, dest="version_a",
                   help="Firmware version string A (e.g. s3-base)")
    p.add_argument("--version-b",    required=True, dest="version_b",
                   help="Firmware version string B (e.g. s3-mn1)")
    p.add_argument("--runs",         type=int, default=10,
                   help="Target completed runs per direction (default 10)")
    p.add_argument("--settle",       type=int, default=20,
                   help="Seconds to wait between flips (default 20)")
    p.add_argument("--rssi-min",     type=int, default=-75, dest="rssi_min",
                   help="Minimum RSSI dBm for a valid run (default -75)")
    p.add_argument("--max-failures", type=int, default=3, dest="max_failures",
                   help="Circuit-breaker: abort after N consecutive failures (default 3)")

    args = parser.parse_args()
    {
        "capture":  cmd_capture,
        "export":   cmd_export,
        "plot":     cmd_plot,
        "plot3":    cmd_plot3,
        "tail":     cmd_tail,
        "campaign": cmd_campaign,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
