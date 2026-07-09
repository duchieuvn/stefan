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
    get_available_dates,
    get_connection,
    interval_label,
)
from rti.rti_temporal_visualization import create_temporal_figure, draw_temporal_sequence_view


CONFIG: RTIConfig = DEFAULT_CONFIG

if not CONFIG.db_path.exists():
    sys.exit(f"Fehler: Datenbank nicht gefunden: {CONFIG.db_path}")

_con = get_connection(CONFIG)
try:
    ensure_indexes(_con)
    AVAILABLE_DATES: list[str] = get_available_dates(_con, CONFIG)
finally:
    _con.close()

if not AVAILABLE_DATES:
    sys.exit(
        "Fehler: Keine nutzbaren vollständigen Tage in measurement.pc_ts gefunden.\n"
        "Falls du bewusst auch angefangene Randtage sehen willst, setze "
        "require_full_window_coverage = False."
    )

CONTEXT = build_model_context(CONFIG)
INTERVAL_CACHE: dict[tuple[str, str], tuple[dict, dict]] = {}
SEQUENCE_CACHE: dict[str, RTISequenceResult] = {}

app = Flask(__name__)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def get_sequence_result(ref_idx: int, force: bool) -> tuple[int, RTISequenceResult]:
    ref_idx = clamp(ref_idx, 0, len(AVAILABLE_DATES) - 1)
    ref_date = AVAILABLE_DATES[ref_idx]

    if force:
        SEQUENCE_CACHE.pop(ref_date, None)

    result = SEQUENCE_CACHE.get(ref_date)
    if result is None:
        result = compute_temporal_sequence(
            context=CONTEXT,
            ref_date=ref_date,
            sequence_dates=AVAILABLE_DATES,
            interval_cache=INTERVAL_CACHE,
            config=CONFIG,
        )
        SEQUENCE_CACHE[ref_date] = result

    return ref_idx, result


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
    default_ref_idx = 0
    default_cur_idx = len(AVAILABLE_DATES) - 1
    return render_template(
        "index.html",
        available_dates=AVAILABLE_DATES,
        default_ref_idx=default_ref_idx,
        default_cur_idx=default_cur_idx,
        max_idx=len(AVAILABLE_DATES) - 1,
    )


@app.route("/api/update")
def api_update():
    ref_idx = int(request.args.get("ref_idx", 0))
    cur_idx = int(request.args.get("cur_idx", 0))
    force = request.args.get("force", "0") == "1"

    ref_idx, result = get_sequence_result(ref_idx, force)
    cur_idx = clamp(cur_idx, 0, len(result.sequence_dates) - 1)
    ref_date = AVAILABLE_DATES[ref_idx]
    cur_date = result.sequence_dates[cur_idx]

    png_bytes = render_plot_png(result, ref_date, cur_idx)

    return jsonify(
        {
            "ref_idx": ref_idx,
            "cur_idx": cur_idx,
            "ref_label": interval_label(ref_date, CONFIG.ref_start_hhmm, CONFIG.ref_end_hhmm),
            "cur_label": interval_label(cur_date, CONFIG.cur_start_hhmm, CONFIG.cur_end_hhmm),
            "info": build_info_text(result, ref_date, cur_idx),
            "image": base64.b64encode(png_bytes).decode("ascii"),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=False)
