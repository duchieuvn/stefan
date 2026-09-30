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
OUT_LINKS_PATH = CSV_DIR / "link_stats.csv"   # per (config, sender, receiver)

WINDOW_HOURS: Optional[float] = None  # None = whole file; e.g. 2.0 = only the last 2h per configuration
MIN_EVENTS_PER_SENDER = 2   # < 2 trigger timestamps -> no interval computable

# Firmware assumptions that this sketch (slave/src) does not make configurable
# via Modbus and which are therefore not present in the CSVs (see README.md).
ASSUMED_PREAMBLE_LEN = 8
ASSUMED_CRC_ENABLED = True
ASSUMED_EXPLICIT_HEADER = True

STAT_PY_PATH = Path("stat.py")


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


def to_float(value: Any) -> Optional[float]:
    """CSV cells are strings; missing RX values are written as ''."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def percentile(sorted_values: List[float], pct: float) -> float:
    """Linear-interpolated percentile of an already sorted list."""
    k = (len(sorted_values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def describe(values: List[float], prefix: str, ndigits: int = 2) -> Dict[str, Optional[float]]:
    """Basic descriptive statistics as '<prefix>_<stat>' columns (None if no data)."""
    keys = ["mean", "median", "std", "min", "max", "p95"]
    if not values:
        return {f"{prefix}_{k}": None for k in keys}
    v = sorted(values)
    stats = {
        "mean": statistics.mean(v),
        "median": statistics.median(v),
        "std": statistics.stdev(v) if len(v) > 1 else 0.0,
        "min": v[0],
        "max": v[-1],
        "p95": percentile(v, 95),
    }
    return {f"{prefix}_{k}": round(stats[k], ndigits) for k in keys}


def mean_or_none(values: List[float], ndigits: int = 2) -> Optional[float]:
    return round(statistics.mean(values), ndigits) if values else None


def compute_link_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Packet delivery ratio and mean signal quality per (sender, receiver) link."""
    by_link: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        try:
            key = (int(row["sender_id"]), int(row["receiver_id"]))
        except (KeyError, ValueError, TypeError):
            continue
        by_link.setdefault(key, []).append(row)

    links = []
    for (sender_id, receiver_id), link_rows in sorted(by_link.items()):
        ok_rows = [r for r in link_rows if to_float(r.get("ok_match")) == 1]
        rssi = [x for x in (to_float(r.get("rssi")) for r in ok_rows) if x is not None]
        snr = [x for x in (to_float(r.get("snr")) for r in ok_rows) if x is not None]
        links.append({
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "attempts": len(link_rows),
            "ok": len(ok_rows),
            "pdr_pct": round(100.0 * len(ok_rows) / len(link_rows), 2),
            "rssi_mean": mean_or_none(rssi),
            "snr_mean": mean_or_none(snr),
        })
    return links


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

    # Time window: the whole file by default, or the last WINDOW_HOURS relative
    # to the most recent data point in this file (not relative to "now" - the
    # sweeps are historical runs, see README.md).
    all_ts = [parse_pc_ts(r["pc_ts"]) for r in rows if r.get("pc_ts")]
    if not all_ts:
        log(f"[skip] {path.name}: no valid pc_ts")
        return None

    window_end = max(all_ts)
    if WINDOW_HOURS is None:
        window_start = min(all_ts)
    else:
        window_start = window_end - timedelta(hours=WINDOW_HOURS)
    window_rows = [r for r, ts in zip(rows, all_ts) if window_start <= ts <= window_end]

    if not window_rows:
        log(f"[skip] {path.name}: no rows in the {window_label()} window")
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

    # ---- Basic statistics beyond duty cycle ----
    # Signal quality only counts correctly received packets (ok_match == 1);
    # failed links have no or garbage RSSI/SNR.
    ok_rows = [r for r in window_rows if to_float(r.get("ok_match")) == 1]
    rssi = [x for x in (to_float(r.get("rssi")) for r in ok_rows) if x is not None]
    snr = [x for x in (to_float(r.get("snr")) for r in ok_rows) if x is not None]
    e2e = [x for x in (to_float(r.get("e2e_ms")) for r in window_rows) if x is not None]
    all_intervals = [d for diffs in sender_intervals.values() for d in diffs]
    rounds = {r.get("round_id") for r in window_rows}

    links = compute_link_stats(window_rows)
    worst_link = min(links, key=lambda l: l["pdr_pct"]) if links else None

    def env_mean(col: str) -> Optional[float]:
        return mean_or_none([x for x in (to_float(r.get(col)) for r in window_rows) if x is not None])

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
        "duration_h": round((window_end - window_start).total_seconds() / 3600.0, 2),
        "rounds": len(rounds),
        "total_links": len(window_rows),
        "ok_links": len(ok_rows),
        "pdr_pct": round(100.0 * len(ok_rows) / len(window_rows), 2),
        "worst_link": f"{worst_link['sender_id']}->{worst_link['receiver_id']}" if worst_link else None,
        "worst_link_pdr_pct": worst_link["pdr_pct"] if worst_link else None,
        **describe(rssi, "rssi"),
        **describe(snr, "snr"),
        **describe(e2e, "e2e_ms", ndigits=1),
        **describe(all_intervals, "interval_s", ndigits=2),
        "bme_temp_mean": env_mean("bme_temp"),
        "bme_hum_mean": env_mean("bme_hum"),
        "bme_press_mean": env_mean("bme_press"),
        "_links": links,
    }


