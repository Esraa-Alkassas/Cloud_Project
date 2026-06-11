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
  harness.py tail    --port /dev/ttyUSB0 [--baud 115200]
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

METRIC_PREFIX = "##M## "
SCHEMA_VERSION = 1


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
    conn.commit()
    return conn


def insert_experiment(conn, exp_id, scenario, note):
    conn.execute(
        "INSERT OR IGNORE INTO experiments (id, started_utc, scenario, note) VALUES (?,?,?,?)",
        (exp_id, time.time(), scenario, note),
    )
    conn.commit()


def insert_event(conn, exp_id, recv_utc, parsed):
    # Extract known fields; everything else goes into payload_json
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


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

def parse_metric_line(line: str):
    """Return parsed dict or raise ValueError."""
    if not line.startswith(METRIC_PREFIX):
        raise ValueError("not a metric line")
    payload = line[len(METRIC_PREFIX):].strip()
    obj = json.loads(payload)
    if obj.get("v") != SCHEMA_VERSION:
        raise ValueError(f"unknown schema version {obj.get('v')}")
    return obj


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
        ser = serial.Serial(args.port, args.baud, timeout=1)
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

    try:
        with open(rawlog, "w", encoding="utf-8", errors="replace") as logf:
            while True:
                if deadline and time.monotonic() >= deadline:
                    print(f"\nDuration reached. {event_count} metric events stored.")
                    break
                try:
                    raw = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    continue
                if not raw:
                    continue
                logf.write(raw + "\n")
                logf.flush()
                recv_utc = time.time()

                if METRIC_PREFIX not in raw:
                    continue

                # Extract just the ##M## portion (handles interleaved ESP_LOG noise)
                idx = raw.find(METRIC_PREFIX)
                line = raw[idx:]

                try:
                    parsed = parse_metric_line(line)
                except Exception as exc:
                    print(f"  [WARN] parse error: {exc} | raw: {raw[:120]}")
                    insert_error(conn, args.experiment, recv_utc, raw)
                    continue

                # Sequence gap / reboot detection
                seq = parsed.get("seq", 0)
                ev  = parsed.get("ev", "")
                if ev == "boot":
                    if last_seq is not None:
                        print(f"  [NOTICE] Device rebooted (seq reset {last_seq} → {seq})")
                    last_seq = seq
                elif last_seq is not None and seq != last_seq + 1:
                    print(f"  [NOTICE] seq gap: expected {last_seq + 1}, got {seq} (dropped {seq - last_seq - 1} lines)")
                    last_seq = seq
                else:
                    last_seq = seq

                insert_event(conn, args.experiment, recv_utc, parsed)
                event_count += 1
                print(f"  [{ev:12s}] seq={seq} t_us={parsed.get('t_us')} fw={parsed.get('fw')}")

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

    # Collect all payload keys for flattened columns
    all_payload_keys = set()
    for row in rows:
        try:
            extra = json.loads(row["payload_json"] or "{}")
            all_payload_keys.update(extra.keys())
        except Exception:
            pass

    base_cols = ["id", "exp_id", "recv_utc", "dev", "fw", "seq", "t_us", "ev"]
    extra_cols = sorted(all_payload_keys)
    fieldnames = base_cols + extra_cols

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            record = dict(row)
            try:
                extra = json.loads(record.pop("payload_json") or "{}")
            except Exception:
                extra = {}
            record.update(extra)
            writer.writerow({k: record.get(k, "") for k in fieldnames})

    print(f"Exported {len(rows)} events → {args.out}")
    conn.close()


def cmd_plot(args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib not installed — run: pip install matplotlib")

    os.makedirs(args.out, exist_ok=True)
    conn = open_db(args.db)

    # --- Heap plot ---
    rows = conn.execute(
        "SELECT t_us, payload_json FROM events WHERE exp_id=? AND ev='heartbeat' ORDER BY id",
        (args.experiment,),
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
        ax.set_title(f"Heap — experiment {args.experiment}")
        ax.legend()
        out_path = os.path.join(args.out, f"{args.experiment}_heap.png")
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        print(f"Heap plot → {out_path}")
    else:
        print("No heartbeat events found — skipping heap plot")

    # --- Event count summary ---
    ev_rows = conn.execute(
        "SELECT ev, COUNT(*) AS cnt FROM events WHERE exp_id=? GROUP BY ev ORDER BY cnt DESC",
        (args.experiment,),
    ).fetchall()

    if ev_rows:
        evs   = [r["ev"]  for r in ev_rows]
        cnts  = [r["cnt"] for r in ev_rows]
        fig, ax = plt.subplots(figsize=(max(6, len(evs) * 1.2), 4))
        ax.bar(evs, cnts)
        ax.set_xlabel("Event type")
        ax.set_ylabel("Count")
        ax.set_title(f"Event counts — experiment {args.experiment}")
        out_path = os.path.join(args.out, f"{args.experiment}_event_counts.png")
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        print(f"Event count plot → {out_path}")

    conn.close()


def cmd_tail(args):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed — run: pip install pyserial")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
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
                    print(f"  \033[32m[{ev:12s}]\033[0m seq={seq:>5}  t={t/1e6:9.2f}s  {extra}")
                except Exception:
                    print(f"  \033[33m[METRIC?]\033[0m {raw}")
            else:
                print(f"  \033[90m{raw}\033[0m")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


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

    # tail
    p = sub.add_parser("tail", help="Pretty-print live serial output (no DB)")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)

    args = parser.parse_args()
    {"capture": cmd_capture, "export": cmd_export,
     "plot": cmd_plot, "tail": cmd_tail}[args.cmd](args)


if __name__ == "__main__":
    main()
