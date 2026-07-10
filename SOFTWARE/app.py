#!/usr/bin/env python3
"""
Flask web viewer for the dynamic temporal RTI reconstruction.

Numeric/data work lives in `rti_temporal_core.py`; Matplotlib drawing lives in
`rti_temporal_visualization.py`. This file just wires them up to HTTP routes.
"""

from __future__ import annotations

# Must happen before NumPy/OpenBLAS import through the core module.
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import base64
import io
import sys
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask, jsonify, render_template, request

from config import DEFAULT_CONFIG, RTIConfig
from rti.rti_temporal_core import (
    RTISequenceResult,
    build_model_context,
    compute_temporal_sequence,
    ensure_indexes,
    get_connection,
    get_dates_since,
    get_latest_pc_ts,
    interval_label,
    parse_pc_ts,
)
from rti.rti_temporal_visualization import create_temporal_figure, draw_temporal_sequence_view


CONFIG: RTIConfig = DEFAULT_CONFIG

# Bounds every DB touch (the initial date listing and every RSSI bulk query)
# to at most this many days back from the latest measurement, regardless of
# how much history has accumulated in the database.
FETCH_BUFFER_DAYS = 190  # ~6 months + slack

WINDOW_DAYS = {"3m": 92, "6m": 183}
DEFAULT_WINDOW = "3m"

if not CONFIG.db_path.exists():
    sys.exit(f"Fehler: Datenbank nicht gefunden: {CONFIG.db_path}")

_con = get_connection(CONFIG)
try:
    ensure_indexes(_con)
    _latest_ts = get_latest_pc_ts(_con)
    if _latest_ts is None:
        sys.exit("Fehler: Keine Messdaten in measurement gefunden.")
    _since_dt = parse_pc_ts(_latest_ts) - timedelta(days=FETCH_BUFFER_DAYS)
    RECENT_DATES: list[str] = get_dates_since(_con, _since_dt.isoformat(timespec="seconds"))
finally:
    _con.close()

if not RECENT_DATES:
    sys.exit(f"Fehler: Keine nutzbaren Tage im {FETCH_BUFFER_DAYS}-Tage-Fenster gefunden.")

CONTEXT = build_model_context(CONFIG)
INTERVAL_CACHE: dict[tuple[str, str], tuple[dict, dict]] = {}
SEQUENCE_CACHE: dict[tuple[str, str], RTISequenceResult] = {}  # (ref_date, window) -> result

app = Flask(__name__)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def windowed_dates(window: str) -> list[str]:
    days = WINDOW_DAYS.get(window, WINDOW_DAYS[DEFAULT_WINDOW])
    latest = datetime.strptime(RECENT_DATES[-1], "%Y-%m-%d")
    cutoff = (latest - timedelta(days=days)).strftime("%Y-%m-%d")
    return [d for d in RECENT_DATES if d >= cutoff]


def resolve_index(dates: list[str], date_str: str | None, idx: int | None, default: int) -> int:
    if date_str and date_str in dates:
        return dates.index(date_str)
    if idx is not None:
        return clamp(idx, 0, len(dates) - 1)
    return clamp(default, 0, len(dates) - 1)


def get_sequence_result(
    window: str,
    ref_idx: int | None,
    ref_date: str | None,
    force: bool,
) -> tuple[list[str], int, RTISequenceResult]:
    dates = windowed_dates(window)
    idx = resolve_index(dates, ref_date, ref_idx, default=0)
    resolved_ref_date = dates[idx]

    key = (resolved_ref_date, window)
    if force:
        SEQUENCE_CACHE.pop(key, None)

    result = SEQUENCE_CACHE.get(key)
    if result is None:
        result = compute_temporal_sequence(
            context=CONTEXT,
            ref_date=resolved_ref_date,
            sequence_dates=dates,
            interval_cache=INTERVAL_CACHE,
            config=CONFIG,
        )
        SEQUENCE_CACHE[key] = result

    return dates, idx, result


def render_plot_png(result: RTISequenceResult, ref_date: str, cur_idx: int) -> bytes:
    figure = create_temporal_figure()
    try:
        draw_temporal_sequence_view(
            figure=figure,
            context=CONTEXT,
            result=result,
            sequence_index=cur_idx,
            ref_date=ref_date,
            config=CONFIG,
        )
        buf = io.BytesIO()
        figure.fig.savefig(buf, format="png", dpi=100)
        return buf.getvalue()
    finally:
        plt.close(figure.fig)


def build_info_text(result: RTISequenceResult, ref_date: str, cur_idx: int) -> str:
    cur_date = result.sequence_dates[cur_idx]
    mean_now = float(result.mean_change[cur_idx])

    return (
        f"Zeitfolge relativ zu Referenz {ref_date} | "
        f"zeige {cur_date} ({cur_idx + 1}/{len(result.sequence_dates)}) | "
        f"{'Referenz selbst: Nullkarte | ' if cur_date == ref_date else ''}"
        f"gültige Links: {result.valid_counts[cur_idx]}/{len(CONTEXT.pairs)} | "
        f"α_time={CONFIG.alpha_time:.3f} | "
        f"mittlere rekonstruierte Δ-Dämpfung={mean_now:.3f} dB | "
        f"Kartenwerte ΔDämpfung [{result.raw_min:.3f}, {result.raw_max:.3f}] dB"
    )


@app.route("/")
def index():
    return render_template("index.html", default_window=DEFAULT_WINDOW)


@app.route("/api/update")
def api_update():
    window = request.args.get("window", DEFAULT_WINDOW)
    if window not in WINDOW_DAYS:
        window = DEFAULT_WINDOW
    force = request.args.get("force", "0") == "1"

    ref_idx_param = request.args.get("ref_idx")
    ref_date_param = request.args.get("ref_date")
    cur_idx_param = request.args.get("cur_idx")
    cur_date_param = request.args.get("cur_date")

    ref_idx_in = int(ref_idx_param) if ref_idx_param is not None else None
    cur_idx_in = int(cur_idx_param) if cur_idx_param is not None else None

    dates, ref_idx, result = get_sequence_result(window, ref_idx_in, ref_date_param, force)
    ref_date = dates[ref_idx]

    cur_idx = resolve_index(
        result.sequence_dates, cur_date_param, cur_idx_in, default=len(result.sequence_dates) - 1
    )
    cur_date = result.sequence_dates[cur_idx]

    png_bytes = render_plot_png(result, ref_date, cur_idx)

    return jsonify(
        {
            "window": window,
            "ref_idx": ref_idx,
            "cur_idx": cur_idx,
            "max_idx": len(dates) - 1,
            "ref_date": ref_date,
            "cur_date": cur_date,
            "first_date": dates[0],
            "last_date": dates[-1],
            "ref_label": interval_label(ref_date, CONFIG.ref_start_hhmm, CONFIG.ref_end_hhmm),
            "cur_label": interval_label(cur_date, CONFIG.cur_start_hhmm, CONFIG.cur_end_hhmm),
            "info": build_info_text(result, ref_date, cur_idx),
            "image": base64.b64encode(png_bytes).decode("ascii"),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=False, port=7081)