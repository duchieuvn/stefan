# Duty-Cycle Analysis for `lora_logger_sweep.py`

Documents `analyze_sweep_duty_cycle.py`: what it computes, exactly how, and
why each step is done the way it is.

## Purpose

`lora_logger_sweep.py` automatically applies a list of LoRa configurations
(`SWEEP_CONFIGS`) to all 8 nodes and, for each one, logs every link
measurement to both SQLite and a per-configuration CSV under `runs_csv/`.
`analyze_sweep_duty_cycle.py` reads those CSVs and, for each configuration,
estimates the **average duty cycle across all transmitters**, using only the
most recent `WINDOW_HOURS` (default 2h) of data in each file.

## Inputs

For each `runs_csv/results_*.csv` file (one per swept configuration):

| Column | Used for |
|---|---|
| `freq_mhz`, `tx_power`, `sf`, `bw_khz`, `cr_denom` | Identify the configuration; `sf`/`bw_khz`/`cr_denom` feed the Time-on-Air formula |
| `payload_len` | Time-on-Air formula input |
| `round_id`, `sender_id` | Group rows into "one transmission event" per node per round |
| `pc_ts`, `e2e_ms` | Reconstruct *when* each transmission actually happened (see below) |

Two things needed for the calculation are **not** in the CSV, because the
`slave/src` firmware doesn't configure or report them over Modbus — they're
fixed RadioLib SX1262 defaults, assumed as constants in the script
(`ASSUMED_PREAMBLE_LEN=8`, `ASSUMED_CRC_ENABLED=True`,
`ASSUMED_EXPLICIT_HEADER=True`):

## Step 1 — Pick the analysis window

```
window_end   = max(pc_ts) in this file
window_start = window_end - WINDOW_HOURS
```

The window is anchored to the **latest timestamp in the file**, not to the
current wall-clock time — these are historical sweep runs, and the script is
meant to work identically whether it's run right after a sweep or weeks
later. Rows outside `[window_start, window_end]` are dropped before anything
else.

## Step 2 — Time-on-Air (once per configuration)

`sf`, `bw_khz`, `cr_denom`, `payload_len` are constant within one CSV (one
file = one configuration), so ToA is computed once via
[`electronic_black_box/stat.py`](../../electronic_black_box/stat.py)'s
`calculate_time_on_air_ms()` — the same Semtech LoRa airtime formula used
everywhere else in this project. The script loads that file directly by path
(`importlib.util.spec_from_file_location`) instead of `import stat`, because
`stat` is also the name of a Python standard-library module and a plain
import would resolve to the wrong one.

## Step 3 — Reconstruct each transmission's real trigger time

The CSV's `pc_ts` is only second-precision (`datetime.isoformat(timespec="seconds")`
in `lora_logger_sweep.py`), which is too coarse to measure intervals of a
few seconds accurately. Every row also has `e2e_ms` — the time from
triggering the sender to confirming receipt — so:

```
trigger_ts ≈ pc_ts - e2e_ms
```

recovers a sub-second-precision estimate of when the sender's transmission
actually happened. All rows sharing the same `(round_id, sender_id)` come
from the *same* physical transmission (one sender, multiple receivers), so
their reconstructed `trigger_ts` values are averaged into one event
timestamp per `(round_id, sender_id)`.

**Why not just use `pc_ts` directly, or the round's `duration_s`?** Unlike
Both scripts now start rounds every `MEASURE_INTERVAL_S` (120 s by default in
`lora_logger_sweep.py`), but that's only a target: a round that runs longer
than the interval (e.g. several RX timeouts) delays the next one, so the
configured value alone can underestimate the real spacing (see the
`max(duration_s, measure_interval_s)` approach discussed for `lora_logger_03.py`).
Reconstructing the actual per-transmission timestamps and diffing them
directly is the more general and more accurate method, and works
identically for both scripts.

## Step 4 — Per-transmitter interval

For each `sender_id`, its event timestamps (one per round it participated
in) are sorted, and consecutive differences are taken:

```
intervals[sender_id] = [t2-t1, t3-t2, t4-t3, ...]   (seconds)
mean_interval_s = mean(intervals[sender_id])
```

A sender needs at least `MIN_EVENTS_PER_SENDER` (2) events in the window to
produce an interval; if it has fewer (e.g. it dropped out, or the window is
too short), it's excluded from that configuration's result and a message is
logged.

## Step 5 — Duty cycle, per transmitter then averaged

```
duty_cycle_pct[sender_id] = (toa_ms / (mean_interval_s * 1000)) * 100

avg_duty_cycle_pct = mean(duty_cycle_pct[sender_id] for all senders)
```

This is the "average estimated duty cycle of all transmitters" for that
configuration. `min`/`max` across senders are also reported, since a single
average can hide one node repeating much faster or slower than the rest
(e.g. because of RX timeouts elsewhere in a round dragging out its own
cadence).

## Output

`runs_csv/duty_cycle_summary.csv` — one row per configuration:

| Column | Meaning |
|---|---|
| `csv_file` | Source file this row summarizes |
| `freq_mhz`, `tx_power`, `sf`, `bw_khz`, `cr_denom`, `payload_bytes` | The configuration |
| `toa_ms` | Calculated Time-on-Air for this configuration |
| `window_start`, `window_end` | The actual `WINDOW_HOURS` window used |
| `num_transmitters` | How many senders had enough data to include |
| `avg_duty_cycle_pct`, `min_duty_cycle_pct`, `max_duty_cycle_pct` | Across those transmitters |
| `samples_used` | Row count in the window (before dedup to events) |

The script also prints one summary line per configuration to the console.

## Known limitations

- **Preamble/CRC/header are assumed, not measured** — if the firmware is
  ever changed to configure these, this script's constants must be updated
  to match, since nothing in the data would flag the mismatch.
- **The window is per-file, not wall-clock "now"** — if you want "the last 2
  real-time hours regardless of when the sweep ran," change `window_end` in
  `analyze_config_csv()` to `datetime.now()` instead of `max(all_ts)`.
- **`e2e_ms`-based reconstruction has its own jitter** (Modbus polling
  granularity `RX_POLL_S=0.05s`, RS‑485 round-trip time) — fine for
  averaging over many rounds, but individual reconstructed timestamps
  shouldn't be trusted to better than ~100 ms.
