from __future__ import annotations

import csv
import importlib.util
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# =========================
# CONFIG (adjust as needed!)
# =========================
CSV_DIR = Path("runs_csv")                              # written by lora_logger_sweep.py
OUT_SUMMARY_PATH = CSV_DIR / "duty_cycle_summary.csv"

WINDOW_HOURS = 2.0          # "last 2 hours" per sweep configuration
MIN_EVENTS_PER_SENDER = 2   # < 2 trigger timestamps -> no interval computable

# Firmware assumptions that this sketch (slave/src) does not make configurable
# via Modbus and which are therefore not present in the CSVs (see README.md).
ASSUMED_PREAMBLE_LEN = 8
ASSUMED_CRC_ENABLED = True
ASSUMED_EXPLICIT_HEADER = True

STAT_PY_PATH = Path(__file__).resolve().parents[2] / "electronic_black_box" / "stat.py"


def _load_stat_module():
    """Loads electronic_black_box/stat.py via its file path instead of importing
    it by name - the module name 'stat' would otherwise collide with Python's
    standard library module of the same name. stat.py itself does `import config`,
    so its directory must briefly be on sys.path, otherwise that import fails."""
    stat_dir = str(STAT_PY_PATH.parent)
    added = stat_dir not in sys.path
    if added:
        sys.path.insert(0, stat_dir)
    try:
        spec = importlib.util.spec_from_file_location("ebb_stat", STAT_PY_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(stat_dir)


stat = _load_stat_module()


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_pc_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def read_config_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def reconstruct_trigger_dt(row: Dict[str, Any]) -> Optional[datetime]:
    """Reconstructs the approximate (sub-second accurate) trigger timestamp
    of a transmission from pc_ts (second resolution) minus e2e_ms:
        trigger_ts ~= pc_ts - e2e_ms
    See README.md, section 'Why e2e_ms instead of pc_ts directly'."""
    try:
        pc_ts = parse_pc_ts(row["pc_ts"])
        e2e_ms = float(row["e2e_ms"])
    except (KeyError, ValueError, TypeError):
        return None
    return pc_ts - timedelta(milliseconds=e2e_ms)


def compute_sender_intervals_s(rows: List[Dict[str, Any]]) -> Dict[int, List[float]]:
    """Groups rows by (round_id, sender_id), takes the mean reconstructed
    trigger timestamp per group (all receivers of a round share the same
    physical trigger), sorts these per sender_id, and returns the
    successive differences in seconds."""
    by_round_sender: Dict[tuple, List[datetime]] = {}
    for row in rows:
        try:
            round_id = int(row["round_id"])
            sender_id = int(row["sender_id"])
        except (KeyError, ValueError, TypeError):
            continue
        dt = reconstruct_trigger_dt(row)
        if dt is None:
            continue
        by_round_sender.setdefault((round_id, sender_id), []).append(dt)

    trigger_events: Dict[int, List[datetime]] = {}
    for (round_id, sender_id), dts in by_round_sender.items():
        avg_epoch = sum(d.timestamp() for d in dts) / len(dts)
        trigger_events.setdefault(sender_id, []).append(datetime.fromtimestamp(avg_epoch))

    intervals: Dict[int, List[float]] = {}
    for sender_id, events in trigger_events.items():
        events.sort()
        if len(events) < MIN_EVENTS_PER_SENDER:
            continue
        diffs = [
            (events[i + 1] - events[i]).total_seconds()
            for i in range(len(events) - 1)
        ]
        intervals[sender_id] = diffs

    return intervals


def analyze_config_csv(path: Path) -> Optional[Dict[str, Any]]:
    rows = read_config_csv(path)
    if not rows:
        log(f"[skip] {path.name}: empty")
        return None

    # Time window: the last WINDOW_HOURS relative to the most recent data
    # point in this file (not relative to "now" - the sweeps are historical
    # runs, see README.md).
    all_ts = [parse_pc_ts(r["pc_ts"]) for r in rows if r.get("pc_ts")]
    if not all_ts:
        log(f"[skip] {path.name}: no valid pc_ts")
        return None

    window_end = max(all_ts)
    window_start = window_end - timedelta(hours=WINDOW_HOURS)
    window_rows = [r for r, ts in zip(rows, all_ts) if window_start <= ts <= window_end]

    if not window_rows:
        log(f"[skip] {path.name}: no rows in the {WINDOW_HOURS}h window")
        return None

    first = window_rows[0]
    freq_mhz = float(first["freq_mhz"])
    tx_power = float(first["tx_power"])
    sf = int(first["sf"])
    bw_khz = float(first["bw_khz"])
    cr_denom = int(first["cr_denom"])
    payload_bytes = int(first["payload_len"])

    toa = stat.calculate_time_on_air_ms(
        sf=sf, bw_khz=bw_khz, cr_denom=cr_denom,
        preamble_len=ASSUMED_PREAMBLE_LEN, payload_bytes=payload_bytes,
        crc_enabled=ASSUMED_CRC_ENABLED, explicit_header=ASSUMED_EXPLICIT_HEADER,
        ldro="auto",
    )
    toa_ms = toa["toa_ms"]

    sender_intervals = compute_sender_intervals_s(window_rows)
    if not sender_intervals:
        log(f"[skip] {path.name}: too few rounds per sender in the window "
            f"(need >= {MIN_EVENTS_PER_SENDER} triggers per sender)")
        return None

    per_sender_duty_pct: Dict[int, float] = {}
    for sender_id, diffs in sender_intervals.items():
        mean_interval_s = statistics.mean(diffs)
        if mean_interval_s <= 0:
            continue
        per_sender_duty_pct[sender_id] = (toa_ms / (mean_interval_s * 1000.0)) * 100.0

    if not per_sender_duty_pct:
        log(f"[skip] {path.name}: no valid sender intervals")
        return None

    duty_values = list(per_sender_duty_pct.values())

    return {
        "csv_file": path.name,
        "freq_mhz": freq_mhz,
        "tx_power": tx_power,
        "sf": sf,
        "bw_khz": bw_khz,
        "cr_denom": cr_denom,
        "payload_bytes": payload_bytes,
        "toa_ms": round(toa_ms, 2),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": window_end.isoformat(timespec="seconds"),
        "num_transmitters": len(per_sender_duty_pct),
        "avg_duty_cycle_pct": round(statistics.mean(duty_values), 4),
        "min_duty_cycle_pct": round(min(duty_values), 4),
        "max_duty_cycle_pct": round(max(duty_values), 4),
        "samples_used": len(window_rows),
    }


SUMMARY_FIELDS = [
    "csv_file", "freq_mhz", "tx_power", "sf", "bw_khz", "cr_denom", "payload_bytes",
    "toa_ms", "window_start", "window_end", "num_transmitters",
    "avg_duty_cycle_pct", "min_duty_cycle_pct", "max_duty_cycle_pct", "samples_used",
]


def main():
    config_csvs = sorted(
        p for p in CSV_DIR.glob("results_*.csv") if p.is_file()
    )
    if not config_csvs:
        log(f"No results_*.csv found under {CSV_DIR.resolve()}. "
            f"Run lora_logger_sweep.py first.")
        return

    log(f"Analyzing {len(config_csvs)} sweep configuration(s) from {CSV_DIR.resolve()} "
        f"(window: last {WINDOW_HOURS:.0f}h per file)\n")

    summaries: List[Dict[str, Any]] = []
    for path in config_csvs:
        result = analyze_config_csv(path)
        if result is None:
            continue
        summaries.append(result)
        log(
            f"{result['csv_file']}: "
            f"freq={result['freq_mhz']:.1f}MHz pwr={result['tx_power']}dBm "
            f"SF{result['sf']} bw={result['bw_khz']:.0f}kHz CR4/{result['cr_denom']} | "
            f"ToA={result['toa_ms']:.1f}ms | "
            f"{result['num_transmitters']} senders | "
            f"avg duty cycle = {result['avg_duty_cycle_pct']:.3f}% "
            f"(min={result['min_duty_cycle_pct']:.3f}%, max={result['max_duty_cycle_pct']:.3f}%)"
        )

    if not summaries:
        log("\nNo configuration had enough data in the time window - no summary written.")
        return

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_SUMMARY_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for s in summaries:
            writer.writerow(s)

    log(f"\nSummary written: {OUT_SUMMARY_PATH.resolve()}")


if __name__ == "__main__":
    main()