LINK_FIELDS = [
    "csv_file", "freq_mhz", "tx_power",
    "sender_id", "receiver_id", "attempts", "ok", "pdr_pct", "rssi_mean", "snr_mean",
]


def fmt(value: Optional[float], spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def print_table(headers: List[str], rows: List[List[Any]], indent: str = "    ") -> None:
    """Prints a plain left/right aligned text table (numbers right-aligned)."""
    cells = [[("n/a" if c is None else str(c)) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    numeric = [all(isinstance(row[i], (int, float)) or row[i] is None for row in rows)
               for i in range(len(headers))]

    def line(values: List[str]) -> str:
        return "  ".join(v.rjust(w) if num else v.ljust(w)
                         for v, w, num in zip(values, widths, numeric)).rstrip()

    log(indent + line(headers))
    log(indent + "  ".join("-" * w for w in widths))
    for r in cells:
        log(indent + line(r))


STAT_KEYS = ["mean", "median", "std", "min", "max", "p95"]
STAT_ROWS = [("RSSI [dBm]", "rssi"), ("SNR [dB]", "snr"),
             ("e2e latency [ms]", "e2e_ms"), ("TX interval [s]", "interval_s")]


def print_config_report(r: Dict[str, Any]) -> None:
    log("=" * 78)
    log(f"{r['csv_file']}")
    log(f"  config     : freq={r['freq_mhz']:.1f}MHz pwr={r['tx_power']}dBm SF{r['sf']} "
        f"bw={r['bw_khz']:.0f}kHz CR4/{r['cr_denom']} payload={r['payload_bytes']}B "
        f"ToA={r['toa_ms']:.1f}ms")
    log(f"  window     : {r['window_start']} .. {r['window_end']} "
        f"({r['duration_h']:.2f}h, {r['rounds']} rounds, {r['samples_used']} samples)")
    log(f"  duty cycle : avg={r['avg_duty_cycle_pct']:.4f}% min={r['min_duty_cycle_pct']:.4f}% "
        f"max={r['max_duty_cycle_pct']:.4f}% over {r['num_transmitters']} transmitters")
    log(f"  delivery   : PDR={r['pdr_pct']:.2f}% ({r['ok_links']}/{r['total_links']} links ok), "
        f"worst link {r['worst_link']} = {fmt(r['worst_link_pdr_pct'], '.2f')}%")
    log(f"  environment: T={fmt(r['bme_temp_mean'], '.2f')}C RH={fmt(r['bme_hum_mean'], '.2f')}% "
        f"p={fmt(r['bme_press_mean'], '.2f')}hPa")
    log("")
    print_table(["metric", *STAT_KEYS],
                [[label, *(r[f"{prefix}_{k}"] for k in STAT_KEYS)] for label, prefix in STAT_ROWS])
    log("")
    print_table(["sender", "receiver", "attempts", "ok", "PDR %", "RSSI mean", "SNR mean"],
                [[l["sender_id"], l["receiver_id"], l["attempts"], l["ok"],
                  l["pdr_pct"], l["rssi_mean"], l["snr_mean"]] for l in r["_links"]])
    log("")


def print_comparison(summaries: List[Dict[str, Any]]) -> None:
    log("=" * 78)
    log("Comparison across configurations")
    print_table(
        ["file", "MHz", "dBm", "SF", "rounds", "duty %", "PDR %", "worst link",
         "RSSI med", "SNR med", "e2e p95 ms"],
        [[s["csv_file"], s["freq_mhz"], s["tx_power"], s["sf"], s["rounds"],
          s["avg_duty_cycle_pct"], s["pdr_pct"],
          f"{s['worst_link']} ({fmt(s['worst_link_pdr_pct'], '.1f')}%)",
          s["rssi_median"], s["snr_median"], s["e2e_ms_p95"]] for s in summaries],
        indent="  ",
    )


def window_label() -> str:
    return "full file" if WINDOW_HOURS is None else f"last {WINDOW_HOURS:g}h per file"


def main():
    config_csvs = sorted(
        p for p in CSV_DIR.glob("results_*.csv") if p.is_file()
    )
    if not config_csvs:
        log(f"No results_*.csv found under {CSV_DIR.resolve()}. "
            f"Run lora_logger_sweep.py first.")
        return

    log(f"Analyzing {len(config_csvs)} sweep configuration(s) from {CSV_DIR.resolve()} "
        f"(window: {window_label()})\n")

    summaries: List[Dict[str, Any]] = []
    for path in config_csvs:
        result = analyze_config_csv(path)
        if result is None:
            continue
        summaries.append(result)
        print_config_report(result)

    if not summaries:
        log("\nNo configuration had enough data in the time window - no summary written.")
        return

    print_comparison(summaries)

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    summary_fields = [k for k in summaries[0] if not k.startswith("_")]
    with OUT_SUMMARY_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields, extrasaction="ignore")
        writer.writeheader()
        for s in summaries:
            writer.writerow(s)

    with OUT_LINKS_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LINK_FIELDS)
        writer.writeheader()
        for s in summaries:
            for link in s["_links"]:
                writer.writerow({"csv_file": s["csv_file"], "freq_mhz": s["freq_mhz"],
                                 "tx_power": s["tx_power"], **link})

    log(f"\nSummary written: {OUT_SUMMARY_PATH.resolve()}")
    log(f"Link stats written: {OUT_LINKS_PATH.resolve()}")


if __name__ == "__main__":
    main()
